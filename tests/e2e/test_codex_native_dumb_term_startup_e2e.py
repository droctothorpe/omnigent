"""E2E: a dumb/unset TERM must not block native Codex app-server startup.

Regression guard: Codex's TERM=dumb confirmation must not block app-server
startup and end the session.

The reported journey
--------------------
A user opens a Codex (``codex-native``) session on a host whose daemon runs
under a non-interactive terminal (``TERM=dumb``, as an Arca / headless host
does). The runner launches the native Codex app-server for the session, but
Codex hits its TUI-only TERM confirmation::

    WARNING: TERM is set to "dumb". Codex's interactive TUI may not work in
    this terminal.
    Continue anyway? [y/N]:

Codex cannot proceed non-interactively, so it never starts a thread and the
web UI reports an app-server startup failure ("Codex app-server never started
a thread (startup timed out)"); the session ends instead of starting Codex.

Root cause exercised here
-------------------------
:func:`omnigent.harnesses.codex_native.app_server.build_codex_native_server`
builds the app-server subprocess environment via ``_clean_codex_env()``, which
allow-lists ``TERM`` (``omnigent.inner.agent_env.BASE_ALLOW_EXACT``) and so
passes the host daemon's ``TERM=dumb`` straight through to the ``codex
app-server`` process. Codex then refuses to start non-interactively.

This test drives the runner's real app-server launch path
(``build_codex_native_server`` + ``CodexNativeAppServer.start()``) under a
``TERM=dumb`` process, with a faithful fake ``codex`` CLI that models the real
vendor CLI's behaviour: its ``app-server`` subcommand refuses under a
dumb/unset TERM (prints the confirmation, declines when it cannot read a
"yes", and exits without serving), and serves normally under a sane TERM.

- On buggy code the product hands ``TERM=dumb`` to Codex, the fake refuses,
  and ``start()`` fails -> this test FAILS (the reproduction).
- On fixed code the product sanitizes the TERM it hands Codex (or launches
  Codex non-interactively), the fake serves, and ``start()`` succeeds -> this
  test PASSES.

Hermetic: the "CLI" is a fake ``codex`` app-server keyed on ``$TERM`` -- no
real Codex CLI, network, or credentials are needed, only this repo's own
launch code. Because the real Codex CLI and the reported Arca (Databricks
Sandbox) host are unavailable in CI, this is a best-effort reproduction of the
likely mechanism against a faithful stand-in; the reproduced symptom (Codex
refusing under a dumb TERM handed to it by the product) is the invariant a fix
must clear.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="native Codex app-server launch + TERM handling is POSIX-only",
)

# Faithful fake Codex CLI. Models the real vendor CLI under a dumb/unset TERM:
# the app-server subcommand refuses to start non-interactively (prints the
# reported confirmation, declines on EOF, exits without serving), and serves a
# minimal app-server (answers ``initialize``) under a sane TERM.
_FAKE_CODEX_TEMPLATE = """#!{python}
'''Fake Codex CLI modelling the TERM=dumb interactive confirmation.'''
import asyncio
import json
import os
import sys

WARNING = (
    'WARNING: TERM is set to \"dumb\". '
    \"Codex's interactive TUI may not work in this terminal.\"
)


def _term_is_dumb():
    term = os.environ.get(\"TERM\", \"\")
    return term == \"\" or term == \"dumb\"


async def _serve(listen_url):
    import websockets

    host, _, port = listen_url.removeprefix(\"ws://\").partition(\":\")

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if \"id\" not in msg:
                continue  # notification (e.g. \"initialized\")
            if msg.get(\"method\") == \"initialize\":
                result = {{\"serverInfo\": {{\"name\": \"fake-codex\",
                                           \"version\": \"0.140.0\"}}}}
            else:
                result = {{}}
            await ws.send(json.dumps({{\"id\": msg[\"id\"], \"result\": result}}))

    async with websockets.serve(handler, host, int(port)):
        await asyncio.Future()


def main():
    args = sys.argv[1:]
    if \"--version\" in args:
        print(\"codex-cli 0.140.0\")
        return 0
    if args and args[0] == \"app-server\" and \"--listen\" in args:
        if _term_is_dumb():
            # The reported TUI-only confirmation. Codex cannot read a \"yes\"
            # from a non-interactive stdin, so it declines and never serves.
            sys.stderr.write(WARNING + \"\\n\")
            sys.stderr.write(\"Continue anyway? [y/N]: \")
            sys.stderr.flush()
            sys.stdin.readline()  # EOF on DEVNULL -> declined
            sys.stderr.write(\"\\nDeclined; exiting.\\n\")
            sys.stderr.flush()
            return 1
        asyncio.run(_serve(args[args.index(\"--listen\") + 1]))
        return 0
    return 2


if __name__ == \"__main__\":
    raise SystemExit(main())
"""

# The exact warning fragment the real Codex CLI prints and the reporter saw.
_TERM_WARNING_FRAGMENT = 'TERM is set to "dumb"'
_CONTINUE_PROMPT_FRAGMENT = "Continue anyway?"


def _write_fake_codex(path: Path) -> None:
    """Write the executable fake ``codex`` CLI at *path*."""
    path.write_text(_FAKE_CODEX_TEMPLATE.format(python=sys.executable))
    path.chmod(0o755)


def _free_port() -> int:
    """Return an ephemeral loopback TCP port for the app-server listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _start_codex_app_server_under_term(codex_path: str, root: Path) -> str | None:
    """Drive the runner's real Codex app-server launch; return the failure, if any.

    Builds the app-server exactly as the runner's native codex orchestration
    does (``build_codex_native_server`` with a cli-config-style
    ``model_provider`` override so no credentials are needed), then runs the
    real ``CodexNativeAppServer.start()``. The process ``TERM`` is whatever the
    caller set, so the product's env-building decides what TERM Codex runs
    under.

    :param codex_path: Path to the fake ``codex`` CLI.
    :param root: Sandbox dir for the private CODEX_HOME / bridge / workspace.
    :returns: ``None`` when the app-server started, else the error string
        (the reproduction on buggy code).
    """
    from omnigent.harnesses.codex_native.app_server import build_codex_native_server

    app = build_codex_native_server(
        socket_path=root / "codex.sock",
        codex_home=root / "codex-home",
        cwd=root,
        model=None,
        profile=None,
        bridge_dir=root / "bridge",
        codex_path=codex_path,
        # A cli-config codex provider pins only a model_provider name (the
        # "codex-databricks via cli-config" launch the ticket reports); it
        # needs no credentials to build, so the launch routes without a login.
        extra_config_overrides=['model_provider="databricks"'],
    )
    app.listen_url = f"ws://127.0.0.1:{_free_port()}"
    # The product hands the codex app-server subprocess this exact env; on
    # buggy code it still carries the host's TERM=dumb.
    handed_term = app.env.get("TERM")
    try:
        await app.start()
    except BaseException as exc:  # noqa: BLE001 - surface any startup failure
        return f"{type(exc).__name__}: {exc} (env TERM handed to codex={handed_term!r})"
    finally:
        try:
            await app.close()
        except Exception:  # noqa: BLE001 - teardown best-effort
            pass
    return None


@pytest.mark.timeout(120)
def test_codex_app_server_starts_when_host_term_is_dumb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host under ``TERM=dumb`` must still start the native Codex app-server.

    Journey: a Codex session launches on a host whose daemon inherited a
    non-interactive ``TERM=dumb`` -> the runner starts the Codex app-server ->
    it must come up (Codex must not block on the TUI-only TERM confirmation).

    Buggy code passes ``TERM=dumb`` through to Codex, which refuses
    non-interactively and never starts -> this assertion fails (the
    reproduction). A fix that sanitizes the TERM handed to Codex (or launches it
    non-interactively) makes the app-server start -> this passes.
    """
    # Reproduce the reported host condition: the daemon runs under a dumb TERM.
    monkeypatch.setenv("TERM", "dumb")
    # Keep the private CODEX_HOME population hermetic (no dependence on the
    # runner's real ~/.codex).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    _write_fake_codex(codex)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    failure = asyncio.run(_start_codex_app_server_under_term(str(codex), workspace))

    assert failure is None, (
        "Native Codex app-server did not start under TERM=dumb: the runner "
        "handed the host's dumb TERM straight through to `codex app-server`, "
        "which blocked on its TUI-only TERM confirmation and exited without "
        "serving -- so no thread starts and the session ends. "
        f"Startup failure: {failure}"
    )


@pytest.mark.timeout(120)
def test_codex_app_server_startup_control_sane_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the same launch path succeeds under a sane TERM.

    Isolates ``TERM=dumb`` as the cause -- with a normal terminal the fake
    Codex serves and the product's ``start()`` completes, so a failure of the
    dumb-TERM test above is specifically about the TERM Codex is handed, not
    about the launch path or the fake being unusable.
    """
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    _write_fake_codex(codex)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    failure = asyncio.run(_start_codex_app_server_under_term(str(codex), workspace))

    assert failure is None, (
        "Control failed: the native Codex app-server should start under a "
        f"sane TERM, but startup raised: {failure}"
    )
