"""A real Agent Framework agent and AG-UI endpoint on the package's stores, with a fake chat client."""

from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from typing import Any

import httpx
import pytest
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from agent_framework_ag_ui import add_agent_framework_fastapi_endpoint
from fastapi import FastAPI

from agent_framework_community_postgres import PostgresPersistence

pytestmark = pytest.mark.integration


class NumberedReplies(BaseChatClient):
    """Answers ``reply<n>`` on the n-th call and records what the model was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[list[str]] = []

    def _reply(self, messages: Sequence[Message]) -> str:
        self.seen.append([f"{m.role}:{m.text}" for m in messages])
        return f"reply{len(self.seen)}"

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        text = self._reply(messages)
        if stream:

            async def updates() -> AsyncIterator[ChatResponseUpdate]:
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text(text)], message_id=text)

            return self._build_response_stream(updates())

        async def response() -> ChatResponse:
            return ChatResponse(messages=[Message(role="assistant", contents=[text])])

        return response()


async def test_an_agent_keeps_a_repeated_user_message_and_restores_the_history(
    persistence: PostgresPersistence,
) -> None:
    history = persistence.history_provider()
    agent = Agent(NumberedReplies(), "Be brief.", context_providers=[history])
    session = agent.create_session(session_id="s1")
    for text in ("yes", "tell me more", "yes"):
        await agent.run(text, session=session)
    turns = ["user:yes", "assistant:reply1", "user:tell me more", "assistant:reply2", "user:yes", "assistant:reply3"]
    assert [f"{m.role}:{m.text}" for m in await history.get_messages("s1")] == turns

    store = persistence.session_store()
    await store.set("s1", session)
    restored = await store.get("s1")
    assert restored is not None
    client = NumberedReplies()
    await Agent(client, "Be brief.", context_providers=[persistence.history_provider()]).run("again", session=restored)
    assert [line for line in client.seen[-1] if not line.startswith("system:")] == [*turns, "user:again"]


def _run_body(run_id: str, message_id: str, text: str) -> dict[str, Any]:
    message = {"id": message_id, "role": "user", "content": text}
    return {"threadId": "t1", "runId": run_id, "messages": [message], "state": {}, "tools": [], "context": []}


async def test_the_ag_ui_endpoint_stores_both_runs_in_the_thread_snapshot(persistence: PostgresPersistence) -> None:
    store = persistence.thread_snapshot_store()
    app = FastAPI()
    add_agent_framework_fastapi_endpoint(
        app,
        Agent(NumberedReplies(), "Be brief."),
        "/",
        snapshot_store=store,
        snapshot_scope_resolver=lambda request: "user-1",
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        for run_id, message_id, text in (("r1", "u1", "hi"), ("r2", "u2", "again")):
            response = await http.post("/", json=_run_body(run_id, message_id, text))
            assert response.status_code == 200
    snapshot = await store.get(scope="user-1", thread_id="t1")
    assert snapshot is not None
    assert [(m.get("role"), m.get("content")) for m in snapshot.messages] == [
        ("user", "hi"),
        ("assistant", "reply1"),
        ("user", "again"),
        ("assistant", "reply2"),
    ]
