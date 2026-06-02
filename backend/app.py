import os
import time
import logging
from pathlib import Path

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, Gauge, make_asgi_app
from pydantic import BaseModel
from pythonjsonlogger import jsonlogger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_ollama import ChatOllama


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    Path("logs").mkdir(exist_ok=True)
    logger = logging.getLogger("agente")
    logger.setLevel(logging.INFO)
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    file_handler = logging.FileHandler("logs/agente.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger

# ── Métricas ──────────────────────────────────────────────────────────────────

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


# ── Agente ────────────────────────────────────────────────────────────────────

def load_system_prompt(path: str = "system_prompt.txt") -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


SYSTEM_PROMPT = load_system_prompt()
store: dict[str, InMemoryChatMessageHistory] = {}


def get_session_history(session_id: str) -> InMemoryChatMessageHistory:
    if session_id not in store:
        store[session_id] = InMemoryChatMessageHistory()
        ACTIVE_SESSIONS.set(len(store))
    return store[session_id]


def build_chain() -> RunnableWithMessageHistory:
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
        ]
    )
    llm = ChatOllama(
        model=os.getenv("OLLAMA_MODEL", "gemma3:4b"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=0.2,
    )
    chain = prompt | llm
    return RunnableWithMessageHistory(
        chain,
        get_session_history,
        input_messages_key="input",
        history_messages_key="history",
    )


# ── App ───────────────────────────────────────────────────────────────────────

logger = setup_logging()
tracer = setup_tracing()
chat = build_chain()

app = FastAPI()
app.mount("/metrics", make_asgi_app())
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    session_id: str
    message: str


# ── Export ────────────────────────────────────────────────────────────────────

session_history: dict[str, list[dict]] = {}


def record_message(session_id: str, role: str, content: str):
    if session_id not in session_history:
        session_history[session_id] = []
    session_history[session_id].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    with tracer.start_as_current_span("interacao") as span:
        span.set_attribute("session_id", req.session_id)
        span.set_attribute("input_length", len(req.message))
        span.set_attribute("model", MODEL_NAME)

        record_message(req.session_id, "user", req.message)
        inicio = time.perf_counter()
        try:
            resposta = chat.invoke(
                {"input": req.message},
                config={"configurable": {"session_id": req.session_id}},
            )
            duracao = time.perf_counter() - inicio

            span.set_attribute("response_length", len(resposta.content))
            span.set_attribute("duration_seconds", round(duracao, 3))

            REQUEST_COUNT.labels(model=MODEL_NAME, status="ok").inc()
            RESPONSE_DURATION.labels(model=MODEL_NAME).observe(duracao)
            record_message(req.session_id, "assistant", resposta.content)

            logger.info(
                "Interação concluída",
                extra={
                    "session_id": req.session_id,
                    "input_length": len(req.message),
                    "response_length": len(resposta.content),
                    "duration_seconds": round(duracao, 3),
                },
            )
            return {"response": resposta.content}

        except (ConnectionError, TimeoutError, OSError) as exc:
            REQUEST_COUNT.labels(model=MODEL_NAME, status="error").inc()
            span.record_exception(exc)
            logger.error(
                "Erro de conexão com o modelo",
                extra={"session_id": req.session_id, "error": str(exc)},
            )
            return {"response": "Erro de conexão com o modelo. Tente novamente."}

        except Exception as exc:
            REQUEST_COUNT.labels(model=MODEL_NAME, status="error").inc()
            span.record_exception(exc)
            logger.error(
                "Erro inesperado",
                extra={"session_id": req.session_id, "error": str(exc)},
            )
            return {"response": "Erro interno do servidor. Tente novamente."}


@app.get("/export/{session_id}")
def export_session(session_id: str):
    messages = session_history.get(session_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    return {
        "session_id": session_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_messages": len(messages),
        "messages": messages,
    }
