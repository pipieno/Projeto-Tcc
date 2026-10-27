from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from functools import partial

import httpx
import pytest

from backend import weather
from backend.app import MODEL_NAME, REQUEST_COUNT, create_app
from backend.database import Database
from backend.places import Place, PlaceSearchUnavailable
from backend.weather import (
    TIMEZONE, WeatherClient, WeatherReport, WeatherUnavailable,
    detect_weather_request, format_weather,
)


TODAY = date(2026, 10, 26)
NOW = datetime(2026, 10, 26, 20, 0, tzinfo=TIMEZONE)
ASYNC_CLIENT = httpx.AsyncClient
COMBINED_REQUEST = (
    "ta mas assim me de um restaurante da estrada nova com a sua localização, "
    "e tambem me fala se amanha a previsao esta para chuva ou nao"
)


def payload(today=TODAY):
    return {
        "timezone": "America/Sao_Paulo",
        "current": {
            "time": f"{today}T20:00", "temperature_2m": 18.5,
            "apparent_temperature": 18, "weather_code": 2, "wind_speed_10m": 9,
            "precipitation": 0, "interval": 900,
        },
        "daily": {
            "time": [(today + timedelta(days=day)).isoformat() for day in range(7)],
            "weather_code": [3, 61, 2, 0, 3, 80, 51],
            "temperature_2m_min": [14] * 7,
            "temperature_2m_max": [23] * 7,
            "precipitation_probability_max": [20, 70, 10, 0, 20, 80, 30],
            "precipitation_sum": [0, 4.5, 0, 0, 0.2, 10, 0.5],
            "wind_speed_10m_max": [12] * 7,
        },
    }


@pytest.mark.parametrize("message,offset", [
    ("Vai chover no bairro amanhã?", 1),
    ("qual a temperatura hoje?", 0),
    ("como está o tempo?", 0),
    ("como vai estar o tempo depois de amanhã?", 2),
    ("qual a previsão para sábado?", 5),
    ("vai fazer sol na Estrada Nova amanhã?", 1),
    ("temperatura no bairro para amanhã", 1),
    ("Vai chover em Jaraguá do Sul amanhã?", 1),
    ("vai chover daqui a 3 dias?", 3),
    ("vai chover em 2 dias?", 2),
    ("qual o clima no bairro?", 0),
    ("qual o clima atual da estrada nova, esta chuvendo?", 0),
    ("qual o clima atual da Estrada Nova, está chovendo?", 0),
    ("me fala o clima atual", 0),
    ("qual é o clima da Estrada Nova?", 0),
    ("tá chuvendo?", 0),
    ("está chovendo agora?", 0),
    ("qual o tempo atual do bairro?", 0),
])
def test_weather_questions_select_local_dates(message, offset):
    result = detect_weather_request(message, today=TODAY)
    assert result.days == (TODAY + timedelta(days=offset),)
    assert result.explanation is None


@pytest.mark.parametrize("message", [
    "Quanto tempo demora para me associar?",
    "Qual a previsão da próxima assembleia?",
    "A associação está aberta há quanto tempo?",
    "me manda as farmácias",
    "onde posso comer na estrada nova?",
    "Como faço para me associar?",
    "Como está o clima da associação?",
    "Qual o clima das reuniões da AMOBEN?",
])
def test_other_questions_do_not_trigger_weather(message):
    assert detect_weather_request(message, today=TODAY) is None


def test_follow_up_reuses_weather_context_but_not_after_topic_change():
    result = detect_weather_request("e amanhã?", history=["como está o tempo?"], today=TODAY)
    assert result.days == (TODAY + timedelta(days=1),)
    assert detect_weather_request("e amanhã?", history=["como está o tempo?", "onde ficam as farmácias?"], today=TODAY) is None
    result = detect_weather_request("e a temperatura?", history=["vai chover amanhã?"], today=TODAY)
    assert result.days == (TODAY + timedelta(days=1),)


@pytest.mark.parametrize("message", ["qual a previsão da semana?", "previsão dos próximos 7 dias"])
def test_week_forecast_uses_seven_dates(message):
    result = detect_weather_request(message, today=TODAY)
    assert len(result.days) == 7
    assert result.days[-1] == TODAY + timedelta(days=6)


@pytest.mark.parametrize("message", [
    "Vai chover em Curitiba amanhã?",
    "qual a temperatura em São Paulo?",
    "qual a temperatura de Curitiba amanhã?",
    "qual a previsão para a próxima semana?",
    "qual a temperatura em 20/12/2026?",
    "como estava o tempo ontem?",
    "vai chover daqui a 10 dias?",
    "vai chover no próximo mês?",
])
def test_other_locations_and_unsupported_dates_are_explained(message):
    result = detect_weather_request(message, today=TODAY)
    assert result.explanation
    assert result.days == ()


def test_today_uses_sao_paulo_date_at_utc_midnight():
    utc_moment = datetime.fromisoformat("2026-10-27T01:00:00+00:00")
    assert utc_moment.astimezone(TIMEZONE).date() == TODAY
    result = detect_weather_request("vai chover amanhã?", today=utc_moment.astimezone(TIMEZONE).date())
    assert result.days == (date(2026, 10, 27),)


def test_today_and_tomorrow_returns_both_dates():
    result = detect_weather_request("vai chover hoje e amanhã?", today=TODAY)
    assert result.days == (TODAY, TODAY + timedelta(days=1))


@pytest.mark.parametrize("message", [
    COMBINED_REQUEST,
    "me dê um restaurante em Estrada Nova com a localização e diga se vai chover amanhã",
    "vai chover amanhã? E me indica um restaurante",
    "previsão amanhã em Jaraguá do Sul e um restaurante com endereço",
])
def test_combined_request_does_not_treat_other_request_as_foreign_location(message):
    result = detect_weather_request(message, today=TODAY)
    assert result.explanation is None
    assert result.days == (TODAY + timedelta(days=1),)


def test_formatted_forecast_has_probability_units_date_and_source():
    result = detect_weather_request("vai chover amanhã?", today=TODAY)
    content = format_weather(result, WeatherReport(payload(), NOW))
    assert "27/10/2026" in content
    assert "70%" in content
    assert "4,5 mm" in content
    assert "14 °C" in content
    assert "12 km/h" in content
    assert "Open-Meteo" in content
    assert "São Paulo" in content


def test_missing_probability_is_not_reported_as_zero():
    data = payload()
    data["daily"]["precipitation_probability_max"][1] = None
    result = detect_weather_request("vai chover amanhã?", today=TODAY)
    content = format_weather(result, WeatherReport(data, NOW))
    assert "Chance máxima de chuva: não informado" in content


def test_current_weather_resets_tomorrow_context_and_reports_precipitation():
    request = detect_weather_request(
        "qual o clima atual da estrada nova, esta chuvendo?",
        history=["Vai chover amanhã?"], today=TODAY,
    )
    assert request.days == (TODAY,)
    assert request.include_current
    data = payload()
    data["current"]["precipitation"] = 0.4
    content = format_weather(request, WeatherReport(data, NOW))
    assert "Condições estimadas às 20:00" in content
    assert "Precipitação estimada nos últimos 15 minutos: 0,4 mm" in content


def test_missing_current_precipitation_is_not_reported_as_zero():
    data = payload()
    data["current"]["precipitation"] = None
    request = detect_weather_request("tá chuvendo?", today=TODAY)
    content = format_weather(request, WeatherReport(data, NOW))
    assert "Precipitação estimada nos últimos 15 minutos: não informado" in content


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(weather, "local_now", lambda: NOW)
    monkeypatch.setenv("WEATHER_CACHE_TTL_SECONDS", "600")
    monkeypatch.setenv("WEATHER_TIMEOUT_SECONDS", "8")
    return WeatherClient()


def mock_network(monkeypatch, handler):
    monkeypatch.setattr(weather.httpx, "AsyncClient", partial(ASYNC_CLIENT, transport=httpx.MockTransport(handler)))


def test_api_parameters_and_cache(client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["timezone"] == "America/Sao_Paulo"
        assert request.url.params["latitude"] == str(client.latitude)
        assert request.url.params["wind_speed_unit"] == "kmh"
        assert request.url.params["forecast_days"] == "7"
        return httpx.Response(200, json=payload())

    mock_network(monkeypatch, handler)
    assert not client.forecast().cached
    assert client.forecast().cached
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", [
    {"timezone": "GMT"},
    {"current": {}},
    {"daily": {}},
    {"daily": {**payload()["daily"], "precipitation_probability_max": [150] * 7}},
    {"daily": {**payload()["daily"], "temperature_2m_max": [float("inf")] * 7}},
])
def test_invalid_data_is_not_cached(client, monkeypatch, invalid):
    data = payload()
    data.update(invalid)

    async def fetch():
        return data

    monkeypatch.setattr(client, "_fetch", fetch)
    with pytest.raises(WeatherUnavailable):
        client.forecast()
    assert client._cache is None


def test_temporary_failure_retries_then_succeeds(client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=payload())

    async def sleep(_seconds):
        pass

    monkeypatch.setattr(weather.asyncio, "sleep", sleep)
    mock_network(monkeypatch, handler)
    assert client.forecast().payload == payload()
    assert len(calls) == 2


def test_rate_limited_response_does_not_retry_immediately(client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    mock_network(monkeypatch, handler)
    with pytest.raises(WeatherUnavailable):
        client.forecast()
    assert len(calls) == 1


def test_total_timeout_includes_retry_delay(client, monkeypatch):
    client.timeout_seconds = 0.1
    mock_network(monkeypatch, lambda _request: httpx.Response(503))
    started = time.monotonic()
    with pytest.raises(WeatherUnavailable):
        client.forecast()
    assert time.monotonic() - started < 0.5


def test_cache_is_refreshed_at_local_midnight(client, monkeypatch):
    calls = []

    async def fetch():
        calls.append(True)
        return payload(weather.local_now().date())

    monkeypatch.setattr(client, "_fetch", fetch)
    client.forecast()
    monkeypatch.setattr(weather, "local_now", lambda: NOW + timedelta(days=1))
    assert not client.forecast().cached
    assert len(calls) == 2


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeWeather:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def forecast(self):
        self.calls += 1
        if self.fail:
            raise WeatherUnavailable("falha simulada")
        now = weather.local_now()
        return WeatherReport(payload(now.date()), now)


class FakePlaces:
    def __init__(self):
        self.calls = []
        self.fail = False

    def search(self, category):
        self.calls.append(category.key)
        if self.fail:
            raise PlaceSearchUnavailable("falha simulada no mapa")
        return [Place("Restaurante do Bairro", "Rua José Picolli, 100", 150, -26.47, -49.126)]


@pytest.fixture
async def api():
    database = Database("sqlite+pysqlite:///:memory:")
    provider = FakeWeather()
    provider.places = FakePlaces()

    class NoModel:
        def invoke(self, _payload):
            raise AssertionError("A consulta meteorológica não deve chamar o modelo")

    app = create_app(database=database, chain=NoModel(), weather_client=provider, place_finder=provider.places)
    try:
        async with app.router.lifespan_context(app):
            async with ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                yield client, provider
    finally:
        database.dispose()


@pytest.mark.anyio
async def test_weather_follow_up_and_authenticated_export(api):
    client, provider = api
    session = (await client.post("/sessions")).json()
    headers = {"Authorization": "Bearer " + session["session_token"]}
    for message in ["Qual a previsão do tempo hoje?", "e amanhã?"]:
        response = await client.post("/chat", headers=headers, json={"session_id": session["session_id"], "message": message})
        assert response.status_code == 200
        assert "Open-Meteo" in response.json()["response"]
    assert provider.calls == 2
    exported = await client.get("/export/" + session["session_id"], headers=headers)
    assert exported.json()["total_messages"] == 4


@pytest.mark.anyio
async def test_exact_current_weather_message_calls_tool_and_handles_follow_up(api):
    client, provider = api
    session = (await client.post("/sessions")).json()
    headers = {"Authorization": "Bearer " + session["session_token"]}
    for message in ["qual o clima atual da estrada nova, esta chuvendo?", "e amanhã?"]:
        response = await client.post("/chat", headers=headers, json={"session_id": session["session_id"], "message": message})
        assert response.status_code == 200
        assert "Fonte: Open-Meteo" in response.json()["response"]
        if "chuvendo" in message:
            assert "Condições estimadas" in response.json()["response"]
            assert "Precipitação estimada" in response.json()["response"]
    assert provider.calls == 2


@pytest.mark.anyio
async def test_weather_failure_is_friendly_and_counted_as_error(api):
    client, provider = api
    provider.fail = True
    session = (await client.post("/sessions")).json()
    before = REQUEST_COUNT.labels(model=MODEL_NAME, status="error")._value.get()
    response = await client.post("/chat", headers={"Authorization": "Bearer " + session["session_token"]},
                                 json={"session_id": session["session_id"], "message": "vai chover amanhã?"})
    assert response.status_code == 200
    assert "Não consegui consultar a previsão" in response.json()["response"]
    assert REQUEST_COUNT.labels(model=MODEL_NAME, status="error")._value.get() == before + 1


@pytest.mark.anyio
async def test_foreign_city_is_explained_without_calling_api(api):
    client, provider = api
    session = (await client.post("/sessions")).json()
    response = await client.post("/chat", headers={"Authorization": "Bearer " + session["session_token"]},
                                 json={"session_id": session["session_id"], "message": "vai chover em Curitiba amanhã?"})
    assert "apenas para a Estrada Nova" in response.json()["response"]
    assert provider.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("message", [
    COMBINED_REQUEST,
    "vai chover amanhã? E me indica um restaurante",
    "vai chover amanhã e um restaurante?",
    "me de um restaurante, e amanhã vai chover?",
])
async def test_combined_request_uses_both_tools_and_exports_one_answer(api, message):
    client, provider = api
    session = (await client.post("/sessions")).json()
    headers = {"Authorization": "Bearer " + session["session_token"]}
    response = await client.post("/chat", headers=headers, json={"session_id": session["session_id"], "message": message})
    assert response.status_code == 200
    content = response.json()["response"]
    assert "Restaurante do Bairro" in content
    assert "https://www.google.com/maps/search/" in content
    assert "Fonte: Open-Meteo" in content
    assert provider.calls == 1
    assert provider.places.calls == ["food"]
    exported = (await client.get("/export/" + session["session_id"], headers=headers)).json()
    assert exported["total_messages"] == 2
    assert exported["messages"][1]["content"] == content


@pytest.mark.anyio
@pytest.mark.parametrize("weather_fail,places_fail", [(True, False), (False, True), (True, True)])
async def test_combined_request_preserves_other_result_on_partial_failure(api, weather_fail, places_fail):
    client, provider = api
    provider.fail = weather_fail
    provider.places.fail = places_fail
    session = (await client.post("/sessions")).json()
    before = REQUEST_COUNT.labels(model=MODEL_NAME, status="error")._value.get()
    response = await client.post("/chat", headers={"Authorization": "Bearer " + session["session_token"]},
                                 json={"session_id": session["session_id"], "message": COMBINED_REQUEST})
    content = response.json()["response"]
    assert response.status_code == 200
    assert ("Não consegui consultar a previsão" in content) == weather_fail
    assert ("Não consegui consultar o mapa" in content) == places_fail
    assert ("Restaurante do Bairro" in content) == (not places_fail)
    assert ("Fonte: Open-Meteo" in content) == (not weather_fail)
    assert REQUEST_COUNT.labels(model=MODEL_NAME, status="error")._value.get() == before + 1


@pytest.mark.anyio
async def test_combined_tools_run_concurrently_in_the_same_trace(api, monkeypatch):
    import threading
    from opentelemetry import trace

    client, provider = api
    barrier = threading.Barrier(2)
    seen_trace_ids = []
    forecast = provider.forecast
    search = provider.places.search

    def weather_call():
        seen_trace_ids.append(trace.get_current_span().get_span_context().trace_id)
        barrier.wait(timeout=2)
        return forecast()

    def places_call(category):
        seen_trace_ids.append(trace.get_current_span().get_span_context().trace_id)
        barrier.wait(timeout=2)
        return search(category)

    monkeypatch.setattr(provider, "forecast", weather_call)
    monkeypatch.setattr(provider.places, "search", places_call)
    session = (await client.post("/sessions")).json()
    response = await client.post("/chat", headers={"Authorization": "Bearer " + session["session_token"]},
                                 json={"session_id": session["session_id"], "message": COMBINED_REQUEST})
    assert response.status_code == 200
    assert len(seen_trace_ids) == 2
    assert seen_trace_ids[0] == seen_trace_ids[1] != 0
