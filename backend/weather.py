"""Weather forecasts for the fixed neighborhood, provided by Open-Meteo."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

try:
    from .places import DEFAULT_CENTER_LAT, DEFAULT_CENTER_LON, _float_setting, _integer_setting, normalize_text
except ImportError:  # Docker runs the modules directly from /app.
    from places import DEFAULT_CENTER_LAT, DEFAULT_CENTER_LON, _float_setting, _integer_setting, normalize_text


TIMEZONE = ZoneInfo("America/Sao_Paulo")
API_URL = "https://api.open-meteo.com/v1/forecast"
logger = logging.getLogger("agente")
DAILY_FIELDS = (
    "weather_code", "temperature_2m_min", "temperature_2m_max",
    "precipitation_probability_max", "precipitation_sum", "wind_speed_10m_max",
)
CURRENT_FIELDS = ("temperature_2m", "apparent_temperature", "weather_code", "wind_speed_10m", "precipitation")
WEEKDAYS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
WEATHER_WORDS = {
    "chover", "chove", "chovera", "chovendo", "chuvendo", "chuva", "chuvas", "garoa", "clima",
    "temperatura", "temperaturas", "vento", "ventos", "ventania", "frio",
    "calor", "nublado", "ensolarado", "meteorologia", "meteorologica",
}
WEATHER_CODES = {
    0: "céu limpo", 1: "predomínio de céu limpo", 2: "parcialmente nublado",
    3: "nublado", 45: "neblina", 48: "neblina com geada",
    51: "garoa leve", 53: "garoa moderada", 55: "garoa intensa",
    56: "garoa congelante leve", 57: "garoa congelante intensa",
    61: "chuva fraca", 63: "chuva moderada", 65: "chuva forte",
    66: "chuva congelante leve", 67: "chuva congelante forte",
    71: "neve fraca", 73: "neve moderada", 75: "neve forte", 77: "grãos de neve",
    80: "pancadas de chuva leves", 81: "pancadas de chuva moderadas",
    82: "pancadas de chuva fortes", 85: "pancadas de neve fracas",
    86: "pancadas de neve fortes", 95: "trovoadas", 96: "trovoadas com granizo",
    97: "trovoadas fortes", 99: "trovoadas fortes com granizo",
}


@dataclass(frozen=True)
class WeatherRequest:
    days: tuple[date, ...]
    include_current: bool = False
    explanation: str | None = None


@dataclass(frozen=True)
class WeatherReport:
    payload: dict
    fetched_at: datetime
    cached: bool = False


class WeatherUnavailable(RuntimeError):
    pass


def local_now() -> datetime:
    return datetime.now(TIMEZONE)


def _parse_request(text: str, previous: WeatherRequest | None, today: date) -> WeatherRequest | None:
    normalized = normalize_text(text)
    tokens = set(normalized.split())
    temporal = bool(tokens.intersection({"hoje", "amanha", "agora", "ontem", "semana", "dias", *WEEKDAYS})) or bool(
        re.search(r"\bproximos (?:sete|7) dias\b", normalized)
    )
    weather_words = tokens.intersection(WEATHER_WORDS)
    # "Clima" is normally a weather request here, but social/organizational
    # uses should still be answered by the association's conversational model.
    if re.search(r"\bclima (?:d[ao]s?|entre (?:os|as)) (?:reunioes|reuniao|assembleia|associacao|moradores|equipe)\b", normalized):
        weather_words.discard("clima")
    explicit = bool(weather_words) or bool(re.search(
        r"\b(?:previsao do tempo|previsao meteorologica|como (?:esta|ta|estara|estava|vai estar) o tempo|"
        r"vai fazer sol|tempo atual)\b", normalized,
    )) or ("previsao" in tokens and temporal and not tokens.intersection({"assembleia", "evento", "reuniao", "entrega"}))
    follow_up = previous is not None and bool(re.fullmatch(
        r"(?:(?:e|entao|para|pra|o|a|no|na|em|de|do|da|dia|feira) )*"
        r"(?:hoje|amanha|depois de amanha|agora|segunda|terca|quarta|quinta|sexta|sabado|domingo|"
        r"(?:os )?proximos (?:sete|7) dias|(?:esta |essa |a )?semana)(?: feira)?", normalized,
    ))
    if not explicit and not follow_up:
        return None

    # A location ends before the next date, predicate or request. In particular,
    # the imperative "me dê" is not the preposition in "temperatura de Curitiba".
    location_end = r"(?= (?:hoje|amanha|agora|depois|atual|esta|vai|e|tambem|me|para|pra|com|sem|que|onde|em|no|na)\b|$)"
    locations = re.findall(r"\b(?:em|no|na) (.+?)" + location_end, normalized)
    locations.extend(re.findall(
        r"\b(?:clima|tempo|temperatura|previsao)(?: atual)? (?:de|da|do) (.+?)" + location_end,
        normalized,
    ))
    for location in locations:
        remaining = set(location.split()).difference({
            "estrada", "nova", "jaragua", "do", "sul", "sc", "bairro", "nosso", "nossa",
            "regiao", "area", "hoje", "amanha", "agora", "depois", "de", "aqui", "centro",
            "manha", "tarde", "noite", "fim", "semana", "final", "feira", *WEEKDAYS,
            "para", "pra", "durante", "esta", "essa", "o", "a", "os", "as", "e",
            "vai", "estar", "ficar", "fazer", "qual", "como", "que", "previsto",
            "proximos", "sete", "7", "dias", "tempo", "clima", "previsao", *WEATHER_WORDS,
            "proximo", "proxima", "mes", "meses", "ano", "anos", "dia",
            "atual", "atualmente", "momento", "neste", "nesse",
        })
        if any(not token.isdigit() for token in remaining):
            return WeatherRequest((), explanation="Consigo consultar a previsão apenas para a Estrada Nova, em Jaraguá do Sul. Você pode perguntar como estará o tempo no bairro hoje ou amanhã.")

    if tokens.intersection({"ontem", "passado", "passada"}) or re.search(r"\b\d+ de \w+\b", normalized):
        return WeatherRequest((), explanation="Consigo consultar a previsão dos próximos sete dias. Informe hoje, amanhã, depois de amanhã ou um dia da semana dentro desse período.")
    if re.search(r"\b\d{1,2}[/-]\d{1,2}", text) or re.search(r"\b(?:proxim[ao]|que vem)\b", normalized):
        # A complete seven-day request is supported below; ambiguous future dates are not guessed.
        return WeatherRequest((), explanation="Consigo consultar os próximos sete dias. Para escolher uma data, diga hoje, amanhã, depois de amanhã ou o nome do dia da semana.")

    relative = re.search(r"\b(?:daqui a|em) (\d+) dias?\b", normalized)
    full_week = bool(re.search(r"\bproximos (?:sete|7) dias\b", normalized))
    if (relative and int(relative.group(1)) > 6) or (
        tokens.intersection({"mes", "meses", "ano", "anos", "dias"}) and not relative and not full_week
    ):
        return WeatherRequest((), explanation="Consigo consultar apenas os próximos sete dias. Você pode pedir a previsão de hoje, amanhã ou da semana.")

    include_current = bool(tokens.intersection({"hoje", "agora", "atual", "atualmente", "momento"}))
    if tokens.intersection({"chovendo", "chuvendo"}) and not (
        tokens.intersection({"amanha", "semana", *WEEKDAYS}) or relative or full_week
    ):
        include_current = True
    offsets = set()
    remaining = normalized
    if "depois de amanha" in remaining:
        offsets.add(2)
        remaining = remaining.replace("depois de amanha", "")
    if "amanha" in remaining.split():
        offsets.add(1)
    if include_current:
        offsets.add(0)
    if offsets:
        offsets = tuple(sorted(offsets))
    elif relative:
        offsets = (int(relative.group(1)),)
    elif "fim de semana" in normalized or "final de semana" in normalized:
        saturday = (5 - today.weekday()) % 7
        offsets = (0,) if today.weekday() == 6 else tuple(day for day in (saturday, saturday + 1) if day < 7)
    elif "semana" in tokens or full_week:
        offsets = tuple(range(7))
    elif any(day in tokens for day in WEEKDAYS):
        offsets = tuple(sorted((index - today.weekday()) % 7 for index, day in enumerate(WEEKDAYS) if day in tokens))
    elif previous is not None:
        return WeatherRequest(previous.days, previous.include_current)
    else:
        offsets, include_current = (0,), True
    return WeatherRequest(tuple(today + timedelta(days=offset) for offset in offsets), include_current)


def detect_weather_request(message: str, history: list[str] | None = None, *, today: date | None = None) -> WeatherRequest | None:
    today = today or local_now().date()
    previous = None
    for prior in history or []:
        previous = _parse_request(prior, previous, today)
    return _parse_request(message, previous, today)


class WeatherClient:
    def __init__(self) -> None:
        self.latitude = _float_setting("NEIGHBORHOOD_CENTER_LAT", DEFAULT_CENTER_LAT, -90, 90)
        self.longitude = _float_setting("NEIGHBORHOOD_CENTER_LON", DEFAULT_CENTER_LON, -180, 180)
        self.timeout_seconds = _integer_setting("WEATHER_TIMEOUT_SECONDS", 8, 2, 30)
        self.cache_ttl_seconds = _integer_setting("WEATHER_CACHE_TTL_SECONDS", 600, 0, 3600)
        self._cache: tuple[float, WeatherReport] | None = None
        self._lock = threading.Lock()

    def forecast(self) -> WeatherReport:
        now = local_now()
        with self._lock:
            if self._cache is not None:
                stored_at, report = self._cache
                if time.monotonic() - stored_at < self.cache_ttl_seconds and report.fetched_at.date() == now.date():
                    return WeatherReport(report.payload, report.fetched_at, cached=True)
        try:
            payload = asyncio.run(self._fetch())
            self._validate(payload)
        except (TimeoutError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            logger.warning("Previsão do tempo indisponível", extra={"error_type": type(exc).__name__})
            raise WeatherUnavailable("Não foi possível consultar uma previsão válida") from exc
        report = WeatherReport(payload, local_now())
        with self._lock:
            self._cache = (time.monotonic(), report)
        return report

    async def _fetch(self) -> dict:
        params = {
            "latitude": self.latitude, "longitude": self.longitude,
            "timezone": "America/Sao_Paulo", "forecast_days": 7,
            "current": ",".join(CURRENT_FIELDS), "daily": ",".join(DAILY_FIELDS),
            "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm",
        }
        last_error = None
        async with asyncio.timeout(self.timeout_seconds):
            async with httpx.AsyncClient(timeout=max(0.05, (self.timeout_seconds - 0.5) / 2)) as client:
                for attempt in range(2):
                    if attempt:
                        await asyncio.sleep(0.5)
                    try:
                        response = await client.get(API_URL, params=params)
                        response.raise_for_status()
                        return response.json()
                    except httpx.HTTPError as exc:
                        last_error = exc
                        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                        logger.warning("Falha na consulta à Open-Meteo", extra={
                            "attempt": attempt + 1, "http_status": status,
                            "error_type": type(exc).__name__, "error_reason": str(exc)[:300],
                        })
                        if status is not None and status not in {408, 500, 502, 503, 504}:
                            break
        raise WeatherUnavailable("Open-Meteo indisponível") from last_error

    @staticmethod
    def _validate(payload: dict) -> None:
        if payload.get("timezone") != "America/Sao_Paulo":
            raise ValueError("Fuso horário inesperado")
        daily = payload["daily"]
        dates = daily["time"]
        if not isinstance(dates, list) or not dates or len(dates) > 7:
            raise ValueError("Previsão diária inválida")
        for value in dates:
            date.fromisoformat(value)
        if dates != [(local_now().date() + timedelta(days=offset)).isoformat() for offset in range(7)]:
            raise ValueError("Datas da previsão desatualizadas ou incompletas")
        for field in DAILY_FIELDS:
            values = daily[field]
            if not isinstance(values, list) or len(values) != len(dates):
                raise ValueError("Série diária incompleta")
            if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)) for value in values):
                raise ValueError("Valor meteorológico inválido")
        for value in daily["precipitation_probability_max"]:
            if value is not None and not 0 <= value <= 100:
                raise ValueError("Probabilidade de chuva inválida")
        current = payload["current"]
        datetime.fromisoformat(current["time"])
        for field in CURRENT_FIELDS:
            value = current[field]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("Condição atual inválida")


def _number(value, suffix: str) -> str:
    if value is None:
        return "não informado"
    return f"{value:g}".replace(".", ",") + suffix


def format_weather(request: WeatherRequest, report: WeatherReport) -> str:
    daily = report.payload["daily"]
    lines = ["Previsão para a Estrada Nova, Jaraguá do Sul (SC)."]
    if request.include_current:
        current = report.payload["current"]
        moment = datetime.fromisoformat(current["time"])
        lines.extend(["", f"Condições estimadas às {moment:%H:%M}: {_number(current['temperature_2m'], ' °C')}, "
                      f"sensação de {_number(current['apparent_temperature'], ' °C')}. "
                      f"{WEATHER_CODES.get(current['weather_code'], 'Condição não informada').capitalize()}. "
                      f"Vento: {_number(current['wind_speed_10m'], ' km/h')}."])
        interval = current.get("interval")
        if isinstance(interval, (int, float)) and not isinstance(interval, bool) and math.isfinite(interval) and interval > 0:
            period = f"nos últimos {_number(interval / 60, ' minutos')}"
        else:
            period = "no intervalo informado pela API"
        lines.append(f"Precipitação estimada {period}: {_number(current['precipitation'], ' mm')}.")
    for day in request.days:
        if day.isoformat() not in daily["time"]:
            raise WeatherUnavailable("A API não retornou a data solicitada")
        index = daily["time"].index(day.isoformat())
        code = daily["weather_code"][index]
        lines.extend([
            "", f"{day:%d/%m/%Y}: {WEATHER_CODES.get(code, 'condição não informada')}.",
            f"Temperatura: mínima de {_number(daily['temperature_2m_min'][index], ' °C')} e máxima de {_number(daily['temperature_2m_max'][index], ' °C')}.",
            f"Chance máxima de chuva: {_number(daily['precipitation_probability_max'][index], '%')}; volume previsto: {_number(daily['precipitation_sum'][index], ' mm')}.",
            f"Vento máximo previsto: {_number(daily['wind_speed_10m_max'][index], ' km/h')}.",
        ])
    lines.extend(["", f"Fonte: Open-Meteo. Consulta em {report.fetched_at:%d/%m/%Y às %H:%M} (horário de São Paulo).",
                  "A previsão pode mudar e representa a região do bairro."])
    return "\n".join(lines)
