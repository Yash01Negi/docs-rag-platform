"""Helpers for chat tests: SSE parsing and direct access to the chat service."""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings


@dataclass(frozen=True)
class SseEvent:
    id: int | None
    event: str
    data: dict[str, Any]


def parse_sse(body: str) -> list[SseEvent]:
    """Parse a complete text/event-stream body strictly (comments are skipped)."""
    events: list[SseEvent] = []
    assert body.endswith("\n\n"), "every SSE message must end with a blank line"
    for block in body.split("\n\n"):
        if not block:
            continue
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            name, _, value = line.partition(": ")
            assert name in {"id", "event", "data"}, f"unexpected SSE field {line!r}"
            assert name not in fields, f"repeated SSE field {name}"
            fields[name] = value
        if not fields:
            continue
        events.append(
            SseEvent(
                int(fields["id"]) if "id" in fields else None,
                fields["event"],
                json.loads(fields["data"]),
            )
        )
    return events


def names(events: list[SseEvent]) -> list[str]:
    collapsed: list[str] = []
    for event in events:
        if not (collapsed and collapsed[-1] == event.event == "delta"):
            collapsed.append(event.event)
    return collapsed


def answer_text(events: list[SseEvent]) -> str:
    return "".join(e.data["text"] for e in events if e.event == "delta")


def one(events: list[SseEvent], name: str) -> dict[str, Any]:
    matching = [e.data for e in events if e.event == name]
    assert len(matching) == 1, f"expected one {name} event, got {len(matching)}"
    return matching[0]


def create_conversation(client: TestClient, title: str | None = None) -> str:
    response = client.post("/api/v1/conversations", json={"title": title} if title else {})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def ask(client: TestClient, conversation_id: str, content: str, **fields: Any):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages", json={"content": content, **fields}
    )


def ask_events(
    client: TestClient, conversation_id: str, content: str, **fields: Any
) -> list[SseEvent]:
    response = ask(client, conversation_id, content, **fields)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


def query_rows(settings: Settings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            return list(connection.execute(text(sql), params).all())
    finally:
        engine.dispose()


def execute(settings: Settings, sql: str, **params: Any) -> None:
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            connection.execute(text(sql), params)
    finally:
        engine.dispose()


def new_conversation_row(settings: Settings) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    execute(
        settings,
        "INSERT INTO conversations (id, title) VALUES (:id, 'test')",
        id=conversation_id,
    )
    return conversation_id
