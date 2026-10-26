"""Download a small, reusable OSM extract for the fixed neighborhood.

Run manually with `python -m backend.refresh_places_snapshot`. This is a one-off
download, never a request made by the chat against the OSM editing API.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .places import CATEGORIES, OverpassPlaceFinder, _distance_meters, matches_category


SNAPSHOT_PATH = Path(__file__).with_name("data") / "estrada_nova_places.json"
TAG_KEYS = {
    "name", "brand", "operator", "amenity", "shop", "healthcare",
    "addr:street", "addr:housenumber", "addr:suburb", "addr:district",
}


def download_tile(client: httpx.Client, bbox: tuple, depth: int = 0) -> list[dict]:
    response = client.get(
        "https://api.openstreetmap.org/api/0.6/map.json",
        params={"bbox": ",".join(f"{value:.7f}" for value in bbox)},
    )
    # Dense tiles can exceed OSM's 50,000-node limit. Split only those tiles;
    # do not repeatedly send the same rejected download.
    if response.status_code == 400 and "too many nodes" in response.text.lower() and depth < 2:
        left, bottom, right, top = bbox
        middle_x, middle_y = (left + right) / 2, (bottom + top) / 2
        elements = []
        for tile in (
            (left, bottom, middle_x, middle_y),
            (middle_x, bottom, right, middle_y),
            (left, middle_y, middle_x, top),
            (middle_x, middle_y, right, top),
        ):
            elements.extend(download_tile(client, tile, depth + 1))
        return elements
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get("elements"), list):
        raise RuntimeError("O OpenStreetMap retornou um extrato inválido")
    return payload["elements"]


def build_snapshot(finder: OverpassPlaceFinder) -> dict:
    lat, lon = finder.center_lat, finder.center_lon
    lat_offset = finder.radius_meters / 110_000
    lon_offset = finder.radius_meters / (110_000 * math.cos(math.radians(lat)))
    elements = {}
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "AMOBEN-TCC/1.0 (one-off neighborhood extract)"},
    ) as client:
        for left, right in ((lon - lon_offset, lon), (lon, lon + lon_offset)):
            for bottom, top in ((lat - lat_offset, lat), (lat, lat + lat_offset)):
                for element in download_tile(client, (left, bottom, right, top)):
                    elements[(element["type"], element["id"])] = element

    coordinates = {
        key: (element["lat"], element["lon"])
        for key, element in elements.items() if key[0] == "node"
    }
    for key, element in elements.items():
        if key[0] != "way":
            continue
        points = [coordinates[("node", ref)] for ref in element.get("nodes", []) if ("node", ref) in coordinates]
        if points:
            latitudes, longitudes = zip(*points)
            coordinates[key] = (
                (min(latitudes) + max(latitudes)) / 2,
                (min(longitudes) + max(longitudes)) / 2,
            )

    selected = []
    for key, element in elements.items():
        tags = element.get("tags", {})
        if key not in coordinates or not (tags.get("name") or tags.get("brand") or tags.get("operator")):
            continue
        if not any(matches_category(tags, category) for _, category in CATEGORIES):
            continue
        latitude, longitude = coordinates[key]
        if _distance_meters(lat, lon, latitude, longitude) > finder.radius_meters:
            continue
        selected.append({
            "type": key[0], "id": key[1],
            "lat": latitude, "lon": longitude,
            "tags": {name: value for name, value in tags.items() if name in TAG_KEYS},
        })
    return {
        "version": 1,
        "source": "https://www.openstreetmap.org",
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "license_url": "https://www.openstreetmap.org/copyright",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "center_lat": lat, "center_lon": lon,
        "radius_meters": finder.radius_meters,
        "elements": sorted(selected, key=lambda item: (item["type"], item["id"])),
    }


def main() -> None:
    finder = OverpassPlaceFinder()
    snapshot = build_snapshot(finder)
    if not snapshot["elements"]:
        raise RuntimeError("Extrato sem estabelecimentos; a cópia anterior foi preservada")
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SNAPSHOT_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SNAPSHOT_PATH)
    print(f"Cópia atualizada: {SNAPSHOT_PATH} ({len(snapshot['elements'])} estabelecimentos)")
    for _, category in CATEGORIES:
        count = sum(matches_category(item["tags"], category) for item in snapshot["elements"])
        print(f"  {category.key}: {count}")


if __name__ == "__main__":
    main()
