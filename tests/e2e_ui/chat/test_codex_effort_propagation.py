"""E2E: composer-selected Codex effort must reach the codex-native terminal.

A Codex reasoning effort selected in the session composers must propagate to
the Codex terminal.

Two journeys, one per composer:

1. **New-session composer** (``test_new_session_effort_reaches_codex_terminal``,
   RED while the bug lives): the session is created with
   ``reasoning_effort="high"`` — the exact create-call field the new-session
   gear commits (see ``tests/e2e_ui/start_session/test_codex_effort_prelaunch``,
   which pins that wire contract) — and a message is sent through the real web
   composer. The codex TUI in the session terminal must run at the requested
   effort; while the bug lives the runner's codex-native launch drops the
   persisted ``reasoning_effort`` (``_CodexNativeLaunchConfig`` never reads
   it) and the native-terminal message forward carries no ``reasoning``
   either, so the TUI footer stays ``gpt-5.5 default``.

2. **Existing-session composer gear** (``test_gear_effort_change_reaches_codex_terminal``,
   green on main — regression guard): picking a different effort from the
   in-session gear PATCHes ``reasoning_effort``, which the server forwards as
   an ``effort_change`` and the runner applies to the live thread via
   ``thread/settings/update``; the TUI footer flips to the picked level.

The stack is real end to end — spawned Omnigent server + runner, the real
``codex`` CLI in a tmux-backed session terminal, the real SPA — with only the
LLM edge faked (the mock Responses server, via a pinned provider config), the
same lane as ``native_codex_mock_session``. The terminal-side assertion reads
the codex TUI pane text over tmux (``capture-pane``), i.e. exactly what the
user's Terminal view displays; the pane is identified by the unique message
the test sent so a parallel session's pane can never satisfy the check.
"""

from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import time
import uuid
from collections.abc import Generator, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode, _send

# The codex catalog default: codex ships effort metadata for it (so the TUI
# footer renders "<model> <effort>") and the web gear resolves its effort
# ladder from the catalog row once the session reports the model.
_CODEX_MODEL = "gpt-5.5"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_REPLY_TEXT = "Effort probe reply."
_TURN_TIMEOUT_MS = 120_000
# Codex cold boot (app-server + TUI + thread) is usually <30s on this lane.
_CODEX_READY_TIMEOUT_S = 240.0


def _require_codex_cli() -> None:
    """Skip when the real ``codex`` CLI is not installed on this machine."""
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is required for the codex-native terminal lane")


@contextlib.contextmanager
def _mock_codex_provider(mock_llm_server_url: str) -> Generator[None, None, None]:
    """Pin the Codex provider to the mock Responses server for this test.

    Mirrors ``_temp_omnigent_mock_config('codex')`` but honours
    ``OMNIGENT_CONFIG_HOME`` (CI points provider config there) and pins a
    reasoning-capable default model so effort is meaningful to codex.

    :param mock_llm_server_url: Mock LLM base URL, e.g. ``"http://127.0.0.1:51235"``.
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    config_dir = Path(config_home) if config_home else Path.home() / ".omnigent"
    config_path = config_dir / "config.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)
    original = config_path.read_text() if config_path.exists() else None
    config_path.write_text(
        textwrap.dedent(f"""\
            providers:
              mock-codex:
                kind: key
                default: [openai]
                openai:
                  base_url: "{mock_llm_server_url}/v1"
                  api_key: "mock-key"
                  wire_api: responses
                  models:
                    default: {_CODEX_MODEL}
            """)
    )
    try:
        yield
    finally:
        if original is not None:
            config_path.write_text(original)
        else:
            config_path.unlink(missing_ok=True)


def _create_codex_session(
    base_url: str,
    runner_id: str,
    *,
    reasoning_effort: str | None,
) -> str:
    """Create and bind a codex-native wrapper session.

    Mirrors ``_create_native_codex_session`` (the exact terminal-first spec
    ``omnigent codex`` ships, wrapper + terminal-first labels), adding the
    optional create-time ``reasoning_effort`` — the field the new-session
    composer's gear commits on the create call.

    :param base_url: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind.
    :param reasoning_effort: Create-time effort, e.g. ``"high"``, or ``None``.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_codex_agent_spec(Path(tmp), model=None)
        yaml_text = spec_path.read_text()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the native_codex_session fixture.
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    metadata: dict[str, object] = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
        },
        "workspace": str(_REPO_ROOT),
    }
    if reasoning_effort is not None:
        metadata["reasoning_effort"] = reasoning_effort
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("codex-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@contextlib.contextmanager
def _codex_terminal_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    reasoning_effort: str | None,
) -> Generator[str, None, None]:
    """A runner-bound codex-native session on the mock Responses provider.

    :param live_server: Spawned server fixture base URL.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param tmp_path_factory: Pytest temp path factory (runner respawn log).
    :param reasoning_effort: Create-time effort for the session, or ``None``.
    :yields: The session id.
    """
    _require_codex_cli()
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    reset_mock_llm(mock_llm_server_url)
    # Enough entries for the turn(s) plus the background title generation,
    # which draws from the same default queue.
    configure_mock_llm(mock_llm_server_url, [{"text": _REPLY_TEXT}] * 8)
    with _mock_codex_provider(mock_llm_server_url):
        session_id = _create_codex_session(
            live_server, runner_id, reasoning_effort=reasoning_effort
        )
        try:
            yield session_id
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _wait_codex_thread(base_url: str, session_id: str) -> None:
    """Block until the codex TUI owns a live thread (boot finished).

    :param base_url: Server base URL.
    :param session_id: Session id to poll.
    :raises AssertionError: If codex never captures a thread id in time.
    """
    deadline = time.time() + _CODEX_READY_TIMEOUT_S
    last: dict[str, object] = {}
    while time.time() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
        resp.raise_for_status()
        last = resp.json()
        if last.get("external_session_id"):
            return
        time.sleep(2.0)
    raise AssertionError(
        f"codex terminal never became ready for {session_id}: no external_session_id "
        f"after {_CODEX_READY_TIMEOUT_S}s (status={last.get('status')!r})"
    )


def _iter_tmux_panes() -> Iterator[tuple[str, str, str]]:
    """Yield ``(socket, target, text)`` for every live omnigent terminal pane.

    The runner hosts each session terminal on a private tmux socket at
    ``$TMPDIR/omnigent-terminal-*/tmux.sock``; ``capture-pane`` returns the
    visible pane text — exactly what the user's Terminal view renders.
    """
    roots = {os.environ.get("TMPDIR") or "/tmp", "/tmp"}
    sockets: set[str] = set()
    for root in roots:
        sockets.update(glob.glob(f"{root}/omnigent-terminal-*/tmux.sock"))
    for sock in sorted(sockets):
        try:
            panes = subprocess.run(
                [
                    "tmux",
                    "-S",
                    sock,
                    "list-panes",
                    "-a",
                    "-F",
                    "#{session_name}:#{window_index}.#{pane_index}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for target in panes.stdout.split():
                cap = subprocess.run(
                    ["tmux", "-S", sock, "capture-pane", "-p", "-t", target],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                yield sock, target, cap.stdout
        except (OSError, subprocess.SubprocessError):
            continue


def _wait_codex_pane_effort(
    marker: str,
    effort: str,
    timeout_s: float,
) -> tuple[bool, list[str]]:
    """Poll the session's codex pane for the expected footer effort.

    The pane is identified by *marker* (the unique message this test sent
    through the composer, echoed as ``› <marker>`` in the TUI), so another
    session's terminal can never satisfy the check. The codex TUI footer
    renders ``<model> <effort> · <cwd>`` — ``default`` when no effort ever
    reached the thread.

    :param marker: Unique substring of a message this session's TUI shows.
    :param effort: Expected effort level, e.g. ``"high"``.
    :param timeout_s: Poll budget in seconds.
    :returns: ``(matched, footer_lines)`` — the model-bearing pane lines
        observed on the last poll, for the assertion message.
    """
    pattern = re.compile(rf"{re.escape(_CODEX_MODEL)}\s+{re.escape(effort)}\b")
    deadline = time.time() + timeout_s
    footer_lines: list[str] = []
    matched = False
    last_text = ""
    while time.time() < deadline:
        for _sock, _target, text in _iter_tmux_panes():
            if marker not in text:
                continue
            last_text = text
            footer_lines = [line.strip() for line in text.splitlines() if _CODEX_MODEL in line]
            if pattern.search(text):
                matched = True
        if matched:
            break
        time.sleep(2.0)
    # When a recording run is on, drop the observed pane text alongside the
    # footage as machine-checkable evidence (best-effort).
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir and last_text:
        with contextlib.suppress(OSError):
            Path(record_dir).mkdir(parents=True, exist_ok=True)
            (Path(record_dir) / f"codex-pane-{marker}.txt").write_text(last_text)
    return matched, footer_lines


@pytest.mark.timeout(420)
def test_new_session_effort_reaches_codex_terminal(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> None:
    """A create-time Codex effort must reach the session's codex terminal.

    Journey (the reporter's "new session" steps): create a Codex session with
    a non-default effort — the ``reasoning_effort`` create field the
    new-session composer commits — then send a message through the web
    composer. The composer reflects the requested effort (the gear's Effort
    row shows ``high``), so the remaining leg is server → runner → codex; the
    codex TUI must run the thread at ``high``.

    Red while the bug lives: the codex-native terminal launch never reads the
    persisted ``reasoning_effort`` and the native-terminal message forward
    carries no ``reasoning``, so the TUI footer stays ``gpt-5.5 default``
    while the composer keeps claiming ``high``.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("codex-native terminal lane requires the spawned local server")
    marker = f"effort-probe-{uuid.uuid4().hex[:8]}"
    with _codex_terminal_session(
        live_server, mock_llm_server_url, tmp_path_factory, reasoning_effort="high"
    ) as session_id:
        _wait_codex_thread(live_server, session_id)

        page.goto(f"{live_server}/c/{session_id}")
        _select_view_mode(page, "Chat")

        # Send a real message through the composer; the unique marker also
        # tags this session's TUI pane for the terminal-side check.
        _send(page, f"{marker}: confirm the reasoning effort")
        expect(page.locator(_ASSISTANT).filter(has_text=_REPLY_TEXT).first).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )

        # The composer reflects the requested effort — the report's premise.
        # (The gear's Effort row keys its ladder on the reported model, so it
        # renders only after the session's first model report.)
        gear = page.get_by_test_id("composer-config-gear")
        expect(gear).to_be_visible(timeout=30_000)
        gear.click()
        expect(page.get_by_test_id("composer-config-modal")).to_be_visible()
        expect(page.get_by_test_id("composer-config-effort")).to_contain_text(
            "high", timeout=30_000
        )
        page.get_by_test_id("composer-config-cancel").click()

        # Show the terminal view (what a user checking the TUI sees) while
        # the pane is asserted underneath via tmux.
        _select_view_mode(page, "Terminal")
        matched, footer = _wait_codex_pane_effort(marker, "high", timeout_s=30.0)
        page.wait_for_timeout(2_000)
        assert matched, (
            "codex terminal never received the composer-selected reasoning effort: "
            "the session was created with reasoning_effort='high' (the new-session "
            "composer's create field) and a message was sent, but the codex TUI "
            f"still reports {footer!r} — expected '{_CODEX_MODEL} high'"
        )


@pytest.mark.timeout(420)
def test_gear_effort_change_reaches_codex_terminal(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> None:
    """A gear-picked effort in an existing session reaches the codex terminal.

    Journey (the reporter's "existing session" steps): open a live Codex
    session, pick a different effort from the composer gear, send a message.
    The gear's save PATCHes ``reasoning_effort``; the server forwards an
    ``effort_change`` and the runner applies it to the live thread via
    ``thread/settings/update`` — the codex TUI footer flips to the picked
    level and the next turn runs on it. Guards the working existing-session
    half against regression by a fix for the create-time half.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("codex-native terminal lane requires the spawned local server")
    marker = f"effort-probe-{uuid.uuid4().hex[:8]}"
    with _codex_terminal_session(
        live_server, mock_llm_server_url, tmp_path_factory, reasoning_effort=None
    ) as session_id:
        _wait_codex_thread(live_server, session_id)

        page.goto(f"{live_server}/c/{session_id}")
        _select_view_mode(page, "Chat")

        # First message tags this session's TUI pane with the unique marker.
        _send(page, f"{marker}: hello codex")
        expect(page.locator(_ASSISTANT).filter(has_text=_REPLY_TEXT).first).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )

        # Pick a different effort from the composer gear — the reporter's
        # journey — and save.
        gear = page.get_by_test_id("composer-config-gear")
        expect(gear).to_be_visible(timeout=30_000)
        gear.click()
        expect(page.get_by_test_id("composer-config-modal")).to_be_visible()
        effort_select = page.get_by_test_id("composer-config-effort")
        expect(effort_select).to_be_enabled(timeout=30_000)
        effort_select.click()
        page.locator('[data-effort-level="low"]').click()
        page.get_by_test_id("composer-config-save").click()
        expect(page.get_by_test_id("composer-config-modal")).to_be_hidden(timeout=30_000)

        # The live thread settings update lands without needing a new turn…
        matched, footer = _wait_codex_pane_effort(marker, "low", timeout_s=45.0)
        assert matched, (
            "codex terminal never received the gear-picked reasoning effort: "
            "the composer gear committed reasoning_effort='low' on a live codex "
            f"session, but the codex TUI still reports {footer!r} — expected "
            f"'{_CODEX_MODEL} low'"
        )

        # …and the next message keeps running on the picked effort.
        _send(page, f"{marker}: and again")
        expect(page.locator(_ASSISTANT).filter(has_text=_REPLY_TEXT).nth(1)).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )
        _select_view_mode(page, "Terminal")
        matched, footer = _wait_codex_pane_effort(marker, "low", timeout_s=15.0)
        page.wait_for_timeout(2_000)
        assert matched, (
            "codex terminal lost the gear-picked reasoning effort after the next "
            f"turn: the TUI reports {footer!r} — expected '{_CODEX_MODEL} low'"
        )
