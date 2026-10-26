"""Search named places around the fixed OpenStreetMap center of Estrada Nova."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx


DEFAULT_CENTER_LAT = -26.4700649
DEFAULT_CENTER_LON = -49.1264936
DEFAULT_RADIUS_METERS = 1_800
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_OVERPASS_FALLBACK_URL = "https://overpass.private.coffee/api/interpreter"
MAX_SEARCH_ATTEMPTS = 3
DEFAULT_SNAPSHOT_PATH = Path(__file__).with_name("data") / "estrada_nova_places.json"
logger = logging.getLogger("agente")
LOCATION_REQUEST_PATTERN = re.compile(
    r"^(?:(?:e|entao|agora|tambem|por|favor|me|nos|voce|pode|poderia|consegue|conseguiria) )*"
    r"(?:manda|mande|mandar|envia|envie|enviar|mostra|mostre|mostrar|liste|lista|listar|"
    r"passa|passe|passar|indica|indique|indicar|recomenda|recomende|recomendar|"
    r"sugere|sugira|sugerir|busca|busque|buscar|traz|traga|trazer)\b"
)


def normalize_text(value: str) -> str:
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


@dataclass(frozen=True)
class PlaceCategory:
    key: str
    singular: str
    plural: str
    filters: tuple[str, ...]


CATEGORIES = (
    (
        {"cafe", "cafes", "cafeteria", "cafeterias", "coffee"},
        PlaceCategory(
            key="cafe",
            singular="café",
            plural="cafés",
            filters=('["amenity"="cafe"]', '["shop"="coffee"]'),
        ),
    ),
    (
        {"padaria", "padarias", "pao", "paes"},
        PlaceCategory(
            key="bakery",
            singular="padaria",
            plural="padarias",
            filters=('["shop"="bakery"]',),
        ),
    ),
    (
        {
            "restaurante",
            "restaurantes",
            "almocar",
            "almoco",
            "jantar",
            "comer",
            "lanchonete",
            "lanchonetes",
            "pizzaria",
            "pizzarias",
        },
        PlaceCategory(
            key="food",
            singular="local para comer",
            plural="locais para comer",
            filters=('["amenity"~"^(restaurant|fast_food|food_court)$"]',),
        ),
    ),
    (
        {"mercado", "mercados", "supermercado", "supermercados", "conveniencia"},
        PlaceCategory(
            key="market",
            singular="mercado",
            plural="mercados",
            filters=('["shop"~"^(supermarket|convenience|grocery)$"]',),
        ),
    ),
    (
        {"farmacia", "farmacias", "remedio", "remedios"},
        PlaceCategory(
            key="pharmacy",
            singular="farmácia",
            plural="farmácias",
            filters=('["amenity"="pharmacy"]', '["shop"="chemist"]'),
        ),
    ),
    (
        {"gasolina", "combustivel", "abastecer"},
        PlaceCategory(
            key="fuel",
            singular="posto de combustível",
            plural="postos de combustível",
            filters=('["amenity"="fuel"]',),
        ),
    ),
    (
        {"hospital", "hospitais", "upa"},
        PlaceCategory(
            key="hospital",
            singular="hospital",
            plural="hospitais",
            filters=('["amenity"="hospital"]', '["healthcare"="hospital"]'),
        ),
    ),
    (
        {
            "saude",
            "clinica",
            "clinicas",
            "medico",
            "dentista",
            "ubs",
            "postinho",
        },
        PlaceCategory(
            key="health",
            singular="serviço de saúde",
            plural="serviços de saúde",
            filters=('["amenity"~"^(clinic|doctors|dentist|hospital)$"]',),
        ),
    ),
    (
        {"escola", "escolas", "creche", "creches", "colegio", "colegios"},
        PlaceCategory(
            key="education",
            singular="instituição de ensino",
            plural="instituições de ensino",
            filters=('["amenity"~"^(school|kindergarten|college)$"]',),
        ),
    ),
    (
        {"academia", "academias", "fitness"},
        PlaceCategory(
            key="fitness",
            singular="academia",
            plural="academias",
            filters=('["leisure"="fitness_centre"]',),
        ),
    ),
    (
        {"banco", "bancos", "caixa", "atm"},
        PlaceCategory(
            key="bank",
            singular="banco ou caixa eletrônico",
            plural="bancos e caixas eletrônicos",
            filters=('["amenity"~"^(bank|atm)$"]',),
        ),
    ),
    (
        {"parque", "parques", "praca", "pracas", "playground"},
        PlaceCategory(
            key="leisure",
            singular="área de lazer",
            plural="áreas de lazer",
            filters=('["leisure"~"^(park|playground|sports_centre)$"]',),
        ),
    ),
)


def matches_category(tags: dict, category: PlaceCategory) -> bool:
    """Apply the same category filters to a downloaded OSM extract."""
    for expression in category.filters:
        match = re.fullmatch(r'\["([^"]+)"(=|~)"([^"]+)"\]', expression)
        if match is None:
            continue
        key, operator, expected = match.groups()
        value = tags.get(key, "")
        if operator == "=" and value == expected:
            return True
        if operator == "~" and re.search(expected, value):
            return True
    return False


def detect_place_category(value: str) -> PlaceCategory | None:
    normalized = normalize_text(value)
    tokens = set(normalized.split())
    search_terms = {
        "onde",
        "qual",
        "quais",
        "bom",
        "bons",
        "boa",
        "boas",
        "melhor",
        "melhores",
        "comprar",
        "encontrar",
        "achar",
        "indique",
        "indicar",
        "recomende",
        "recomendar",
        "quero",
        "conhecer",
        "saber",
        "procuro",
        "procurando",
        "mostre",
        "mostrar",
        "tem",
        "existe",
        "existem",
        "lista",
        "endereco",
        "perto",
        "proximo",
        "proximos",
        "proxima",
        "proximas",
    }
    has_location_context = bool(tokens.intersection(search_terms)) or (
        "estrada nov" in normalized
    ) or bool(LOCATION_REQUEST_PATTERN.match(normalized))
    follow_up_fillers = {
        "e",
        "sobre",
        "um",
        "uma",
        "algum",
        "alguma",
        "alguns",
        "algumas",
        "o",
        "a",
        "os",
        "as",
    }

    for terms, category in CATEGORIES:
        if not tokens.intersection(terms):
            continue
        is_short_follow_up = len(tokens) <= 4 and (
            tokens.difference(terms).issubset(follow_up_fillers)
        )
        if has_location_context or is_short_follow_up:
            return category
    return None


@dataclass(frozen=True)
class Place:
    name: str
    address: str | None
    distance_meters: int
    latitude: float
    longitude: float

    @property
    def map_url(self) -> str:
        return (
            "https://www.google.com/maps/search/"
            f"?api=1&query={self.latitude:.6f}%2C{self.longitude:.6f}"
        )


class PlaceSearchUnavailable(RuntimeError):
    pass


class PlaceResults(list[Place]):
    """Keep source metadata alongside results, including empty search results."""

    def __init__(self, places=(), *, snapshot_updated_at: datetime | None = None):
        super().__init__(places)
        self.snapshot_updated_at = snapshot_updated_at

    def copy(self) -> PlaceResults:
        return PlaceResults(self, snapshot_updated_at=self.snapshot_updated_at)


def _float_setting(name: str, default: float, minimum: float, maximum: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser um número") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} deve estar entre {minimum} e {maximum}")
    return value


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser um número inteiro") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} deve estar entre {minimum} e {maximum}")
    return value


def _distance_meters(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> int:
    earth_radius = 6_371_000
    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)
    haversine = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    )
    return round(earth_radius * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine)))


class OverpassPlaceFinder:
    def __init__(self) -> None:
        self.center_lat = _float_setting(
            "NEIGHBORHOOD_CENTER_LAT", DEFAULT_CENTER_LAT, -90, 90
        )
        self.center_lon = _float_setting(
            "NEIGHBORHOOD_CENTER_LON", DEFAULT_CENTER_LON, -180, 180
        )
        self.radius_meters = _integer_setting(
            "NEIGHBORHOOD_SEARCH_RADIUS_METERS", DEFAULT_RADIUS_METERS, 100, 10_000
        )
        self.timeout_seconds = _integer_setting(
            "PLACE_SEARCH_TIMEOUT_SECONDS", 15, 2, 60
        )
        self.total_timeout_seconds = _integer_setting(
            "PLACE_SEARCH_TOTAL_TIMEOUT_SECONDS", 18, 2, 120
        )
        self.cache_ttl_seconds = _integer_setting(
            "PLACE_SEARCH_CACHE_TTL_SECONDS", 600, 0, 86_400
        )
        self.max_results = _integer_setting("PLACE_SEARCH_MAX_RESULTS", 5, 1, 10)
        self.overpass_url = os.getenv("OVERPASS_URL", DEFAULT_OVERPASS_URL)
        fallback_url = os.getenv("OVERPASS_FALLBACK_URL", DEFAULT_OVERPASS_FALLBACK_URL)
        self.endpoints = list(dict.fromkeys(
            url.strip() for url in (self.overpass_url, fallback_url) if url.strip()
        ))
        if not self.endpoints or not self.overpass_url.strip():
            raise RuntimeError("OVERPASS_URL deve usar HTTPS")
        for endpoint in self.endpoints:
            parsed_url = urllib.parse.urlsplit(endpoint)
            if parsed_url.scheme != "https" or not parsed_url.hostname:
                raise RuntimeError("Os endpoints Overpass devem usar HTTPS")
        self.snapshot_path = DEFAULT_SNAPSHOT_PATH
        self._online_retry_at = 0.0
        self._cache: dict[str, tuple[float, PlaceResults]] = {}
        self._cache_lock = threading.Lock()

    def _query(self, category: PlaceCategory, timeout_seconds: int | None = None) -> str:
        searches = "\n".join(
            f"nwr{osm_filter}(around:{self.radius_meters},{self.center_lat},{self.center_lon});"
            for osm_filter in category.filters
        )
        timeout_seconds = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        return f"[out:json][timeout:{timeout_seconds}];\n(\n{searches}\n);\nout center tags;"

    def _fetch(self, category: PlaceCategory) -> dict[str, Any]:
        try:
            # The synchronous chat handler runs in FastAPI's thread pool. An async
            # deadline also bounds DNS, streaming bodies and retry delays together.
            return asyncio.run(self._fetch_with_retries(category))
        except TimeoutError as exc:
            logger.warning(
                "Tempo total da busca no mapa esgotado",
                extra={"category": category.key, "timeout_seconds": self.total_timeout_seconds},
            )
            raise PlaceSearchUnavailable("Tempo total da busca no mapa esgotado") from exc

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        value = response.headers.get("Retry-After", "0")
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                return 0.0
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0

    async def _fetch_with_retries(self, category: PlaceCategory) -> dict[str, Any]:
        blocked_until: dict[str, float] = {}
        last_error: Exception | None = None
        attempt_timeout = min(
            self.timeout_seconds + 3,
            self.total_timeout_seconds / MAX_SEARCH_ATTEMPTS,
        )
        query_timeout = min(self.timeout_seconds, max(1, math.ceil(attempt_timeout) - 1))
        async with asyncio.timeout(self.total_timeout_seconds):
            async with httpx.AsyncClient(
                headers={"User-Agent": "AMOBEN-TCC/1.0 (OpenStreetMap place search)"},
                follow_redirects=True,
            ) as client:
                for attempt in range(MAX_SEARCH_ATTEMPTS):
                    if attempt:
                        await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                    candidates = self.endpoints[attempt % len(self.endpoints):] + self.endpoints[:attempt % len(self.endpoints)]
                    endpoint = next((
                        url for url in candidates
                        if blocked_until.get(url, 0) <= time.monotonic()
                    ), None)
                    if endpoint is None:
                        break
                    try:
                        async with asyncio.timeout(attempt_timeout):
                            async with client.stream(
                                "POST", endpoint,
                                data={"data": self._query(category, query_timeout)},
                                timeout=attempt_timeout,
                            ) as response:
                                response.raise_for_status()
                                raw_response = bytearray()
                                async for chunk in response.aiter_bytes():
                                    raw_response.extend(chunk)
                                    if len(raw_response) > 2_000_000:
                                        raise PlaceSearchUnavailable("Resposta do OpenStreetMap excedeu o limite")
                            try:
                                parsed = json.loads(raw_response)
                            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                                raise PlaceSearchUnavailable("Resposta inválida do OpenStreetMap") from exc
                            if not isinstance(parsed, dict) or not isinstance(parsed.get("elements"), list):
                                raise PlaceSearchUnavailable("Resposta inesperada do OpenStreetMap")
                            # Overpass may return HTTP 200 with partial results after
                            # a query timeout. Never cache those as a successful search.
                            if parsed.get("remark"):
                                raise PlaceSearchUnavailable("OpenStreetMap retornou resultados incompletos")
                        if attempt:
                            logger.info(
                                "Busca no mapa recuperada após nova tentativa",
                                extra={"category": category.key, "endpoint": urllib.parse.urlsplit(endpoint).hostname, "attempt": attempt + 1},
                            )
                        return parsed
                    except (httpx.HTTPError, TimeoutError, PlaceSearchUnavailable) as exc:
                        last_error = exc
                        status_code = None
                        retryable = True
                        if isinstance(exc, httpx.HTTPStatusError):
                            status_code = exc.response.status_code
                            retryable = status_code in {406, 408, 425, 429, 500, 502, 503, 504}
                            cooldown = self._retry_after(exc.response)
                            if status_code in {406, 429}:
                                cooldown = max(30.0, cooldown)
                            blocked_until[endpoint] = time.monotonic() + cooldown
                        logger.warning(
                            "Falha na consulta ao mapa",
                            extra={
                                "category": category.key,
                                "endpoint": urllib.parse.urlsplit(endpoint).hostname,
                                "attempt": attempt + 1,
                                "error_type": type(exc).__name__,
                                "error_reason": str(exc)[:300] or "Tempo limite da tentativa esgotado",
                                "http_status": status_code,
                                "retryable": retryable,
                            },
                        )
                        if not retryable:
                            break
        raise PlaceSearchUnavailable("OpenStreetMap indisponível após as tentativas") from last_error

    def _parse_places(self, payload: dict[str, Any]) -> list[Place]:
        places: list[Place] = []
        seen: set[tuple[str, int, int]] = set()
        for element in payload["elements"]:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags")
            if not isinstance(tags, dict):
                continue
            name = tags.get("name") or tags.get("brand") or tags.get("operator")
            if not isinstance(name, str) or not name.strip():
                continue

            center = element.get("center") if isinstance(element.get("center"), dict) else {}
            latitude = element.get("lat", center.get("lat"))
            longitude = element.get("lon", center.get("lon"))
            if not isinstance(latitude, (int, float)) or not isinstance(
                longitude, (int, float)
            ):
                continue

            suburb = tags.get("addr:suburb") or tags.get("addr:district")
            if isinstance(suburb, str) and normalize_text(suburb) != "estrada nova":
                continue

            distance = _distance_meters(
                self.center_lat, self.center_lon, float(latitude), float(longitude)
            )
            if distance > self.radius_meters:
                continue

            normalized_name = normalize_text(name)
            identity = (
                normalized_name,
                round(float(latitude) * 100_000),
                round(float(longitude) * 100_000),
            )
            if identity in seen:
                continue
            seen.add(identity)

            street = tags.get("addr:street")
            house_number = tags.get("addr:housenumber")
            address_parts = [
                part.strip()
                for part in (street, house_number)
                if isinstance(part, str) and part.strip()
            ]
            places.append(
                Place(
                    name=name.strip(),
                    address=", ".join(address_parts) or None,
                    distance_meters=distance,
                    latitude=float(latitude),
                    longitude=float(longitude),
                )
            )

        places.sort(key=lambda place: (place.distance_meters, place.name.casefold()))
        return places[: self.max_results]

    def _snapshot_results(self, category: PlaceCategory) -> PlaceResults | None:
        try:
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            updated_at = datetime.fromisoformat(snapshot["retrieved_at"])
            now = datetime.now(timezone.utc)
            if (
                snapshot["version"] != 1
                or updated_at.tzinfo is None
                or not timedelta(0) <= now - updated_at <= timedelta(days=30)
                or not math.isclose(snapshot["center_lat"], self.center_lat, abs_tol=1e-7)
                or not math.isclose(snapshot["center_lon"], self.center_lon, abs_tol=1e-7)
                or snapshot["radius_meters"] < self.radius_meters
                or not isinstance(snapshot["elements"], list)
            ):
                raise ValueError("Cópia local expirada ou incompatível com a área configurada")
            elements = [
                item for item in snapshot["elements"]
                if isinstance(item, dict)
                and isinstance(item.get("tags"), dict)
                and matches_category(item["tags"], category)
            ]
            result = PlaceResults(
                self._parse_places({"elements": elements}),
                snapshot_updated_at=updated_at,
            )
            logger.info(
                "Busca no mapa atendida pela cópia local",
                extra={
                    "category": category.key,
                    "snapshot_updated_at": updated_at.isoformat(),
                    "results_count": len(result),
                },
            )
            return result
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(
                "Cópia local do mapa indisponível",
                extra={"error_type": type(exc).__name__, "error_reason": str(exc)[:300]},
            )
            return None

    def search(self, category: PlaceCategory) -> PlaceResults:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(category.key)
            if cached and now - cached[0] <= self.cache_ttl_seconds:
                return cached[1].copy()
            use_snapshot = now < self._online_retry_at

        places = self._snapshot_results(category) if use_snapshot else None
        if places is None:
            try:
                places = PlaceResults(self._parse_places(self._fetch(category)))
            except PlaceSearchUnavailable:
                places = self._snapshot_results(category)
                if places is None:
                    raise
                # A short cooldown avoids waiting for the same unavailable
                # providers again when the user asks about another category.
                with self._cache_lock:
                    self._online_retry_at = time.monotonic() + 60
        with self._cache_lock:
            self._cache[category.key] = (time.monotonic(), places)
        return places.copy()


def format_place_results(category: PlaceCategory, places: list[Place]) -> str:
    snapshot_updated_at = getattr(places, "snapshot_updated_at", None)
    source_note = (
        "\n\nDados da cópia local do OpenStreetMap, consultados em "
        f"{snapshot_updated_at:%d/%m/%Y}. A consulta online está indisponível."
        if snapshot_updated_at is not None else ""
    )
    if not places:
        search_url = "https://www.google.com/maps/search/?" + urllib.parse.urlencode({
            "api": "1", "query": f"{category.plural} Estrada Nova Jaraguá do Sul SC",
        })
        return (
            f"Não encontrei registros de {category.plural} dentro da área de referência da "
            "Estrada Nova. O cadastro de locais pode estar incompleto, então isso "
            "não significa necessariamente que não existam opções."
            f"\n\nVocê também pode conferir no Google Maps:\n{search_url}"
            + source_note
        )

    lines = [
        f"Resultados para {category.plural} na área da Estrada Nova:",
        "",
    ]
    for index, place in enumerate(places, start=1):
        distance = (
            f"{place.distance_meters} m"
            if place.distance_meters < 1_000
            else f"{place.distance_meters / 1_000:.1f} km".replace(".", ",")
        )
        address = f" — {place.address}" if place.address else ""
        lines.extend(
            [
                f"{index}. {place.name}{address} (aprox. {distance} do centro do bairro)",
                place.map_url,
            ]
        )

    lines.extend(
        [
            "",
            "Os resultados são ordenados por proximidade, não por avaliação. "
            "Confirme o endereço e o horário antes de sair.",
        ]
    )
    return "\n".join(lines) + source_note
