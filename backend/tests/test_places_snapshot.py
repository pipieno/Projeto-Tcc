from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.places import (
    OverpassPlaceFinder,
    PlaceSearchUnavailable,
    detect_place_category,
    format_place_results,
)
from backend.refresh_places_snapshot import matches_category


@pytest.fixture
def finder(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACE_SEARCH_CACHE_TTL_SECONDS", "600")
    finder = OverpassPlaceFinder()
    finder.snapshot_path = tmp_path / "snapshot.json"
    snapshot = {
        "version": 1,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "center_lat": finder.center_lat,
        "center_lon": finder.center_lon,
        "radius_meters": finder.radius_meters,
        "elements": [
            {"lat": finder.center_lat, "lon": finder.center_lon,
             "tags": {"shop": "bakery", "name": "Padaria cadastrada"}},
            {"lat": finder.center_lat, "lon": finder.center_lon,
             "tags": {"amenity": "pharmacy", "name": "Farmácia cadastrada"}},
            {"lat": finder.center_lat, "lon": finder.center_lon,
             "tags": {"shop": "bakery", "name": "Outro bairro", "addr:suburb": "Outro bairro"}},
        ],
    }
    finder.snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return finder


def category(name="padaria"):
    return detect_place_category(f"onde fica a {name}")


def test_online_outage_uses_local_data_and_keeps_date_in_cache(finder, monkeypatch):
    calls = []

    def unavailable(_category):
        calls.append(_category.key)
        raise PlaceSearchUnavailable("provedores indisponíveis")

    monkeypatch.setattr(finder, "_fetch", unavailable)
    result = finder.search(category())
    assert [place.name for place in result] == ["Padaria cadastrada"]
    content = format_place_results(category(), result)
    assert "cópia local do OpenStreetMap" in content
    assert result.snapshot_updated_at.strftime("%d/%m/%Y") in content
    assert "https://www.google.com/maps/search/" in content
    cached = finder.search(category())
    assert cached.snapshot_updated_at == result.snapshot_updated_at
    assert calls == ["bakery"]

    # Another category uses the same local extract during the provider cooldown.
    assert finder.search(category("farmacia"))[0].name == "Farmácia cadastrada"
    assert calls == ["bakery"]


@pytest.mark.parametrize("change", [
    {"retrieved_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()},
    {"center_lat": 0},
    {"radius_meters": 100},
    {"version": 99},
])
def test_expired_or_incompatible_snapshot_does_not_supply_places(finder, monkeypatch, change):
    snapshot = json.loads(finder.snapshot_path.read_text(encoding="utf-8"))
    snapshot.update(change)
    finder.snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    def unavailable(_category):
        raise PlaceSearchUnavailable("falha original")

    monkeypatch.setattr(finder, "_fetch", unavailable)
    with pytest.raises(PlaceSearchUnavailable, match="falha original"):
        finder.search(category())


def test_missing_category_keeps_snapshot_date_and_offers_manual_search(finder, monkeypatch):
    def unavailable(_category):
        raise PlaceSearchUnavailable()

    monkeypatch.setattr(finder, "_fetch", unavailable)
    result = finder.search(category("hospital"))
    assert result == []
    content = format_place_results(category("hospital"), result)
    assert "Não encontrei registros" in content
    assert "cópia local" in content
    assert "Google Maps" in content


def test_successful_online_search_takes_precedence_over_snapshot(finder, monkeypatch):
    monkeypatch.setattr(finder, "_fetch", lambda _category: {"elements": []})
    result = finder.search(category())
    assert result == []
    assert result.snapshot_updated_at is None


def test_corrupt_snapshot_preserves_original_failure(finder, monkeypatch):
    finder.snapshot_path.write_text("broken JSON", encoding="utf-8")

    def unavailable(_category):
        raise PlaceSearchUnavailable("falha original")

    monkeypatch.setattr(finder, "_fetch", unavailable)
    with pytest.raises(PlaceSearchUnavailable, match="falha original"):
        finder.search(category())


def test_local_category_match_supports_regex_filters():
    assert matches_category({"amenity": "restaurant"}, category("restaurante"))
    assert not matches_category({"shop": "bakery"}, category("farmacia"))
