from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import secrets
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_ollama import ChatOllama
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from pydantic import BaseModel, Field, UUID4, field_validator
from pythonjsonlogger import jsonlogger
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

try:  # Supports both `uvicorn app:app` and `import backend.app`.
    from .database import ChatSession, Database, Message
    from .weather import WeatherClient, WeatherUnavailable, detect_weather_request, format_weather
    from .places import (
        OverpassPlaceFinder,
        PlaceSearchUnavailable,
        detect_place_category,
        format_place_results,
    )
except ImportError:  # pragma: no cover - exercised by the Docker entrypoint
    from database import ChatSession, Database, Message
    from weather import WeatherClient, WeatherUnavailable, detect_weather_request, format_weather
    from places import (
        OverpassPlaceFinder,
        PlaceSearchUnavailable,
        detect_place_category,
        format_place_results,
    )


# ── Logging ───────────────────────────────────────────────────────────────────

UUID_PATTERN = re.compile(
    r"(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"(?![0-9a-f])",
    re.IGNORECASE,
)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_ref(session_id: str) -> str:
    """Return a non-reversible, low-cardinality-safe reference for diagnostics."""

    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


class RedactSessionIdFilter(logging.Filter):
    """Prevent UUID session IDs in Uvicorn access-log request paths."""

    @staticmethod
    def _redact(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return UUID_PATTERN.sub(
            lambda match: f"session_ref={session_ref(match.group(0))}",
            value,
        )

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: self._redact(value) for key, value in record.args.items()
            }
        return True


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("agente")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not getattr(logger, "_agente_configured", False):
        formatter = jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        try:
            log_directory = Path(os.getenv("LOG_DIRECTORY", "logs"))
            log_directory.mkdir(exist_ok=True)
            file_handler = logging.FileHandler(log_directory / "agente.log")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            logger.warning(
                "Log em arquivo indisponível; mantendo saída no terminal",
                extra={"error_type": type(exc).__name__},
            )

        logger._agente_configured = True  # type: ignore[attr-defined]

    access_filter = RedactSessionIdFilter()
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(access_filter)
    for handler in uvicorn_access_logger.handlers:
        handler.addFilter(access_filter)
    return logger


# ── Metrics ───────────────────────────────────────────────────────────────────

MODEL_NAME = os.getenv("OLLAMA_MODEL", "gemma3:4b")

REQUEST_COUNT = Counter(
    "agente_requests_total",
    "Total de interações com o agente",
    ["model", "status"],
)
RESPONSE_DURATION = Histogram(
    "agente_response_duration_seconds",
    "Tempo de resposta do modelo em segundos",
    ["model"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0],
)
ACTIVE_SESSIONS = Gauge(
    "agente_active_sessions",
    "Número de sessões ativas",
)


# ── Tracing ───────────────────────────────────────────────────────────────────

def setup_tracing() -> trace.Tracer:
    resource = Resource.create({"service.name": "agente-conversacional"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=os.getenv("JAEGER_ENDPOINT", "http://localhost:4317"),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("agente-conversacional")


# ── Agent ─────────────────────────────────────────────────────────────────────

def load_system_prompt(path: str | Path | None = None) -> str:
    prompt_path = Path(path) if path else Path(__file__).resolve().with_name(
        "system_prompt.txt"
    )
    return prompt_path.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = load_system_prompt()


def build_chain():
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
        ]
    )
    llm = ChatOllama(
        model=MODEL_NAME,
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=0.2,
    )
    return prompt | llm


# ── API models and helpers ────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: UUID4
    message: str = Field(min_length=1, max_length=4_000)

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A mensagem não pode estar vazia")
        return value


class SessionResponse(BaseModel):
    session_id: UUID4
    session_token: str
    expires_at: datetime


class ChatResponse(BaseModel):
    response: str


class ExportedMessage(BaseModel):
    role: str
    content: str
    timestamp: datetime


class ExportResponse(BaseModel):
    session_id: UUID4
    exported_at: datetime
    total_messages: int
    messages: list[ExportedMessage]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_session_ttl(session_ttl_hours: float | None = None) -> timedelta:
    raw_value: str | float = (
        session_ttl_hours
        if session_ttl_hours is not None
        else os.getenv("SESSION_TTL_HOURS", "24")
    )
    try:
        hours = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SESSION_TTL_HOURS deve ser um número") from exc
    if not math.isfinite(hours) or not 0 < hours <= 8_760:
        raise RuntimeError("SESSION_TTL_HOURS deve estar entre 0 e 8760")
    return timedelta(hours=hours)


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sessão ou credencial inválida",
        headers={
            "WWW-Authenticate": "Bearer",
            "Cache-Control": "no-store",
        },
    )


def get_bearer_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    if not authorization or len(authorization) > 512:
        raise unauthorized()
    scheme, separator, credentials = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not credentials.strip():
        raise unauthorized()
    return credentials.strip()


def remove_expired_sessions(database_session: Session, now: datetime) -> int:
    expired_ids = list(
        database_session.scalars(
            select(ChatSession.id).where(ChatSession.expires_at <= now)
        )
    )
    if not expired_ids:
        return 0
    database_session.execute(
        delete(Message).where(Message.session_id.in_(expired_ids))
    )
    database_session.execute(
        delete(ChatSession).where(ChatSession.id.in_(expired_ids))
    )
    return len(expired_ids)


def update_active_sessions_metric(database_session: Session, now: datetime) -> None:
    active_count = database_session.scalar(
        select(func.count())
        .select_from(ChatSession)
        .where(ChatSession.expires_at > now)
    )
    ACTIVE_SESSIONS.set(active_count or 0)


def authenticate_session(
    database_session: Session,
    session_id: str,
    token: str,
) -> ChatSession:
    chat_session = database_session.get(ChatSession, session_id)
    if chat_session is None:
        # Hashing even for a missing session makes valid/invalid IDs less distinguishable.
        secrets.compare_digest(token_digest(token), "0" * 64)
        raise unauthorized()

    now = utc_now()
    if as_utc(chat_session.expires_at) <= now:
        database_session.execute(
            delete(Message).where(Message.session_id == chat_session.id)
        )
        database_session.delete(chat_session)
        database_session.commit()
        update_active_sessions_metric(database_session, now)
        logger.info(
            "Sessão expirada removida",
            extra={"session_ref": session_ref(session_id)},
        )
        raise unauthorized()

    if not secrets.compare_digest(chat_session.token_hash, token_digest(token)):
        logger.warning(
            "Credencial de sessão rejeitada",
            extra={"session_ref": session_ref(session_id)},
        )
        raise unauthorized()
    return chat_session


def persisted_history(database_session: Session, session_id: str) -> list[Any]:
    records = database_session.scalars(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.id)
    ).all()
    history: list[Any] = []
    for record in records:
        if record.role == "user":
            history.append(HumanMessage(content=record.content))
        elif record.role == "assistant":
            history.append(AIMessage(content=record.content))
    return history


def normalize_intent(value: str) -> str:
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def asks_for_previous_user_message(value: str) -> bool:
    """Identify common requests to recall the user's previous message."""

    normalized = normalize_intent(value)
    tokens = set(normalized.split())
    refers_to_previous = bool(
        tokens.intersection({"ultima", "ultimo", "anterior", "antes"})
    )
    refers_to_message = bool(
        tokens.intersection(
            {
                "pergunta",
                "mensagem",
                "perguntei",
                "mandei",
                "enviei",
                "escrevi",
                "disse",
                "fiz",
            }
        )
    )
    refers_to_user = bool(
        tokens.intersection(
            {"eu", "meu", "minha", "mandei", "enviei", "escrevi", "disse", "fiz"}
        )
    )
    return refers_to_previous and refers_to_message and refers_to_user


def conversation_memory_response(value: str, history: list[Any]) -> str | None:
    if not asks_for_previous_user_message(value):
        return None

    previous_message = next(
        (
            message.content
            for message in reversed(history)
            if isinstance(message, HumanMessage)
        ),
        None,
    )
    if previous_message is None:
        return (
            "Esta é a sua primeira pergunta nesta conversa; "
            "ainda não há uma pergunta anterior."
        )
    return f'Sua última pergunta foi: "{previous_message}"'


# ── App ───────────────────────────────────────────────────────────────────────

logger = setup_logging()
tracer = setup_tracing()


def create_app(
    database: Database | None = None,
    chain: Any | None = None,
    place_finder: Any | None = None,
    session_ttl_hours: float | None = None,
    weather_client: Any | None = None,
) -> FastAPI:
    database = database or Database()
    chain = chain or build_chain()
    place_finder = place_finder or OverpassPlaceFinder()
    weather_client = weather_client or WeatherClient()
    session_ttl = resolve_session_ttl(session_ttl_hours)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.create_schema()
        with database.session_factory() as database_session:
            now = utc_now()
            if remove_expired_sessions(database_session, now):
                database_session.commit()
            update_active_sessions_metric(database_session, now)
        yield

    application = FastAPI(lifespan=lifespan)
    application.state.database = database
    application.state.chain = chain
    application.state.place_finder = place_finder
    application.state.weather_client = weather_client
    application.mount("/metrics", make_asgi_app())
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def answer_weather(weather_request) -> tuple[str, str]:
        if weather_request.explanation is not None:
            return weather_request.explanation, "weather_scope"
        with tracer.start_as_current_span("consulta_previsao") as weather_span:
            weather_span.set_attribute("provider", "open-meteo")
            try:
                report = weather_client.forecast()
                content = format_weather(weather_request, report)
                weather_span.set_attribute("cache_hit", report.cached)
                weather_span.set_attribute("fetched_at", report.fetched_at.isoformat())
                return content, "weather"
            except WeatherUnavailable as exc:
                weather_span.record_exception(exc)
                return (
                    "Não consegui consultar a previsão para a Estrada Nova agora. "
                    "Tente novamente em alguns instantes.",
                    "weather_error",
                )

    def answer_places(category) -> tuple[str, str]:
        with tracer.start_as_current_span("consulta_locais") as places_span:
            places_span.set_attribute("category", category.key)
            try:
                places = place_finder.search(category)
                source = "openstreetmap"
                snapshot_updated_at = getattr(places, "snapshot_updated_at", None)
                if snapshot_updated_at is not None:
                    source = "openstreetmap_snapshot"
                    places_span.set_attribute("snapshot_updated_at", snapshot_updated_at.isoformat())
                places_span.set_attribute("response_source", source)
                return format_place_results(category, places), source
            except PlaceSearchUnavailable as exc:
                places_span.record_exception(exc)
                return (
                    "Não consegui consultar o mapa da Estrada Nova agora. "
                    "Tente novamente em alguns instantes.",
                    "openstreetmap_error",
                )

    @application.post(
        "/sessions",
        response_model=SessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_session_endpoint(
        response: Response,
        database_session: Session = Depends(database.get_session),
    ) -> SessionResponse:
        now = utc_now()
        if remove_expired_sessions(database_session, now):
            database_session.flush()

        new_session_id = str(uuid.uuid4())
        session_token = secrets.token_urlsafe(32)
        expires_at = now + session_ttl
        database_session.add(
            ChatSession(
                id=new_session_id,
                token_hash=token_digest(session_token),
                created_at=now,
                expires_at=expires_at,
            )
        )
        database_session.commit()
        update_active_sessions_metric(database_session, now)

        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        logger.info(
            "Sessão criada",
            extra={"session_ref": session_ref(new_session_id)},
        )
        return SessionResponse(
            session_id=uuid.UUID(new_session_id),
            session_token=session_token,
            expires_at=expires_at,
        )

    @application.post("/chat", response_model=ChatResponse)
    def chat_endpoint(
        req: ChatRequest,
        response: Response,
        token: str = Depends(get_bearer_token),
        database_session: Session = Depends(database.get_session),
    ) -> ChatResponse:
        session_id = str(req.session_id)
        chat_session = authenticate_session(database_session, session_id, token)
        reference = session_ref(session_id)

        with tracer.start_as_current_span("interacao") as span:
            span.set_attribute("session_ref", reference)
            span.set_attribute("input_length", len(req.message))
            span.set_attribute("model", MODEL_NAME)

            history = persisted_history(database_session, session_id)
            database_session.add(
                Message(
                    session_id=session_id,
                    role="user",
                    content=req.message,
                    created_at=utc_now(),
                )
            )
            database_session.commit()

            started_at = time.perf_counter()
            try:
                content = conversation_memory_response(req.message, history)
                response_source = "conversation_memory"
                sources = []
                if content is None:
                    weather_request = detect_weather_request(
                        req.message,
                        history=[message.content for message in history if isinstance(message, HumanMessage)],
                    )
                    category = detect_place_category(req.message)
                    jobs = []
                    if category is not None:
                        jobs.append((answer_places, category))
                    if weather_request is not None:
                        jobs.append((answer_weather, weather_request))
                    if len(jobs) > 1:
                        # Copy the current trace into each worker. The database
                        # session stays in this handler's thread; tools only fetch data.
                        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
                            futures = [executor.submit(copy_context().run, function, argument) for function, argument in jobs]
                            results = [future.result() for future in futures]
                    else:
                        results = [function(argument) for function, argument in jobs]
                    if results:
                        content = "\n\n".join(text for text, _source in results)
                        sources = [source for _text, source in results]
                        response_source = "+".join(sources)
                        span.set_attribute("tools_used", len(results))
                    else:
                        response_source = "model"
                        result = chain.invoke({"input": req.message, "history": history})
                        content = getattr(result, "content", result)
                        if not isinstance(content, str):
                            content = str(content)

                duration = time.perf_counter() - started_at

                # Do not persist or return a response if the session expired while
                # the model was generating it.
                if as_utc(chat_session.expires_at) <= utc_now():
                    database_session.execute(
                        delete(Message).where(Message.session_id == session_id)
                    )
                    database_session.delete(chat_session)
                    database_session.commit()
                    update_active_sessions_metric(database_session, utc_now())
                    raise unauthorized()

                span.set_attribute("response_length", len(content))
                span.set_attribute("duration_seconds", round(duration, 3))
                span.set_attribute("response_source", response_source)
                interaction_status = "error" if any(source.endswith("_error") for source in sources) else "ok"
                REQUEST_COUNT.labels(model=MODEL_NAME, status=interaction_status).inc()
                RESPONSE_DURATION.labels(model=MODEL_NAME).observe(duration)

                database_session.add(
                    Message(
                        session_id=session_id,
                        role="assistant",
                        content=content,
                        created_at=utc_now(),
                    )
                )
                database_session.commit()

                logger.info(
                    "Interação concluída",
                    extra={
                        "session_ref": reference,
                        "input_length": len(req.message),
                        "response_length": len(content),
                        "duration_seconds": round(duration, 3),
                        "response_source": response_source,
                    },
                )
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                return ChatResponse(response=content)

            except HTTPException:
                REQUEST_COUNT.labels(model=MODEL_NAME, status="error").inc()
                raise
            except (ConnectionError, TimeoutError, OSError) as exc:
                REQUEST_COUNT.labels(model=MODEL_NAME, status="error").inc()
                span.record_exception(exc)
                logger.error(
                    "Erro de conexão com o modelo",
                    extra={
                        "session_ref": reference,
                        "error_type": type(exc).__name__,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Erro de conexão com o modelo. Tente novamente.",
                ) from exc
            except Exception as exc:
                REQUEST_COUNT.labels(model=MODEL_NAME, status="error").inc()
                span.record_exception(exc)
                logger.error(
                    "Erro inesperado",
                    extra={
                        "session_ref": reference,
                        "error_type": type(exc).__name__,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Erro interno do servidor. Tente novamente.",
                ) from exc

    @application.get("/export/{session_id}", response_model=ExportResponse)
    def export_session(
        session_id: UUID4,
        response: Response,
        token: str = Depends(get_bearer_token),
        database_session: Session = Depends(database.get_session),
    ) -> ExportResponse:
        normalized_session_id = str(session_id)
        authenticate_session(database_session, normalized_session_id, token)
        messages = database_session.scalars(
            select(Message)
            .where(Message.session_id == normalized_session_id)
            .order_by(Message.id)
        ).all()

        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return ExportResponse(
            session_id=session_id,
            exported_at=utc_now(),
            total_messages=len(messages),
            messages=[
                ExportedMessage(
                    role=message.role,
                    content=message.content,
                    timestamp=as_utc(message.created_at),
                )
                for message in messages
            ],
        )

    return application


app = create_app()
