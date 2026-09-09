"""E2E reproduction: db-upgrade on a mismatched-schema database must not crash.

A user whose ``chat.db`` was written by a different Omnigent build than the
one installed ran the ``omnigent debug db-upgrade`` command the product
itself suggests, and the command died with an uncaught
``alembic.util.exc.CommandError: Can't locate revision identified by
'za2b3c4d5e6f'`` traceback that triggered the crash reporter. Two journeys:

1. **The database is stamped at a revision this build knows** (the
   originally reported ``za2b3c4d5e6f``, which now ships in the migration
   chain): the upgrade must complete cleanly to head.
   ``test_db_upgrade_from_reported_revision_completes`` guards that.

2. **The database is stamped at a revision this build does NOT know** — the
   state a database written by a build newer than this one is left in. The
   newer-schema guard intentionally rejects this with an actionable
   "newer than this version of Omnigent ... Upgrade Omnigent" message, but
   that ``RuntimeError`` escapes to the CLI's generic crash handler, so the
   user gets a crash screen, a saved crash report, and a pre-filled
   "[Crash] ..." GitHub-issue prompt for a fully expected operator
   situation — the same crash-report pipeline that filed the original
   report. ``test_db_upgrade_unknown_revision_fails_cleanly_without_crash``
   reproduces that journey and FAILS until the rejection is rendered as a
   normal CLI error (no crash report, no file-an-issue prompt, no
   traceback).

Both tests drive the real CLI as a subprocess (the same command the user
ran); no live server or LLM is required.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

# Generous cap for one CLI subprocess (imports alone take a few seconds).
_CLI_TIMEOUT_S = 120

# The revision named in the original crash report. It exists in this
# build's migration chain, so a database genuinely at this revision must
# upgrade cleanly.
_REPORTED_REVISION = "za2b3c4d5e6f"

# An Alembic revision id that no build's migration chain will ever contain —
# stands in for a database stamped by a build newer than this one.
_UNKNOWN_REVISION = "ffffff999999"


def _cli_env(tmp_path: Path) -> dict[str, str]:
    """Environment for a spawned ``omnigent`` CLI, isolated from the host.

    Points config/data dirs at the test's tmp dir so the subprocess never
    reads or writes the developer's real ``~/.omnigent``, and guarantees the
    subprocess imports the same ``omnigent`` package the test process runs.

    :param tmp_path: The test's tmp directory.
    :returns: Env mapping for :func:`subprocess.run`.
    """
    import omnigent

    repo_root = Path(omnigent.__file__).resolve().parent.parent
    env = os.environ.copy()
    env["OMNIGENT_CONFIG_HOME"] = str(tmp_path / "config-home")
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "data-dir")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing}" if existing else str(repo_root)
    return env


# The installed ``omnigent`` binary's console entry point. Spawning it this
# way (rather than ``python -m omnigent.cli``, which skips ``main()``) keeps
# the crash handler installed, so the test observes the same console
# behavior the user did. Non-TTY, so the crash flow is the deterministic
# non-interactive notice rather than a prompt.
_CONSOLE_ENTRY = "import sys; sys.argv[0] = 'omnigent'; from omnigent.cli import main; main()"


def _run_cli(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent <args>`` exactly as a user would, capturing output.

    :param args: CLI arguments after the ``omnigent`` program name.
    :param tmp_path: The test's tmp directory (for env isolation).
    :returns: The completed process with captured stdout/stderr.
    """
    return subprocess.run(
        [sys.executable, "-c", _CONSOLE_ENTRY, *args],
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
        env=_cli_env(tmp_path),
    )


def _head_revision(db_uri: str) -> str:
    """Return the head revision of this build's migration chain."""
    script = ScriptDirectory.from_config(_build_alembic_config(db_uri))
    head = script.get_current_head()
    assert head is not None, "migration chain has no head"
    return head


def _stamped_revision(db_path: Path) -> str:
    """Read the ``alembic_version`` stamp from a SQLite database file."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert row is not None, "database has no alembic_version stamp"
    return str(row[0])


def _seed_db_at_revision(db_path: Path, revision: str) -> None:
    """Create a real Omnigent database, then walk it to *revision*.

    Runs the actual migration chain to head and downgrades to the target
    revision, so the file has the genuine schema of that revision — the
    state a database is in after being written by the build that shipped
    it.

    :param db_path: Filesystem path of the SQLite file to create.
    :param revision: Alembic revision to leave the database at.
    """
    uri = f"sqlite:///{db_path}"
    engine = sa.create_engine(uri)
    try:
        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, revision)
    finally:
        engine.dispose()
        clear_engine_cache()


def _seed_newer_build_db(db_path: Path) -> None:
    """Create a database that looks written by a newer Omnigent build.

    Builds the real schema at this build's head, then restamps
    ``alembic_version`` with a revision this build's chain does not
    contain — exactly the state a newer build leaves behind for an older
    binary to find.

    :param db_path: Filesystem path of the SQLite file to create.
    """
    _seed_db_at_revision(db_path, "head")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE alembic_version SET version_num = ?", (_UNKNOWN_REVISION,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.timeout(300)
def test_db_upgrade_from_reported_revision_completes(tmp_path: Path) -> None:
    """``debug db-upgrade`` on a DB at the reported revision must succeed.

    Journey: the user's database sits at revision ``za2b3c4d5e6f`` (a
    revision this build ships) and the user runs the suggested
    ``omnigent debug db-upgrade sqlite:///.../chat.db``. The command must
    migrate the database to head and exit cleanly — no crash, no error.

    Regression guard for the originally reported crash state: as long as
    the ``za2b3c4d5e6f`` migration remains in the chain, this exact
    database upgrades instead of raising "Can't locate revision".
    """
    db_path = tmp_path / "chat.db"
    _seed_db_at_revision(db_path, _REPORTED_REVISION)
    assert _stamped_revision(db_path) == _REPORTED_REVISION

    result = _run_cli(["debug", "db-upgrade", f"sqlite:///{db_path}"], tmp_path)
    combined = result.stdout + result.stderr

    assert result.returncode == 0, (
        f"db-upgrade from revision {_REPORTED_REVISION!r} must succeed; "
        f"got returncode {result.returncode} with output:\n{combined}"
    )
    assert "Upgrade complete." in result.stdout, (
        f"db-upgrade must report completion; instead it printed:\n{combined}"
    )
    assert _stamped_revision(db_path) == _head_revision(f"sqlite:///{db_path}"), (
        "db-upgrade must leave the database at this build's head revision"
    )


@pytest.mark.timeout(300)
def test_db_upgrade_unknown_revision_fails_cleanly_without_crash(tmp_path: Path) -> None:
    """``debug db-upgrade`` on a newer-build DB must error, not crash.

    Journey: the user's ``chat.db`` was migrated by a newer Omnigent build;
    the older installed binary points them at
    ``omnigent debug db-upgrade sqlite:///.../chat.db``. The command cannot
    upgrade such a database, so it must fail — but with the newer-schema
    guard's actionable message rendered as a plain CLI error.

    FAILS while the guard's rejection escapes to the generic crash handler:
    the user currently gets a crash screen, a saved crash report, a
    traceback, and a pre-filled "[Crash] ..." GitHub-issue prompt for a
    fully expected situation — the crash the original report captured.
    """
    db_path = tmp_path / "chat.db"
    _seed_newer_build_db(db_path)
    assert _stamped_revision(db_path) == _UNKNOWN_REVISION

    result = _run_cli(["debug", "db-upgrade", f"sqlite:///{db_path}"], tmp_path)
    combined = result.stdout + result.stderr

    # The command must still fail — this build cannot upgrade a database
    # stamped by a newer one.
    assert result.returncode != 0, (
        f"db-upgrade against a newer-build database must fail; "
        f"got returncode 0 with output:\n{combined}"
    )

    # The user must get the guard's actionable guidance...
    assert "newer than this version" in combined, (
        "db-upgrade must tell the user the database is newer than this "
        f"build and to upgrade Omnigent; instead it printed:\n{combined}"
    )

    # ...as a clean CLI error, not through the crash reporter.
    assert "crash report was saved" not in combined.lower(), (
        "db-upgrade routed an expected schema-mismatch rejection through "
        f"the crash reporter (crash report saved):\n{combined}"
    )
    assert "File an issue" not in combined, (
        "db-upgrade prompted the user to file a GitHub issue for an "
        f"expected schema-mismatch rejection:\n{combined}"
    )
    assert "Traceback (most recent call last)" not in combined, (
        f"db-upgrade printed a raw traceback instead of a friendly error message:\n{combined}"
    )
    assert "Can't locate revision" not in combined, (
        "db-upgrade leaked the raw alembic resolution error instead of the "
        f"actionable newer-schema message:\n{combined}"
    )
    assert "runner tunnel rejection" not in combined, (
        "db-upgrade appended the stale-host recovery hint to a schema-version "
        f"mismatch, which a stale host cannot cause:\n{combined}"
    )
