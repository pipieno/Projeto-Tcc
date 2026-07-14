"""Search named places around the fixed OpenStreetMap center of Estrada Nova."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_CENTER_LAT = -26.4700649
DEFAULT_CENTER_LON = -49.1264936
DEFAULT_RADIUS_METERS = 1_800
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"


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
    )
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
        self.cache_ttl_seconds = _integer_setting(
            "PLACE_SEARCH_CACHE_TTL_SECONDS", 600, 0, 86_400
        )
        self.max_results = _integer_setting("PLACE_SEARCH_MAX_RESULTS", 5, 1, 10)
        self.overpass_url = os.getenv("OVERPASS_URL", DEFAULT_OVERPASS_URL)
        if not self.overpass_url.startswith("https://"):
            raise RuntimeError("OVERPASS_URL deve usar HTTPS")
        self._cache: dict[str, tuple[float, list[Place]]] = {}
        self._cache_lock = threading.Lock()

    def _query(self, category: PlaceCategory) -> str:
        searches = "\n".join(
            f"nwr{osm_filter}(around:{self.radius_meters},{self.center_lat},{self.center_lon});"
            for osm_filter in category.filters
        )
        return f"[out:json][timeout:{self.timeout_seconds}];\n(\n{searches}\n);\nout center tags;"

    def _fetch(self, category: PlaceCategory) -> dict[str, Any]:
        separator = "&" if "?" in self.overpass_url else "?"
        request_url = (
            f"{self.overpass_url}{separator}"
            f"{urllib.parse.urlencode({'data': self._query(category)})}"
        )
        request = urllib.request.Request(
            request_url,
            headers={
                "User-Agent": "AMOBEN-TCC/1.0 (OpenStreetMap place search)",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds + 3) as response:
                raw_response = response.read(2_000_001)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlaceSearchUnavailable("OpenStreetMap indisponível") from exc

        if len(raw_response) > 2_000_000:
            raise PlaceSearchUnavailable("Resposta do OpenStreetMap excedeu o limite")
        try:
            parsed = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlaceSearchUnavailable("Resposta inválida do OpenStreetMap") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("elements"), list):
            raise PlaceSearchUnavailable("Resposta inesperada do OpenStreetMap")
        return parsed

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

    def search(self, category: PlaceCategory) -> list[Place]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(category.key)
            if cached and now - cached[0] <= self.cache_ttl_seconds:
                return list(cached[1])

        places = self._parse_places(self._fetch(category))
        with self._cache_lock:
            self._cache[category.key] = (now, places)
        return list(places)


def format_place_results(category: PlaceCategory, places: list[Place]) -> str:
    if not places:
        return (
            f"Não encontrei {category.plural} dentro da área de referência da "
            "Estrada Nova. O cadastro de locais pode estar incompleto, então isso "
            "não significa necessariamente que não existam opções."
        )

    lines = [
        f"Encontrei estes {category.plural} na área da Estrada Nova:",
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
    return "\n".join(lines)
