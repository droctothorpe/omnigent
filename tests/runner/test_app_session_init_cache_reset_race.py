"""Session init must not reinstate a spec entry retired by a concurrent reset.

``POST /v1/sessions`` resolves the agent spec early and memoizes it into the
session-keyed spec cache only at the very end of init. An agent-cache reset
landing in between (``POST /v1/sessions/{id}/agent-cache/reset`` — issued by
the server when the user edits the session's agent or its MCP servers) pops
the entry and bumps the session's cache generation; init must not write the
superseded entry back, or every later spec read on the session serves the
pre-reset bundle (instructions, MCP servers, local tools, bundle workdir)
until the next invalidation.

Drives the runner app over real HTTP (ASGI): init is held open inside a slow
agent-spec resolution (the way a slow bundle download holds it open), the
reset is acknowledged mid-init, then a spec-derived read
(``GET /v1/sessions/{id}/skills``) must reflect the post-reset spec.

Usage::

    pytest tests/runner/test_app_session_init_cache_reset_race.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, SkillSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient

_SESSION_ID = "cacheinit_5f0f70bd0e9f4d92a51d8bd4a3a1c9d7"
_AGENT_ID = "agentinit_1febd0be51c94b309a6f9f19c1f6f8aa"


class _SessionSnapshotServerClient(NullServerClient):
    """Server stub whose ``GET /v1/sessions/{id}`` names the bound agent.

    The post-reset spec re-resolution reads the session snapshot to find the
    agent id; the null parent's empty body would abort that read before the
    resolver is ever consulted.
    """

    class _SnapshotResponse(NullServerClient._Response):
        """Stub 200 carrying the session→agent binding."""

        def json(self) -> dict[str, Any]:
            """Return the minimal session snapshot body."""
            return {
                "id": _SESSION_ID,
                "agent_id": _AGENT_ID,
                "created_at": 1234,
                "workspace": None,
            }

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Serve the session snapshot; defer everything else to the null parent.

        :param url: Request path, e.g. ``"/v1/sessions/<id>"``.
        :param kwargs: Extra keyword arguments (forwarded to the parent).
        :returns: Snapshot response for the session URL, empty 200 otherwise.
        """
        if url.split("?", 1)[0] == f"/v1/sessions/{_SESSION_ID}":
            return self._SnapshotResponse()
        return await super().get(url, **kwargs)


def _spec(version: str) -> AgentSpec:
    """Build an agent spec carrying a version-marked, user-invocable skill.

    ``skills_filter="none"`` suppresses host-skill discovery so the skills
    read is exactly the bundled set — hermetic, independent of the dev's
    real ``~/.claude/skills/``.

    :param version: Marker distinguishing the pre-reset ("v1") from the
        post-reset ("v2") spec.
    :returns: The stub agent spec.
    """
    return AgentSpec(
        spec_version=1,
        name=f"cache-reset-race-{version}",
        instructions=f"instructions {version}",
        skills=[
            SkillSpec(
                name=f"marker-{version}",
                description=f"sentinel skill from the {version} bundle",
                content="noop",
            )
        ],
        skills_filter="none",
    )


@pytest.mark.asyncio
async def test_session_init_memoizes_spec_when_no_reset_intervenes() -> None:
    """Absent a reset, init's spec-cache write must stand (no over-fencing).

    Guards the fence added for the reset race: a session that saw no
    invalidation during init must serve later spec reads from the entry init
    memoized, without re-consulting the resolver.
    """
    resolver_calls = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolver_calls
        del agent_id, session_id
        resolver_calls += 1
        return _spec("v1" if resolver_calls == 1 else "v2")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionSnapshotServerClient(),  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )

    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
        )
        assert init_resp.status_code == 201, init_resp.text
        skills_resp = await client.get(f"/v1/sessions/{_SESSION_ID}/skills")

    assert skills_resp.status_code == 200, skills_resp.text
    names = {skill["name"] for skill in skills_resp.json()["skills"]}
    assert resolver_calls == 1, (
        f"spec read after an uninterrupted init re-consulted the resolver "
        f"({resolver_calls} calls): init's memoization was wrongly suppressed"
    )
    assert names == {"marker-v1"}, f"init's resolved spec not served; skills = {sorted(names)}"


@pytest.mark.asyncio
async def test_session_init_does_not_reinstate_spec_superseded_by_reset() -> None:
    """A reset acknowledged during init wins over init's spec-cache write.

    Steps (all over the runner's HTTP API):

    1. ``POST /v1/sessions`` — init starts; agent-spec resolution is slow
       (the resolver blocks, holding init open).
    2. ``POST /v1/sessions/{id}/agent-cache/reset`` — the invalidation the
       server issues when the user edits the session's agent mid-init.
       Returns 200 with the entry dropped and the generation bumped.
    3. Resolution completes with the pre-reset ("v1") spec; init finishes.
    4. ``GET /v1/sessions/{id}/skills`` — a spec-derived read. The reset
       retired the v1 entry, so the read must re-resolve and serve the
       post-reset ("v2") spec, not the superseded entry init memoized.
    """
    resolver_entered = asyncio.Event()
    release_resolver = asyncio.Event()
    resolver_calls = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolver_calls
        del agent_id, session_id
        resolver_calls += 1
        if resolver_calls == 1:
            resolver_entered.set()
            await release_resolver.wait()
            return _spec("v1")
        return _spec("v2")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionSnapshotServerClient(),  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )

    async with _runner_client(app) as client:
        init_task = asyncio.create_task(
            client.post(
                "/v1/sessions",
                json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
            )
        )
        await asyncio.wait_for(resolver_entered.wait(), timeout=10)

        reset_resp = await client.post(
            f"/v1/sessions/{_SESSION_ID}/agent-cache/reset",
            json={"agent_id": _AGENT_ID},
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json()["reset"] is True

        release_resolver.set()
        init_resp = await asyncio.wait_for(init_task, timeout=30)
        assert init_resp.status_code == 201, init_resp.text

        skills_resp = await client.get(f"/v1/sessions/{_SESSION_ID}/skills")

    assert skills_resp.status_code == 200, skills_resp.text
    names = {skill["name"] for skill in skills_resp.json()["skills"]}

    # The acknowledged reset retired the v1 entry: the next spec read must
    # consult the resolver again rather than be served init's stale write.
    assert resolver_calls == 2, (
        "spec read after an acknowledged agent-cache reset was served from the "
        "session spec cache: session init reinstated the superseded entry "
        f"(resolver consulted {resolver_calls} time(s), expected 2)"
    )
    assert "marker-v2" in names, f"post-reset spec not served; skills = {sorted(names)}"
    assert "marker-v1" not in names, (
        f"superseded pre-reset spec still served after reset; skills = {sorted(names)}"
    )
