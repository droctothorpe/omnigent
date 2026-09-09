"""E2E regression test: host daemon stuck in a 403 loop must heal on re-auth.

Reproduces the reported failure: ``omnigent host`` runs against a
Databricks-fronted server whose edge authenticates the tunnel upgrade by
bearer token. When the stored credential goes stale, every reconnect is
rejected with HTTP 403 (``Connection refused (HTTP 403): the host tunnel
was rejected. Retrying — check your VPN/network.``). The user then
re-authenticates (``databricks auth login`` writes a fresh credential to
disk) — but the running daemon keeps dialing with the stale in-process
credential forever, so the 403 loop never ends and the host never comes
back online without a manual restart.

The test stands up a bearer-checking TCP proxy (a Databricks-edge
stand-in) in front of the e2e ``live_server``, connects a real host
daemon THROUGH the proxy using a credential stored in ``~/.databrickscfg``,
rotates the credential the edge accepts (severing the live tunnel so the
daemon reconnects into the 403), then performs the user's re-auth by
writing the fresh credential to ``~/.databrickscfg`` — and asserts the
daemon picks it up and the host comes back online without a restart.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_host_tunnel_403_reauth.py -v
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S

# The edge's refusal for a stale/absent bearer on the tunnel upgrade.
_403_RESPONSE = (
    b"HTTP/1.1 403 Forbidden\r\n"
    b"content-type: text/plain\r\n"
    b"content-length: 9\r\n"
    b"connection: close\r\n"
    b"\r\n"
    b"forbidden"
)

# Cap on the HTTP request head read while sniffing the upgrade request.
_MAX_HEAD_BYTES = 64 * 1024

# How long the daemon gets to pick up the refreshed credential and bring
# the host back online. The auth-reject retry cadence is ~3s, so this
# allows well over a dozen reconnect attempts.
_REAUTH_RECOVERY_TIMEOUT_S = 60.0


class _BearerCheckingProxy:
    """A Databricks-edge stand-in: authenticates tunnel upgrades by bearer.

    Every accepted connection has its HTTP request head sniffed. A request
    for the host tunnel route (path contains ``/tunnel``) must carry
    ``Authorization: Bearer <required>`` or it is refused with a bare HTTP
    403 — exactly how the workspace edge rejects a stale or absent
    credential before the request reaches the app. Everything else (and an
    authorized tunnel upgrade) is piped byte-for-byte to the backend.
    """

    def __init__(self, backend_host: str, backend_port: int, required_bearer: str) -> None:
        self._backend = (backend_host, backend_port)
        self._required_bearer = required_bearer
        self._live_socks: set[socket.socket] = set()
        self._lock = threading.Lock()
        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _read_head(self, client: socket.socket) -> bytes:
        """Read the HTTP request head (through ``\\r\\n\\r\\n``) from *client*."""
        head = b""
        client.settimeout(10.0)
        while b"\r\n\r\n" not in head and len(head) < _MAX_HEAD_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                break
            head += chunk
        return head

    def _authorized(self, head: bytes) -> bool:
        """True when *head* is not a tunnel upgrade or carries the live bearer."""
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if "/tunnel" not in request_line:
            return True
        with self._lock:
            required = self._required_bearer
        expected = f"bearer {required}".encode()
        for raw_line in head.split(b"\r\n")[1:]:
            name, _, value = raw_line.partition(b":")
            if name.strip().lower() == b"authorization" and value.strip().lower() == expected:
                return True
        return False

    def _handle(self, client: socket.socket) -> None:
        try:
            head = self._read_head(client)
        except OSError:
            client.close()
            return
        if not head:
            client.close()
            return
        if not self._authorized(head):
            # Stale/absent bearer on the tunnel upgrade: the edge refuses
            # with 403 before the request reaches the app.
            try:
                client.sendall(_403_RESPONSE)
            except OSError:
                pass
            finally:
                client.close()
            return
        try:
            backend = socket.create_connection(self._backend, timeout=10.0)
            backend.sendall(head)
        except OSError:
            client.close()
            return
        client.settimeout(None)
        with self._lock:
            self._live_socks.update((client, backend))
        try:
            t1 = threading.Thread(target=self._pipe, args=(client, backend), daemon=True)
            t2 = threading.Thread(target=self._pipe, args=(backend, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            with self._lock:
                self._live_socks.discard(client)
                self._live_socks.discard(backend)
            for sock in (client, backend):
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            # Either peer closing during the simulated credential rotation
            # tears the pipe down; the caller closes both ends.
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    def rotate_bearer(self, new_bearer: str) -> None:
        """The stored credential lapses: only *new_bearer* authenticates now.

        Severs live piped connections so the daemon's tunnel drops and its
        reconnect dials into the 403 (the edge no longer accepts the old
        bearer).
        """
        with self._lock:
            self._required_bearer = new_bearer
            socks = list(self._live_socks)
        for sock in socks:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _write_databrickscfg(home: Path, token: str) -> None:
    """Store *token* the way ``databricks auth login`` leaves a credential.

    The daemon resolves its tunnel bearer from the ambient Databricks
    credential chain (``~/.databrickscfg``), so rewriting this file with a
    fresh token is the on-disk effect of the user re-authenticating.
    """
    (home / ".databrickscfg").write_text(
        "[DEFAULT]\nhost = https://dbc-e2e-fake.cloud.databricks.com\n" f"token = {token}\n"
    )


def _spawn_host_daemon_via(
    *,
    tmp_path: Path,
    server_url: str,
    mock_llm_server_url: str,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn an isolated host daemon pointed at *server_url*.

    Mirrors ``tests/e2e/test_host_transient_404_restart.py``'s spawner. The
    daemon's ``HOME`` is *tmp_path*, so its only credential source is the
    ``.databrickscfg`` the test writes there; ambient ``DATABRICKS_*`` env
    from the CI machine is scrubbed so it cannot shadow the file.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param server_url: URL the daemon registers with — the PROXY address.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: ``(proc, host_id, daemon_log)``.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    # The credential under test lives in the tmp-HOME .databrickscfg; drop
    # any real Databricks credentials from the CI environment so the SDK's
    # env-based resolution cannot shadow the file.
    for key in list(env):
        if key.startswith("DATABRICKS_"):
            del env[key]
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", server_url],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _host_online(client: httpx.Client, host_id: str) -> bool:
    """True when *host_id* is registered and online on the server."""
    resp = client.get("/v1/hosts")
    if resp.status_code != 200:
        return False
    return any(
        h["host_id"] == host_id and h["status"] == "online" for h in resp.json().get("hosts", [])
    )


def _wait_for_host_online(
    client: httpx.Client,
    host_id: str,
    timeout: float = 30.0,
) -> None:
    """Poll GET /v1/hosts until *host_id* appears online.

    :param client: HTTP client pointed at the (direct) server.
    :param host_id: Host ID to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _host_online(client, host_id):
                return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


@pytest.mark.timeout(300)
def test_host_recovers_from_403_after_reauth(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A host stuck in the 403 loop must recover once the user re-authenticates.

    Journey (from the bug report): authenticate and start ``omnigent host``
    against a Databricks-fronted server → the stored credential lapses at
    the edge → every tunnel reconnect is rejected with HTTP 403 and the
    daemon loops (``Retrying — check your VPN/network``) → the user
    re-authenticates (``databricks auth login`` stores a fresh credential).
    Expected: a subsequent reconnect dials with the fresh credential and
    the host comes back online. Actual (bug): the daemon keeps dialing
    with the stale in-process credential and fails with 403 forever.
    """
    parsed = urlparse(live_server)
    assert parsed.hostname is not None and parsed.port is not None
    proxy = _BearerCheckingProxy(parsed.hostname, parsed.port, required_bearer="token-v1")
    proc: subprocess.Popen[bytes] | None = None
    try:
        proxy_url = f"http://127.0.0.1:{proxy.port}"

        # Sanity: the server is reachable through the proxy, like a user URL.
        assert httpx.get(f"{proxy_url}/health", timeout=10.0).status_code == 200

        # The user authenticated: the stored credential the daemon dials
        # with, and the one the edge currently accepts.
        _write_databrickscfg(tmp_path, "token-v1")

        proc, host_id, daemon_log = _spawn_host_daemon_via(
            tmp_path=tmp_path,
            server_url=proxy_url,
            mock_llm_server_url=mock_llm_server_url,
        )
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        # The stored credential lapses: the edge now only accepts the
        # rotated token, and the live tunnel is severed so the daemon
        # reconnects into the 403.
        proxy.rotate_bearer("token-v2")

        # The daemon must actually hit the rejected-credential path: its
        # log records the 403 refusal.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if "Connection refused (HTTP 403)" in daemon_log.read_text():
                break
            assert proc.poll() is None, (
                f"Host daemon exited (code {proc.poll()}) while its credential was "
                f"rejected (HTTP 403) instead of retrying. Daemon log tail:\n"
                f"{daemon_log.read_text()[-2000:]}"
            )
            time.sleep(POLL_INTERVAL_S)
        else:
            raise AssertionError(
                "Daemon log never recorded the 403 refusal — the test did not "
                f"exercise the rejected-credential path. Log tail:\n"
                f"{daemon_log.read_text()[-2000:]}"
            )

        # The user re-authenticates: `databricks auth login` stores the
        # fresh credential the edge accepts.
        _write_databrickscfg(tmp_path, "token-v2")

        # The running daemon must pick up the refreshed credential on a
        # subsequent reconnect and bring the host back online — without a
        # restart. The bug: the stale credential is latched in-process,
        # so the 403 loop never ends.
        deadline = time.monotonic() + _REAUTH_RECOVERY_TIMEOUT_S
        while time.monotonic() < deadline:
            rc = proc.poll()
            assert rc is None, (
                f"Host daemon exited with code {rc} instead of recovering after "
                f"the user re-authenticated. Daemon log tail:\n"
                f"{daemon_log.read_text()[-2000:]}"
            )
            if _host_online(http_client, host_id):
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            raise AssertionError(
                "Host daemon never recovered from the 403 loop after "
                f"the user re-authenticated — {_REAUTH_RECOVERY_TIMEOUT_S:.0f}s "
                "after the fresh credential was stored, the daemon was still "
                "dialing the tunnel with the stale in-process credential and "
                "being rejected with HTTP 403 (`Retrying — check your "
                "VPN/network`). Restarting the daemon should not be required. "
                f"Daemon log tail:\n{daemon_log.read_text()[-2000:]}"
            )
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        proxy.close()
