"""``cli_auth``'s runtime path must stay off the UI-SDK import graph.

Per-hook-event subprocesses (the claude-native observer and permission
hooks) call ``load_databricks_org_id`` / ``databricks_request_headers``
on their blocking spawn-to-exit path. Resolving the token file through
``omnigent_ui_sdk`` executes that package's eager ``__init__`` chain
(hundreds of modules: httpx, pydantic, the spec parser), so every such
lookup re-pays a user-visible stall in each fresh spawn. The state-dir
computation is therefore local to ``cli_auth`` and must stay in
lock-step with ``omnigent_ui_sdk.terminal._config.state_dir``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent import cli_auth

# Modules that must never be loaded by a token-record lookup: the UI-SDK
# package whose __init__ drags the heavy graph, plus sentinels of that graph.
_FORBIDDEN_MODULES = ("omnigent_ui_sdk", "pydantic", "httpx", "omnigent.spec.parser")

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_org_id_lookup_never_loads_the_ui_sdk_graph(tmp_path: Path) -> None:
    """
    A fresh-interpreter org-id lookup stays off the heavy import graph.

    Runs the lookup exactly as a spawned hook does — new process, nothing
    pre-imported — and inspects ``sys.modules`` afterwards. Fails when the
    token-file path resolution (or anything else on the lookup path) routes
    through ``omnigent_ui_sdk`` again.
    """
    code = "\n".join(
        (
            "import json, sys",
            f"sys.path.insert(0, {str(_REPO_ROOT)!r})",
            "from omnigent.cli_auth import load_databricks_org_id",
            "org = load_databricks_org_id('https://ws.example.com/api/2.0/omnigent')",
            "assert org is None, org",
            f"loaded = [m for m in {list(_FORBIDDEN_MODULES)!r} if m in sys.modules]",
            "print(json.dumps(loaded))",
        )
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={"OMNIGENT_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded == [], (
        f"looking up a token record imported {loaded}; this runs inside "
        "per-hook-event subprocesses, so every module here is a per-event stall"
    )


def test_token_file_path_honors_data_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OMNIGENT_DATA_DIR`` still relocates the token file."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    assert cli_auth._token_file_path() == tmp_path / "auth_tokens.json"


def test_token_file_path_expands_user_in_data_dir_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``~``-relative override expands, matching the shared helper."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", "~/omnigent-alt-state")
    expected = Path.home() / "omnigent-alt-state" / "auth_tokens.json"
    assert cli_auth._token_file_path() == expected


def test_token_file_path_defaults_to_home_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the override the file stays in ``~/.omnigent``."""
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    assert cli_auth._token_file_path() == Path.home() / ".omnigent" / "auth_tokens.json"


@pytest.mark.parametrize("override", [None, "explicit"])
def test_token_file_path_stays_in_lock_step_with_ui_sdk_state_dir(
    override: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The local computation and the shared helper agree, with and without
    the override — the drift guard for computing the path locally.
    """
    from omnigent_ui_sdk.terminal._config import state_dir

    if override is None:
        monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    assert cli_auth._token_file_path() == Path(state_dir()) / "auth_tokens.json"
