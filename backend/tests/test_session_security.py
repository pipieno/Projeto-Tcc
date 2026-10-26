from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from backend.app import MODEL_NAME, REQUEST_COUNT, create_app, utc_now
from backend.database import ChatSession, Database, Message
from backend.places import Place, PlaceSearchUnavailable


class FakeChain:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, payload: dict):
        self.calls.append(payload)
        return SimpleNamespace(content=f"Resposta: {payload['input']}")


class FakePlaceFinder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, category):
        self.calls.append(category.key)
        if category.key == "pharmacy":
            return [
                Place(
                    name="Farmácia da Estrada",
                    address="Rua José Picolli, 150",
                    distance_meters=400,
                    latitude=-26.4702,
                    longitude=-49.1266,
                )
            ]
        if category.key == "hospital":
            return [
                Place(
                    name="Hospital da Estrada",
                    address="Rua José Picolli, 200",
                    distance_meters=500,
                    latitude=-26.471,
                    longitude=-49.127,
                )
            ]
        if category.key == "bank":
            return [
                Place(
                    name="Banco da Estrada",
                    address="Rua José Picolli, 300",
                    distance_meters=600,
                    latitude=-26.472,
                    longitude=-49.128,
                )
            ]
        return [
            Place(
                name="Café da Estrada",
                address="Rua José Picolli, 100",
                distance_meters=350,
                latitude=-26.4701,
                longitude=-49.1265,
            )
        ]


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def api():
    database = Database("sqlite+pysqlite:///:memory:")
    chain = FakeChain()
    place_finder = FakePlaceFinder()
    application = create_app(
        database=database,
        chain=chain,
        place_finder=place_finder,
        session_ttl_hours=1,
    )
    transport = httpx.ASGITransport(app=application)
    try:
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                yield client, database, chain, place_finder
    finally:
        database.dispose()


async def create_session(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/sessions")
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    parsed_id = uuid.UUID(body["session_id"])
    assert parsed_id.version == 4
    assert body["session_token"]
    assert body["expires_at"]
    return body


def authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_session_token_is_hashed_before_persistence(api):
    client, database, _chain, _place_finder = api
    created = await create_session(client)

    with database.session_factory() as database_session:
        stored = database_session.get(ChatSession, created["session_id"])
        assert stored is not None
        assert stored.token_hash != created["session_token"]
        assert stored.token_hash == hashlib.sha256(
            created["session_token"].encode("utf-8")
        ).hexdigest()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/chat"),
        ("get", "/export/{session_id}"),
    ],
)
async def test_protected_endpoints_reject_missing_token(api, method, path):
    client, _database, chain, _place_finder = api
    created = await create_session(client)
    url = path.format(session_id=created["session_id"])

    if method == "post":
        response = await client.post(
            url,
            json={"session_id": created["session_id"], "message": "Olá"},
        )
    else:
        response = await client.get(url)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert not chain.calls


async def test_wrong_token_is_rejected(api):
    client, _database, chain, _place_finder = api
    created = await create_session(client)

    response = await client.post(
        "/chat",
        headers=authorization("token-incorreto"),
        json={"session_id": created["session_id"], "message": "Olá"},
    )

    assert response.status_code == 401
    assert not chain.calls


async def test_token_from_another_session_cannot_read_or_continue_chat(api):
    client, _database, chain, _place_finder = api
    first = await create_session(client)
    second = await create_session(client)

    wrong_headers = authorization(second["session_token"])
    chat_response = await client.post(
        "/chat",
        headers=wrong_headers,
        json={"session_id": first["session_id"], "message": "Olá"},
    )
    export_response = await client.get(
        f"/export/{first['session_id']}",
        headers=wrong_headers,
    )

    assert chat_response.status_code == 401
    assert export_response.status_code == 401
    assert not chain.calls


async def test_valid_token_persists_history_and_allows_export(api):
    client, _database, chain, _place_finder = api
    created = await create_session(client)
    headers = authorization(created["session_token"])

    first_response = await client.post(
        "/chat",
        headers=headers,
        json={"session_id": created["session_id"], "message": "Primeira"},
    )
    second_response = await client.post(
        "/chat",
        headers=headers,
        json={"session_id": created["session_id"], "message": "Segunda"},
    )
    export_response = await client.get(
        f"/export/{created['session_id']}",
        headers=headers,
    )

    assert first_response.status_code == 200
    assert first_response.headers["cache-control"] == "no-store"
    assert first_response.json() == {"response": "Resposta: Primeira"}
    assert second_response.status_code == 200
    assert second_response.json() == {"response": "Resposta: Segunda"}
    assert len(chain.calls[0]["history"]) == 0
    assert [message.content for message in chain.calls[1]["history"]] == [
        "Primeira",
        "Resposta: Primeira",
    ]

    assert export_response.status_code == 200
    assert export_response.headers["cache-control"] == "no-store"
    exported = export_response.json()
    assert exported["session_id"] == created["session_id"]
    assert exported["total_messages"] == 4
    assert [message["role"] for message in exported["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


async def test_last_question_on_new_session_does_not_invent_history(api):
    client, _database, chain, _place_finder = api
    created = await create_session(client)

    response = await client.post(
        "/chat",
        headers=authorization(created["session_token"]),
        json={
            "session_id": created["session_id"],
            "message": "Qual foi a minha última pergunta?",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "response": (
            "Esta é a sua primeira pergunta nesta conversa; "
            "ainda não há uma pergunta anterior."
        )
    }
    assert not chain.calls


async def test_last_question_is_returned_from_persisted_history(api):
    client, _database, chain, _place_finder = api
    created = await create_session(client)
    headers = authorization(created["session_token"])

    await client.post(
        "/chat",
        headers=headers,
        json={
            "session_id": created["session_id"],
            "message": "Como faço para me associar?",
        },
    )
    response = await client.post(
        "/chat",
        headers=headers,
        json={
            "session_id": created["session_id"],
            "message": "qual foi a ultima pergunta q eu mandei",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "response": 'Sua última pergunta foi: "Como faço para me associar?"'
    }
    assert len(chain.calls) == 1


async def test_local_business_search_uses_fixed_neighborhood_area(api):
    client, _database, chain, place_finder = api
    created = await create_session(client)

    response = await client.post(
        "/chat",
        headers=authorization(created["session_token"]),
        json={
            "session_id": created["session_id"],
            "message": "Quais são os bons cafés na Estrada Nova?",
        },
    )

    assert response.status_code == 200
    content = response.json()["response"]
    assert "Café da Estrada" in content
    assert "Rua José Picolli, 100" in content
    assert "https://www.google.com/maps/search/" in content
    assert place_finder.calls == ["cafe"]
    assert not chain.calls


async def test_food_then_send_pharmacies_uses_map_for_both_messages(api):
    client, _database, chain, place_finder = api
    created = await create_session(client)
    headers = authorization(created["session_token"])
    for message in ["onde posso comer na estrada nova?", "me manda as farmacias"]:
        response = await client.post(
            "/chat",
            headers=headers,
            json={"session_id": created["session_id"], "message": message},
        )
        assert response.status_code == 200

    assert "Farmácia da Estrada" in response.json()["response"]
    assert "https://www.google.com/maps/search/" in response.json()["response"]
    assert place_finder.calls == ["food", "pharmacy"]
    assert not chain.calls


async def test_map_outage_keeps_chat_response_and_counts_as_error(api, monkeypatch):
    client, _database, chain, place_finder = api
    created = await create_session(client)

    def unavailable(_category):
        raise PlaceSearchUnavailable("OpenStreetMap indisponível")

    monkeypatch.setattr(place_finder, "search", unavailable)
    error_count = REQUEST_COUNT.labels(model=MODEL_NAME, status="error")._value.get()
    ok_count = REQUEST_COUNT.labels(model=MODEL_NAME, status="ok")._value.get()
    response = await client.post(
        "/chat",
        headers=authorization(created["session_token"]),
        json={"session_id": created["session_id"], "message": "Onde fica a farmácia?"},
    )

    assert response.status_code == 200
    assert response.json()["response"] == (
        "Não consegui consultar o mapa da Estrada Nova agora. "
        "Tente novamente em alguns instantes."
    )
    assert REQUEST_COUNT.labels(model=MODEL_NAME, status="error")._value.get() == error_count + 1
    assert REQUEST_COUNT.labels(model=MODEL_NAME, status="ok")._value.get() == ok_count
    assert not chain.calls


async def test_category_word_without_location_intent_stays_with_model(api):
    client, _database, chain, place_finder = api
    created = await create_session(client)

    response = await client.post(
        "/chat",
        headers=authorization(created["session_token"]),
        json={
            "session_id": created["session_id"],
            "message": "A AMOBEN serve café nas reuniões?",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "response": "Resposta: A AMOBEN serve café nas reuniões?"
    }
    assert len(chain.calls) == 1
    assert not place_finder.calls


async def test_hospital_in_this_area_is_treated_as_neighborhood_search(api):
    client, _database, chain, place_finder = api
    created = await create_session(client)

    response = await client.post(
        "/chat",
        headers=authorization(created["session_token"]),
        json={
            "session_id": created["session_id"],
            "message": "Tem algum hospital nesta área?",
        },
    )

    assert response.status_code == 200
    assert "Hospital da Estrada" in response.json()["response"]
    assert place_finder.calls == ["hospital"]
    assert not chain.calls


@pytest.mark.parametrize(
    "message",
    [
        "e bancos?",
        "então quero conhecer bancos na Estrada Nov",
    ],
)
async def test_bank_follow_up_and_neighborhood_typo_are_understood(api, message):
    client, _database, chain, place_finder = api
    created = await create_session(client)

    response = await client.post(
        "/chat",
        headers=authorization(created["session_token"]),
        json={"session_id": created["session_id"], "message": message},
    )

    assert response.status_code == 200
    assert "Banco da Estrada" in response.json()["response"]
    assert place_finder.calls == ["bank"]
    assert not chain.calls


async def test_expired_session_is_rejected_and_its_messages_are_removed(api):
    client, database, _chain, _place_finder = api
    created = await create_session(client)
    headers = authorization(created["session_token"])
    chat_response = await client.post(
        "/chat",
        headers=headers,
        json={"session_id": created["session_id"], "message": "Olá"},
    )
    assert chat_response.status_code == 200

    with database.session_factory() as database_session:
        stored = database_session.get(ChatSession, created["session_id"])
        assert stored is not None
        stored.expires_at = utc_now() - timedelta(seconds=1)
        database_session.commit()

    export_response = await client.get(
        f"/export/{created['session_id']}",
        headers=headers,
    )
    assert export_response.status_code == 401

    with database.session_factory() as database_session:
        assert database_session.get(ChatSession, created["session_id"]) is None
        message_count = database_session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == created["session_id"])
        )
        assert message_count == 0
