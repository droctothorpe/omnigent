"""E2E: ``omnigent import`` must bind the imported session to this machine's host.

A session imported
through the web's host-mediated flow (``POST /v1/imports/local``) is bound to
the host that read the transcript, so resuming defaults to the machine the
workspace lives on. The CLI path imports the very same transcript from the
very same machine - one that is a registered, online host - but creates the
session with no ``host_id`` at all, leaving it unbound (the web then falls
back to the "Resume on a machine" picker instead of the machine it came from).

This drives the real journey: a real host daemon registers this machine on the
live server, then the real CLI imports a local Claude Code session against the
same server, sharing the daemon's HOME (and therefore its host identity).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _machine_env(home: Path) -> dict[str, str]:
    """Environment for host/CLI subprocesses acting as one machine at ``home``.

    Strips ambient runner/host identity vars (this suite may itself run inside
    a server-spawned runner) so the host identity comes from ``home/.omnigent``
    and transcripts from ``home/.claude``.
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST")) or key in (
            "RUNNER_SERVER_URL",
            "OMNIGENT_REMOTE_AUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
        ):
            env.pop(key)
    env["HOME"] = str(home)
    env["OMNIGENT_CONFIG_HOME"] = str(home / "config")
    env["OMNIGENT_DATA_DIR"] = str(home / "omnigent-data")
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return env


@pytest.fixture()
def online_host(live_server: str, tmp_path: Path) -> Iterator[str]:
    """Register this machine as a live host on the server and yield its host id.

    Runs the real ``omnigent host`` daemon loop with HOME at ``tmp_path`` so
    the identity it mints (``tmp_path/.omnigent/config.yaml``) is the same one
    the CLI under test reads.
    """
    log_path = tmp_path / "host.log"
    with log_path.open("w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=_machine_env(tmp_path),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        try:
            host_id: str | None = None
            identity_path = tmp_path / ".omnigent" / "config.yaml"
            deadline = time.time() + 90
            while time.time() < deadline:
                if host_id is None and identity_path.exists():
                    host_cfg = yaml.safe_load(identity_path.read_text()) or {}
                    host_id = (host_cfg.get("host") or {}).get("host_id")
                if host_id is not None:
                    hosts = httpx.get(f"{live_server}/v1/hosts", timeout=5).json()["hosts"]
                    if any(h["host_id"] == host_id and h["status"] == "online" for h in hosts):
                        break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "host daemon never came online; log tail:\n"
                    + "\n".join(log_path.read_text().splitlines()[-20:])
                )
            yield host_id
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_cli_import_binds_session_to_this_machines_host(
    live_server: str,
    tmp_path: Path,
    online_host: str,
) -> None:
    """A CLI import from a registered machine binds the session to that host.

    The transcript records a workspace (``cwd``), the machine is a live host
    on the target server, and the identical import through the web path binds
    ``host_id`` - so the CLI-created session must carry this machine's host id
    too, not ``None``.
    """
    source_session_id = str(uuid.uuid4())
    transcript = tmp_path / ".claude" / "projects" / "-repo" / f"{source_session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-1",
                "cwd": "/repo",
                "message": {"role": "user", "content": "cli import host binding"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done."}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "import",
            "--harness",
            "claude",
            "--session",
            source_session_id,
            "--server",
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=_machine_env(tmp_path),
    )

    match = re.search(r"Imported \d+ item\(s\) into \S+/c/(\S+)", result.stdout)
    assert match is not None, result.stdout
    session_id = match.group(1)

    session = httpx.get(
        f"{live_server}/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=10,
    )
    session.raise_for_status()
    session_data = session.json()
    assert session_data["workspace"] == "/repo"
    assert session_data["host_id"] == online_host, (
        "CLI-imported session is not bound to the importing machine's host: "
        f"expected host_id {online_host!r}, got {session_data['host_id']!r} "
        "(the web's /v1/imports/local path binds it; the CLI path must too)"
    )
