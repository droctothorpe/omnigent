"""E2E: the "Random worktree" default must survive Send racing the git-ness probe.

With "Always use a random worktree" enabled in BOTH Settings > Git (the
user-global default) and the project's settings (``use_worktree: true``), a
``?project=`` visit seeds host + workspace from the stored config, then the
composer probes ``GET /v1/hosts/{id}/worktrees`` to learn the workspace is a
git repo before auto-naming a worktree branch (the seed effect in
``web/src/shell/NewChatDialog.tsx`` waits on that probe). Nothing gates Send
on the probe, so on a slow host connection the user can submit the first
message while the probe is still in flight - the branch field is empty and
the create can carry no explicit git decision.

The fix makes the SERVER authoritative for the project's worktree default:
``resolve_project_session_create`` materializes a generated
``worktree-<hex8>`` branch whenever a ``project_id`` create OMITS ``git``
(field absent), and an explicit ``git: null`` is a settled opt-out. That
server half is proven end-to-end in
``tests/server/integration/test_session_worktree_create.py``; these tests pin
the composer's half of the wire contract:

- Send during the race must leave ``git`` ABSENT (delegating the default to
  the server) - pinning ``null`` here would silently opt the session out of
  the worktree the toggles promise.
- Clearing the seeded branch after the probe settles is a resolved decision
  and must pin an explicit ``git: null`` so the server does not re-apply the
  default the user just declined.

Heavy ``page.route`` stubbing mirrors ``test_project_config_prefill.py`` and
is required for the same reason: the e2e_ui harness's tunneled runner
registers no *host*, so ``/v1/hosts``, ``/v1/agents``, the project config,
the worktrees probe, and the create ``POST`` are faked (the POST handler
captures the body - the thing under test - and returns a real seeded session
id so post-send navigation lands somewhere real).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e_wtdefault"
_PROJECT_ID = "proj_e2e_wtdefault"
_PROJECT_NAME = "WorktreeRaceProject"
# A git repo whose main tree is the configured workspace - the probe's
# (eventual) answer says "this is a git repo", which is what arms the seed.
_GIT_REPO = "/work/omnigent"
# The user-global "always use a worktree" preference key (Settings > Git),
# mirrors STORAGE_KEY in web/src/lib/worktreeDefaultPreferences.ts.
_ALWAYS_WORKTREE_KEY = "omnigent:always-use-worktree"
# Bare create endpoint (POST captured); NOT the /{id}/... sub-routes.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
# One project config endpoint: /v1/projects/<id> (not the bare list).
_PROJECT_CFG_RE = re.compile(r"/v1/projects/[^/?]+")
# The worktree-list endpoint the composer probes for the seeded workspace.
_WORKTREES_RE = re.compile(r"/v1/hosts/[^/]+/worktrees")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own loop (see test_start_session)."""
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _hosts_body() -> str:
    return json.dumps(
        {"hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]}
    )


def _agents_body() -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                },
            ]
        }
    )


def _projects_list_body() -> str:
    """``GET /v1/sessions/projects`` - resolves the ?project= name to an id."""
    return json.dumps([{"id": _PROJECT_ID, "name": _PROJECT_NAME}])


def _project_config_body() -> str:
    """``GET /v1/projects/{id}`` - host + git workspace + the per-project
    Random-worktree toggle ON (the ticket has it on in BOTH places)."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": _HOST_ID, "workspace": _GIT_REPO, "use_worktree": True},
        }
    )


def _git_repo_worktrees_body() -> str:
    """``GET /v1/hosts/{id}/worktrees`` - a plain git repo (one main tree)."""
    return json.dumps(
        {
            "object": "list",
            "data": [
                {"path": _GIT_REPO, "branch": "main", "is_main": True, "detached": False},
            ],
        }
    )


class _StubState:
    """Mutable capture shared between the route stubs and the assertions."""

    def __init__(self) -> None:
        self.create_bodies: list[dict[str, Any]] = []
        self.probe_requests: list[str] = []
        # Held probe: models the slow host whose worktree probe is still in
        # flight when the user hits Send. Pre-set for the settled scenarios.
        self.probe_release = asyncio.Event()


async def _install_stubs(page: Any, session_id: str, state: _StubState) -> None:
    """Fake the host/agent/project/create endpoints (no host in this harness).

    The create ``POST`` body is the observable; it fulfills with a real seeded
    session id so post-send navigation lands somewhere real. The worktrees
    probe answers only once ``state.probe_release`` is set.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_projects_list(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_projects_list_body()
        )

    async def handle_project_config(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_project_config_body()
        )

    async def handle_worktrees(route: Route) -> None:
        state.probe_requests.append(route.request.url)
        await state.probe_release.wait()
        await route.fulfill(
            status=200, content_type="application/json", body=_git_repo_worktrees_body()
        )

    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            state.create_bodies.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": session_id}),
            )
        else:
            await route.continue_()

    # Neutralize the agent-discovery scan so only the stubbed catalog
    # feeds the picker (a leftover native agent would rank ahead).
    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/projects", handle_projects_list)
    await page.route(_PROJECT_CFG_RE, handle_project_config)
    await page.route(_WORKTREES_RE, handle_worktrees)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

    # Random worktree ON in the main settings too (Settings > Git).
    await page.add_init_script(
        f"""window.localStorage.setItem("{_ALWAYS_WORKTREE_KEY}", "true");"""
    )


def test_send_during_probe_delegates_worktree_default_to_server(
    seeded_session: tuple[str, str],
) -> None:
    """Send before the git-ness probe resolves must OMIT ``git`` entirely.

    Random worktree is on globally AND on the project. The worktrees probe is
    held in flight (slow host) while the user types the first message and hits
    Send. The composer has no settled decision, so the create must leave the
    ``git`` field ABSENT - the presence-based project default fill on the
    server is what turns that omission into the promised ``worktree-<hex8>``
    branch. Pinning ``git: null`` here instead would read as a settled opt-out
    and silently drop the worktree (the original bug, one layer down).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_probe_race(base_url, session_id))


async def _drive_probe_race(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            state = _StubState()
            await _install_stubs(page, session_id, state)

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The project prefill settles: the workspace chip shows the repo.
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "omnigent", timeout=15_000
            )
            # The git-ness probe is now in flight and held - the branch chip
            # still shows the empty placeholder, no auto-seeded name yet.
            await _wait_until(lambda: len(state.probe_requests) > 0)
            await expect(page.get_by_test_id("new-chat-landing-branch-chip")).to_contain_text(
                "Worktree"
            )
            # A beat on the settled composer (both toggles on, probe loading)
            # so a recording of this journey is readable.
            await asyncio.sleep(1.0)

            # The user doesn't wait for a probe they can't see: type the first
            # message and hit Send while it is still loading. Typed key by key
            # (as a user would) - the probe stays in flight the whole time.
            await page.get_by_test_id("new-chat-landing-input").press_sequentially(
                "please look into the flaky login test", delay=40
            )
            await asyncio.sleep(0.5)
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The create fires while the probe is STILL HELD - captured here,
            # before the release below, which is what makes this a race test.
            await _wait_until(lambda: len(state.create_bodies) >= 1, timeout_s=10.0)
            state.probe_release.set()

            # Let the post-send state render (recording ends on the outcome).
            await asyncio.sleep(1.5)

            body = state.create_bodies[0]
            assert body["project_id"] == _PROJECT_ID, body
            assert "git" not in body, (
                "Send outraced the git-ness probe, so the composer has no "
                "settled worktree decision: the create must OMIT `git` so the "
                "server materializes the project's use_worktree default - "
                f"got: {body}"
            )
        finally:
            # Close the context before the browser so a recording (if the
            # harness injected record_video_dir) is flushed to disk even when
            # an assertion above failed.
            await page.context.close()
            await browser.close()


def test_clearing_the_seeded_branch_pins_an_explicit_opt_out(
    seeded_session: tuple[str, str],
) -> None:
    """Clearing the auto-seeded branch must send an explicit ``git: null``.

    Once the probe settles and the seed fills the branch chip, the user
    emptying that field is a resolved "no worktree for this session" choice.
    The create must pin ``git: null`` - a bare omission would let the server
    re-apply the project default the user just declined.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_declined_seed(base_url, session_id))


async def _drive_declined_seed(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            state = _StubState()
            state.probe_release.set()  # fast host: the probe settles at once
            await _install_stubs(page, session_id, state)

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # The settled probe seeds the worktree default into the chip.
            await expect(page.get_by_test_id("new-chat-landing-branch-chip")).to_contain_text(
                re.compile(r"worktree-[0-9a-f]{8}"), timeout=15_000
            )

            # The user opts out for this session: open the chip, clear the
            # seeded name, and close the popover.
            await page.get_by_test_id("new-chat-landing-branch-chip").click()
            await page.get_by_test_id("new-chat-landing-branch-input").fill("")
            await page.keyboard.press("Escape")
            await expect(page.get_by_test_id("new-chat-landing-branch-chip")).to_contain_text(
                "Worktree"
            )

            await page.get_by_test_id("new-chat-landing-input").fill(
                "start this one in the main checkout please"
            )
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(state.create_bodies) >= 1, timeout_s=10.0)

            body = state.create_bodies[0]
            assert body["project_id"] == _PROJECT_ID, body
            assert "git" in body and body["git"] is None, (
                "clearing the seeded branch is a settled opt-out and must pin "
                f"an explicit `git: null` on the create - got: {body}"
            )
        finally:
            await page.context.close()
            await browser.close()
