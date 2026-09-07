"""E2E regression guards for claude-native's per-hook-event subprocess cost.

Claude Code blocks its TUI on every command hook, and the native wrapper
wires those hooks at launch (``prepare_bridge_dir`` + ``build_hook_settings``
-- the exact calls a real session-create performs). Each spawned hook process
is therefore paid *per event*: per prompt submit, per turn end, per task
update, per streamed chunk, per statusline tick, per tool call. A heavy
import creeping anywhere onto a hook's spawn-to-exit path is a direct,
user-visible latency regression (the TUI stalls ~1s at every such event)
even while every functional test stays green.

This suite drives the REAL wiring end to end, the way Claude Code does:

* build the bridge dir and hook settings exactly as a web-launched session
  does (``ap_server_url`` configured, active session id recorded), then
* spawn a fresh interpreter that runs the observer hook with a real event
  payload on stdin, and pin the *runtime* import graph of that full run.

The existing guards in ``tests/test_claude_native_message_display_hook.py``
pin only the hook module's IMPORT graph. That misses the class of regression
this file exists for: ``main()`` lazily importing a heavy module while
handling an event (e.g. building the conversation URL through
``conversation_browser``/``server_url``/``cli_auth``, which drags in
httpx/pydantic/the spec parser at runtime), which re-adds ~0.7s to every
observer event on a configured bridge while the import-graph guards stay
green.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.claude_native_bridge import (
    MESSAGE_DELTAS_FILE,
    build_hook_settings,
    prepare_bridge_dir,
    read_message_deltas_from_offset,
)

# Repo root of this checkout, handed to spawned interpreters so the child
# resolves the same ``omnigent`` package as the test process (the dev venv
# has no installed copy; the real product runs the hooks with ``-I`` against
# an installed package, which pays the same imports).
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Third-party / heavy-graph modules that must never ride a blocking hook's
# spawn-to-exit path. Mirrors the module-import guard list in
# ``tests/test_claude_native_message_display_hook.py``; here it is enforced
# over a full event RUN, not just the module import.
_HEAVY_IMPORTS = (
    "fastapi",
    "httpx",
    "omnigent.inner.databricks_executor",
    "omnigent.inner.datamodel",
    "omnigent.model_catalog",
    "omnigent.spec.parser",
    "pydantic",
)

# A real web-launched session always configures the permission hook with the
# server's URL; that is the configuration under which the observer hook
# builds conversation URLs. Port 9 (discard) refuses connections instantly,
# and the observer path must never need the network anyway.
_AP_SERVER_URL = "http://127.0.0.1:9"


@pytest.fixture
def configured_bridge() -> Iterator[tuple[Path, dict[str, Any]]]:
    """
    Yield a launch-configured bridge dir plus its generated hook settings.

    Uses the production trusted bridge root (no monkeypatching) so spawned
    hook subprocesses accept the directory exactly as they do in a real
    session, and a fresh conversation id per test so parallel runs never
    collide.

    :yields: ``(bridge_dir, settings)`` -- the bridge directory and the
        Claude settings fragment a real launch would install.
    """
    conversation_id = f"conv_hookspawn_{uuid.uuid4().hex[:12]}"
    bridge_dir = prepare_bridge_dir(conversation_id, workspace=_REPO_ROOT)
    settings = build_hook_settings(
        bridge_dir,
        ap_server_url=_AP_SERVER_URL,
        ap_auth_headers={"Authorization": "Bearer test-token"},
    )
    try:
        yield bridge_dir, settings
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


def _hook_commands(settings: dict[str, Any], group: str) -> list[str]:
    """
    Return every command registered for one hook group.

    :param settings: Claude settings fragment from ``build_hook_settings``.
    :param group: Hook group name, e.g. ``"Stop"``.
    :returns: The ``command`` strings across all entries of the group.
    """
    return [
        hook["command"] for entry in settings["hooks"].get(group, []) for hook in entry["hooks"]
    ]


def _catch_all_commands(settings: dict[str, Any], group: str) -> list[str]:
    """
    Return the commands of a group's matcher-less (catch-all) entries.

    :param settings: Claude settings fragment from ``build_hook_settings``.
    :param group: Hook group name, e.g. ``"PreToolUse"``.
    :returns: Commands that fire for EVERY tool, not just a matched one.
    """
    return [
        hook["command"]
        for entry in settings["hooks"].get(group, [])
        if "matcher" not in entry
        for hook in entry["hooks"]
    ]


def _heavy_imports_after_hook_run(bridge_dir: Path, payload: dict[str, object]) -> list[str]:
    """
    Run one observer hook event in a fresh interpreter; report heavy modules.

    Feeds *payload* on stdin and runs ``omnigent.claude_native_hook.main``
    against *bridge_dir* -- the same module, argv, and stdin contract Claude
    Code uses when it spawns the wired command -- then inspects the child's
    ``sys.modules`` after the event was fully handled.

    :param bridge_dir: Launch-configured bridge directory.
    :param payload: Hook event JSON, e.g. ``{"hook_event_name": "Stop"}``.
    :returns: The subset of :data:`_HEAVY_IMPORTS` the full run loaded.
    """
    code = "\n".join(
        (
            "import json, sys",
            f"sys.path.insert(0, {str(_REPO_ROOT)!r})",
            "import omnigent.claude_native_hook as hook",
            f"rc = hook.main(['--bridge-dir', {str(bridge_dir)!r}])",
            "assert rc == 0, f'observer hook exited {rc}'",
            f"heavy = [m for m in {list(_HEAVY_IMPORTS)!r} if m in sys.modules]",
            "print(json.dumps(heavy))",
        )
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop", "TaskCreated"])
def test_observer_hook_event_run_stays_import_light(
    configured_bridge: tuple[Path, dict[str, Any]], event: str
) -> None:
    """
    Handling one per-turn observer event loads none of the heavy graph.

    Claude Code blocks on these hooks at every prompt submit, turn end, and
    task event, so the whole spawn-to-exit path -- module import AND the
    event handling ``main()`` performs -- must stay on cheap imports. Fails
    when any code reached while handling the event (recording it, rotating
    sessions, building the conversation URL) imports httpx, pydantic, or the
    spec/datamodel graph: that is the ~1s-per-event TUI stall users report,
    in its deterministic form.
    """
    bridge_dir, settings = configured_bridge
    commands = _hook_commands(settings, event)
    # Journey wiring: this event really does spawn the observer module, so
    # the child below exercises the same code path Claude Code pays for.
    assert any("omnigent.claude_native_hook" in command for command in commands), commands

    payload = {"hook_event_name": event, "session_id": "claude-session-1"}
    heavy = _heavy_imports_after_hook_run(bridge_dir, payload)
    assert heavy == [], (
        f"handling a {event} hook event imported {heavy}; the observer hook "
        "is spawned fresh per event and Claude Code blocks on it, so every "
        "module here is a per-event TUI stall"
    )


def test_hot_path_hooks_pay_no_interpreter_spawn(
    configured_bridge: tuple[Path, dict[str, Any]],
) -> None:
    """
    Per-chunk, per-tick, and per-tool-call hooks never spawn Python outright.

    ``MessageDisplay`` fires per streamed assistant-text chunk, the status
    line per refresh, and the catch-all policy entries twice per tool call.
    Wiring any of them to an unconditional interpreter spawn caps streaming
    at a few chunks per second and stalls every tool call -- the storm this
    suite exists to keep dead.
    """
    _bridge_dir, settings = configured_bridge

    message_display_commands = _hook_commands(settings, "MessageDisplay")
    assert len(message_display_commands) == 1, message_display_commands
    assert "python" not in message_display_commands[0], message_display_commands[0]

    # The statusLine may chain a user-configured command, so pin the part we
    # own: it must not run any omnigent module through an interpreter.
    status_command = settings["statusLine"]["command"]
    assert "-m omnigent" not in status_command, status_command

    python_prefix = shlex.quote(sys.executable)
    for group in ("PreToolUse", "PostToolUse"):
        catch_alls = _catch_all_commands(settings, group)
        assert catch_alls, f"{group} lost its catch-all policy entry"
        for command in catch_alls:
            # Relay-first: the every-tool-call path is a bare curl against
            # the runner relay; an interpreter may appear only as the
            # pre-relay/curl-failure fallback, never as the first hop.
            assert "curl" in command, command
            assert not command.lstrip().startswith(python_prefix), command


def test_message_display_sh_hook_appends_delta_without_python(
    configured_bridge: tuple[Path, dict[str, Any]],
) -> None:
    """
    The wired per-chunk appender lands a delta the bridge reader parses.

    Spawns the exact ``MessageDisplay`` command Claude Code would run, with a
    real chunk payload on stdin, and reads it back through the same bridge
    reader the forwarder uses -- proving the interpreter-free hot path still
    delivers live streaming, not just that it is cheap.
    """
    bridge_dir, settings = configured_bridge
    command = _hook_commands(settings, "MessageDisplay")[0]
    payload = {
        "hook_event_name": "MessageDisplay",
        "message_id": "m1",
        "index": 0,
        "final": False,
        "delta": "Hello world",
    }
    proc = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (bridge_dir / MESSAGE_DELTAS_FILE).exists()

    result = read_message_deltas_from_offset(bridge_dir, 0)
    assert [(d.message_id, d.index, d.final, d.delta) for d in result.deltas] == [
        ("m1", 0, False, "Hello world"),
    ]
