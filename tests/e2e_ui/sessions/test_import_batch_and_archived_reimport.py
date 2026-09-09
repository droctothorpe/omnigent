"""E2E: import journeys that need a REAL host: oversized batches, archived re-imports.

Unlike ``test_import_sessions.py`` (which stubs the import endpoints to test
the panel wiring), these tests connect a real ``omnigent host`` daemon to the
spawned live server and let it read genuine Claude Code transcripts, because
the reported failures live in the host tunnel + server import pipeline:

* A batch containing a super-large (>100 MiB) session must complete: today the
  oversized ``host.import_local_session`` frame exceeds the tunnel's message
  cap, kills the host tunnel, the sessions after it never import, and the UI
  sits on "Importing..." for the full 60s per-frame timeout before one
  wholesale error replaces the tally.
* Re-importing a session whose previous import was archived in omni must
  surface that session again: today it is counted "already imported" while
  staying hidden in the archive, so the user can never get it back via import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Big enough that the session's single import frame exceeds the tunnel's
# 100 MiB message cap (RUNNER_TUNNEL_MAX_MESSAGE_BYTES): ~5600 turns of two
# ~10 KB messages each is ~112 MiB of transcript/item JSON.
_OVERSIZED_TURNS = 5_600
_OVERSIZED_MESSAGE_TEXT = "x" * 10_000


def _host_env(home: Path) -> dict[str, str]:
    """Environment for a host daemon subprocess isolated under ``home``.

    Strips the ambient runner/host identity vars (this suite may itself run
    inside a server-spawned runner) so the daemon builds a fresh identity in
    ``home/.omnigent`` and reads transcripts from ``home/.claude``.
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


def _write_claude_transcript(
    home: Path,
    session_id: str,
    *,
    first_message: str,
    filler_turns: int = 0,
    filler_text: str = "",
    mtime: float,
) -> None:
    """Write a Claude Code parent transcript under ``home/.claude/projects``.

    ``first_message`` leads the transcript so it becomes the imported
    session's synthesized title; ``filler_turns`` user/assistant pairs of
    ``filler_text`` follow to inflate the transcript for the oversized case.
    """
    path = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "user-0",
                    "cwd": "/repo",
                    "message": {"role": "user", "content": first_message},
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "assistant-0",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"ack: {first_message}"}],
                    },
                }
            )
            + "\n"
        )
        for i in range(filler_turns):
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": f"user-{i + 1}",
                        "message": {"role": "user", "content": f"q{i}: {filler_text}"},
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": f"assistant-{i + 1}",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": f"a{i}: {filler_text}"}],
                        },
                    }
                )
                + "\n"
            )
    os.utime(path, (mtime, mtime))


@pytest.fixture()
def import_host(live_server: str, tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Connect a real host daemon (fresh identity + HOME) to the live server.

    Yields ``(host_id, home)``: the online host's id and the HOME whose
    ``.claude`` tree the tests seed with transcripts. The daemon is the real
    ``omnigent host`` loop, so imports exercise the genuine tunnel read path.
    """
    home = tmp_path / "host-home"
    home.mkdir()
    log_path = tmp_path / "host.log"
    with log_path.open("w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=_host_env(home),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        try:
            host_id: str | None = None
            identity_path = home / ".omnigent" / "config.yaml"
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
            yield host_id, home
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _select_import_machine_and_harness(page: Page, host_id: str) -> None:
    """On Settings > Import, pick the given machine and the Claude Code harness."""
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-host-select").click()
    page.get_by_test_id(f"import-host-{host_id}").click()
    page.get_by_test_id("import-source-select").click()
    page.get_by_role("option", name="Claude Code").click()


def test_batch_import_with_oversized_session_completes_without_wholesale_error(
    page: Page,
    live_server: str,
    import_host: tuple[str, Path],
) -> None:
    """Settings > Import survives a >100 MiB session: whole batch lands, no stall.

    Journey: the machine has an older small session, a super-large
    one, and a newer small one. The user imports recent Claude Code sessions.
    The batch must finish with all three sessions imported and listed - not
    die on the oversized frame, stall on "Importing..." for the 60s per-frame
    timeout, and replace the tally with one wholesale error while the newer
    session silently never imports.
    """
    host_id, home = import_host
    tag = uuid.uuid4().hex[:8]
    now = time.time()
    old_title = f"import batch old small {tag}"
    big_title = f"import batch oversized {tag}"
    new_title = f"import batch new small {tag}"
    _write_claude_transcript(home, str(uuid.uuid4()), first_message=old_title, mtime=now - 300)
    _write_claude_transcript(
        home,
        str(uuid.uuid4()),
        first_message=big_title,
        filler_turns=_OVERSIZED_TURNS,
        filler_text=_OVERSIZED_MESSAGE_TEXT,
        mtime=now - 200,
    )
    _write_claude_transcript(home, str(uuid.uuid4()), first_message=new_title, mtime=now - 100)

    page.goto(f"{live_server}/settings/import")
    _select_import_machine_and_harness(page, host_id)
    page.get_by_test_id("import-submit").click()

    # The failure mode is a ~60s "Importing..." stall ending in one wholesale
    # error; the fixed flow ends in the tally. Wait for either terminal state
    # (generous budget: the oversized upload is ~112 MiB), then require the
    # tally: every session imported, no wholesale error.
    outcome = page.get_by_test_id("import-result").or_(page.get_by_test_id("import-error"))
    expect(outcome.first).to_be_visible(timeout=240_000)
    expect(
        page.get_by_test_id("import-error"),
        "batch import died with a wholesale error instead of importing every session",
    ).to_have_count(0)
    expect(page.get_by_test_id("import-result")).to_contain_text("Imported 3")

    # The sessions after the oversized one really landed: the newest session
    # is in the sidebar session list (hidden on /settings, so go home).
    page.goto(f"{live_server}/")
    sidebar = page.get_by_test_id("sidebar-conversation-list")
    expect(sidebar).to_contain_text(new_title, timeout=30_000)
    expect(sidebar).to_contain_text(big_title)


def test_reimport_after_archive_resurfaces_the_session(
    page: Page,
    live_server: str,
    import_host: tuple[str, Path],
) -> None:
    """Re-importing a session archived in omni makes it visible again.

    Journey: import a Claude Code session, archive it from the sidebar, then import the
    same session again from Settings > Import. Today the re-import reports
    "already imported" while the session stays hidden in the archive, so the
    user has no way to get it back through the import flow.
    """
    host_id, home = import_host
    tag = uuid.uuid4().hex[:8]
    title = f"archived reimport {tag}"
    source_session_id = str(uuid.uuid4())
    _write_claude_transcript(home, source_session_id, first_message=title, mtime=time.time())

    def run_exact_import() -> None:
        page.goto(f"{live_server}/settings/import")
        _select_import_machine_and_harness(page, host_id)
        page.get_by_test_id("import-mode-select").click()
        page.get_by_role("option", name="Session by ID").click()
        page.get_by_test_id("import-session-id").fill(source_session_id)
        page.get_by_test_id("import-submit").click()
        outcome = page.get_by_test_id("import-result").or_(page.get_by_test_id("import-error"))
        expect(outcome.first).to_be_visible(timeout=120_000)
        expect(page.get_by_test_id("import-error")).to_have_count(0)

    # First import: the session lands and shows in the sidebar list.
    run_exact_import()
    page.goto(f"{live_server}/")
    sidebar = page.get_by_test_id("sidebar-conversation-list")
    expect(sidebar).to_contain_text(title, timeout=30_000)

    # Archive it from the sidebar row's actions menu (the real UI path).
    row = page.locator("li").filter(has_text=title).first
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()
    expect(sidebar).not_to_contain_text(title, timeout=10_000)

    # Re-import the same source session: the user asked for it back, so it
    # must be visible in the session list again - not silently counted as
    # "already imported" while staying hidden in the archive.
    run_exact_import()
    page.goto(f"{live_server}/")
    expect(
        page.get_by_test_id("sidebar-conversation-list"),
        "re-imported session stayed hidden in the archive",
    ).to_contain_text(title, timeout=30_000)
