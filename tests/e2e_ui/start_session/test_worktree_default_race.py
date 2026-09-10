"""E2E repro: the "Random worktree" default is lost when Send races the probe.

With the worktree default enabled in BOTH places — Settings › Git ("always use
a worktree", ``localStorage["omnigent:always-use-worktree"]``) and the
project's settings (``config.use_worktree: true``) — a new session must start
in a fresh randomly-named worktree. The composer implements this by
auto-seeding a ``worktree-<hex>`` branch once its git-ness probe
(``GET /v1/hosts/{id}/worktrees``) resolves, and the create ``POST
/v1/sessions`` carries ``git: {branch_name}`` only when that seed is already in
place (``NewChatDialog.tsx``). The server never default-fills a worktree from
``use_worktree`` (the flag is unknown to the backend), so the client seed is
the only path that creates one.

Nothing gates submit on that seed: ``canSubmit`` needs only a message, agent,
host, and a shape-valid workspace. On a slow host connection the probe takes
seconds, so a user who types the first message and hits Send promptly creates
the session with no ``git`` block — it launches directly in the repo's main
checkout, silently, despite both toggles being on. That is the reported
"sometimes the worktree is not created".

This test drives the real journey — turn the global toggle on in Settings ›
Git, open a new chat for a project whose settings also enable the worktree
default, type and send the first message — while the worktree probe is held in
flight (fulfilled shortly *after* Send, modelling the slow host). It asserts
the feature's contract: the create must still carry a generated
``git.branch_name``. Today the create posts no ``git`` block, so this fails —
the regression guard for the fix.

Route stubbing mirrors ``test_project_config_prefill.py`` (and is required for
the same reason: the e2e_ui harness's tunneled runner registers no host): the
hosts / agents / project-config / create endpoints are faked, the create POST
body is captured as the observable, and the worktrees endpoint is the one
route this test additionally *delays* to expose the race.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e_wtrace"
_PROJECT_ID = "proj_e2e_wtrace"
_PROJECT_NAME = "WorktreeRaceProject"
# A git repo whose main tree is the project's configured workspace.
_GIT_REPO = "/work/omnigent"
# Bare create endpoint (POST captured); NOT the /{id}/... sub-routes.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
# One project config endpoint: /v1/projects/<id> (not the bare list).
_PROJECT_CFG_RE = re.compile(r"/v1/projects/[^/?]+")
# The worktree-list endpoint the composer probes for the seeded workspace —
# the request this test holds in flight to model a slow host connection.
_WORKTREES_RE = re.compile(r"/v1/hosts/[^/]+/worktrees")
# How long after the Send click the held probe resolves. Today's composer
# posts the create synchronously on click, so the capture happens well before
# this; a fixed composer (one that waits for / re-checks the seed) gets the
# probe result shortly after Send and can still create the worktree.
_PROBE_DELAY_S = 2.0


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
    """``GET /v1/sessions/projects`` — resolves the ?project= name to its id."""
    return json.dumps([{"id": _PROJECT_ID, "name": _PROJECT_NAME}])


def _project_config_body() -> str:
    """``GET /v1/projects/{id}`` — the project's "Random worktree" toggle is ON
    (``use_worktree: true``), alongside its host/workspace defaults."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": _HOST_ID, "workspace": _GIT_REPO, "use_worktree": True},
        }
    )


def _git_repo_worktrees_body() -> str:
    """``GET /v1/hosts/{id}/worktrees`` — a plain git repo (one main tree), so
    the workspace is eligible for a fresh worktree once the probe resolves."""
    return json.dumps(
        {
            "object": "list",
            "data": [
                {"path": _GIT_REPO, "branch": "main", "is_main": True, "detached": False},
            ],
        }
    )


def test_worktree_created_when_send_races_slow_worktree_probe(
    seeded_session: tuple[str, str],
) -> None:
    """Sending the first message before the worktree probe resolves must still
    create the worktree the enabled defaults promise.

    Reproduces the reported defect: with "Random worktree" ON in both Settings › Git and
    the project's settings, a Send that lands while ``GET
    /v1/hosts/{id}/worktrees`` is still in flight posts the create with no
    ``git`` block, so the session silently starts in the repo's main checkout
    instead of a fresh worktree.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_worktree_race(base_url, session_id))


async def _drive_worktree_race(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        # Held worktree probe: every request waits on this before fulfilling,
        # modelling a slow server→host round-trip.
        release_probe = asyncio.Event()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_project_config_body()
                )

            async def handle_worktrees(route: Route) -> None:
                # The slow probe: answer only once released. Fulfilling can
                # race the page/context teardown — swallow that, the probe's
                # answer no longer matters by then.
                await release_probe.wait()
                with contextlib.suppress(Exception):
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=_git_repo_worktrees_body(),
                    )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
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

            # Step 1 — the user turns the global default on in Settings › Git
            # (the project's own toggle is already on via its stored config).
            await page.goto(f"{base_url}/settings/git")
            toggle = page.get_by_test_id("settings-always-use-worktree-toggle")
            await toggle.wait_for(state="visible", timeout=30_000)
            await toggle.click()
            await expect(toggle).to_have_attribute("aria-checked", "true")

            # Step 2 — open a new chat for the project. Host + workspace
            # prefill from the project config; the worktree probe for the
            # workspace goes out and is HELD (slow host).
            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "omnigent", timeout=15_000
            )

            # Step 3 — type the first message and Send promptly, while the
            # probe is still in flight. The probe resolves shortly AFTER the
            # click, like a slow host answering a moment too late.
            branch_chip_at_send = await page.get_by_test_id(
                "new-chat-landing-branch-chip"
            ).inner_text()
            await page.get_by_test_id("new-chat-landing-input").fill(
                "please look into the flaky test"
            )
            asyncio.get_running_loop().call_later(_PROBE_DELAY_S, release_probe.set)
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The contract of the enabled defaults: the create carries a
            # generated worktree branch. Today it posts no ``git`` block at
            # all (the seed effect was still waiting on the probe and nothing
            # gates Send on it), so the session starts in the main checkout.
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["project_id"] == _PROJECT_ID, body
            git = body.get("git") or {}
            assert re.fullmatch(r"worktree-[0-9a-f]{8}", git.get("branch_name", "")), (
                "Random worktree is ON in both Settings > Git and the project "
                "settings, so the create must carry git.branch_name for a fresh "
                f"worktree; instead it posted git={body.get('git')!r} "
                f"(branch chip at send: {branch_chip_at_send!r}) — the session "
                f"launches in the repo's main checkout with no worktree. Body: {body}"
            )
        finally:
            release_probe.set()
            # Close the context before the browser so a recorded video (the
            # harness's OMNIGENT_E2E_RECORD_DIR lane) is finalized even when
            # the drive fails at an assertion.
            try:
                await page.context.close()
            finally:
                await browser.close()
