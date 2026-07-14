from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from backend.app import create_app, utc_now
from backend.database import ChatSession, Database, Message


class FakeChain:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, payload: dict):
        self.calls.append(payload)
        return SimpleNamespace(content=f"Resposta: {payload['input']}")


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def api():
    database = Database("sqlite+pysqlite:///:memory:")
    chain = FakeChain()
    application = create_app(
        database=database,
        chain=chain,
        session_ttl_hours=1,
    )
    transport = httpx.ASGITransport(app=application)
    try:
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                yield client, database, chain
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
    client, database, _chain = api
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
    client, _database, chain = api
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
    client, _database, chain = api
    created = await create_session(client)

    response = await client.post(
        "/chat",
        headers=authorization("token-incorreto"),
        json={"session_id": created["session_id"], "message": "Olá"},
    )

    assert response.status_code == 401
    assert not chain.calls


async def test_token_from_another_session_cannot_read_or_continue_chat(api):
    client, _database, chain = api
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
    client, _database, chain = api
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


async def test_expired_session_is_rejected_and_its_messages_are_removed(api):
    client, database, _chain = api
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
