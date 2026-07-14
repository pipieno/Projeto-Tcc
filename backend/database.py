"""Database configuration and persistence models for chat sessions."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    URL,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_id_id", "session_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    session: Mapped[ChatSession] = relationship(back_populates="messages")


def resolve_database_url() -> str | URL:
    """Resolve DATABASE_URL, DB_* MySQL settings, or a local SQLite fallback."""

    configured_url = os.getenv("DATABASE_URL")
    if configured_url:
        # SQLAlchemy 2 requires an explicit driver for the common mysql:// alias.
        if configured_url.startswith("mysql://"):
            return configured_url.replace("mysql://", "mysql+pymysql://", 1)
        return configured_url

    if os.getenv("DB_HOST"):
        try:
            port = int(os.getenv("DB_PORT", "3306"))
        except ValueError as exc:
            raise RuntimeError("DB_PORT deve ser um número inteiro") from exc

        return URL.create(
            drivername=os.getenv("DB_DRIVER", "mysql+pymysql"),
            username=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", ""),
            host=os.environ["DB_HOST"],
            port=port,
            database=os.getenv("DB_NAME", "agente"),
            query={"charset": "utf8mb4"},
        )

    sqlite_path = Path(__file__).resolve().with_name("agente.db")
    return f"sqlite+pysqlite:///{sqlite_path}"


class Database:
    """Own an engine and session factory, allowing isolated databases in tests."""

    def __init__(self, url: str | URL | None = None) -> None:
        self.url = url or resolve_database_url()
        parsed_url = make_url(str(self.url))
        engine_options: dict[str, object] = {"pool_pre_ping": True}

        if parsed_url.get_backend_name() == "sqlite":
            engine_options["connect_args"] = {"check_same_thread": False}
            if parsed_url.database in (None, "", ":memory:"):
                engine_options["poolclass"] = StaticPool

        self.engine: Engine = create_engine(self.url, **engine_options)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

        if parsed_url.get_backend_name() == "sqlite":
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def get_session(self) -> Generator[Session, None, None]:
        with self.session_factory() as database_session:
            yield database_session

    def dispose(self) -> None:
        self.engine.dispose()
