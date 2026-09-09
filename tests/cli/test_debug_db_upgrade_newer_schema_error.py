"""Tests for ``omnigent debug db-upgrade`` on a newer-build database.

A database stamped by a newer Omnigent build cannot be upgraded by an older
binary. The migration guard intentionally rejects it with an actionable
"upgrade Omnigent" message, but that rejection used to escape the command as
a plain ``RuntimeError`` and ride the CLI's crash handler — a crash screen,
a saved crash report, and a file-an-issue prompt for a fully expected
operator situation. The command must render it as a normal CLI error.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.version import VERSION

# An Alembic revision id no build's migration chain will ever contain —
# stands in for a database stamped by a build newer than this one.
_UNKNOWN_REVISION = "ffffff999999"


def _make_db_stamped_at_unknown_revision(db_path: Path) -> str:
    """Create a SQLite file whose ``alembic_version`` names an unknown revision.

    The guard only reads the stamp, so a bare ``alembic_version`` table is
    enough — no real schema or migration run is needed.

    :param db_path: Filesystem path of the SQLite file to create.
    :returns: The SQLAlchemy URI of the created database.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", (_UNKNOWN_REVISION,))
        conn.commit()
    finally:
        conn.close()
    return f"sqlite:///{db_path}"


def test_db_upgrade_newer_schema_renders_clean_cli_error(tmp_path: Path) -> None:
    """The newer-schema rejection must surface as a CLI error, not an escape.

    An escaping non-Click exception is what routes the rejection into the
    crash reporter (crash screen + saved report + file-an-issue prompt).
    """
    uri = _make_db_stamped_at_unknown_revision(tmp_path / "chat.db")

    result = CliRunner().invoke(cli, ["debug", "db-upgrade", uri])

    assert result.exit_code != 0, (
        f"db-upgrade against a newer-build database must fail; output:\n{result.output}"
    )
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        "db-upgrade let the newer-schema rejection escape as "
        f"{result.exception!r}, which the CLI routes to the crash reporter"
    )
    assert "newer than this version" in result.output, (
        "db-upgrade must tell the user the database is newer than this build "
        f"and to upgrade Omnigent; instead it printed:\n{result.output}"
    )
    assert VERSION in result.output, (
        "db-upgrade must state the installed Omnigent version so the user "
        f"knows which build is too old; instead it printed:\n{result.output}"
    )
    assert "Traceback (most recent call last)" not in result.output, (
        f"db-upgrade printed a raw traceback instead of a friendly error:\n{result.output}"
    )
