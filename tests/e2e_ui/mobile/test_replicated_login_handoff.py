"""E2E: the iOS system-browser login handoff on a managed (replicated) server.

The reported trap's second symptom: connecting the iOS app to a managed omnigent
URL "redirects to browser for login, then in the app it's just stuck at login
page forever". The redirect to the system browser is the intended flow — the
shell (``web/ios/Omnigent/OidcLoginManager.swift``) POSTs ``/auth/cli-login``,
opens the returned ``login_url`` in the system browser, and polls
``/auth/cli-poll`` for the session token to install in its WebView. What must
never happen is the second half: the browser login *succeeds* but the app's
poll never yields the token, so the app silently stays unauthenticated.

That is exactly what a **replicated** deployment does to this flow today:
``_cli_tickets`` (``omnigent/server/routes/auth.py``) is a process-local dict.
The OIDC ``state`` — and the ticket id threaded through it — ride in a signed
cookie (shared ``cookie_secret``), so the *browser* leg works against any
replica; but ticket fulfillment at ``/auth/callback`` only happens on the one
replica that minted the ticket, and the app's cookieless polls land on
arbitrary replicas — ``410 Gone`` — which the shell treats as terminal and
gives up on *silently* (``OidcLoginManager.pollForToken`` returns nil). Net:
sign-in completes in the browser, the app is stuck at the login page forever,
with no error anywhere. (The managed deployment is replicated — the SPA even
ships replica slice-key routing, see ``web/src/lib/identity.ts``.)

Playwright cannot execute UIKit or ``WKNavigationDelegate`` on Linux CI (the
same boundary as ``test_ios_oidc_handoff.py``), so this test reproduces the
failure at the production contract boundary, against a faithful managed
topology: TWO real ``omnigent server`` replicas sharing one database, one
cookie secret and one fake IdP, behind a round-robin reverse proxy (a
connectionless LB — the app's ticket requests carry no cookies, so nothing
pins them to a replica). It then drives the app's exact client contract:

1. ``POST /auth/cli-login`` through the LB (the shell's ticket request),
2. the returned ``login_url`` in the browser through the LB (the system-browser
   leg), completing the IdP sign-in — this *succeeds* in the browser,
3. ``GET /auth/cli-poll`` through the LB with the shell's own semantics
   (200 → token, 202 → keep polling, 410 → terminal give-up).

Before the fix the poll never returned the session token (410 from any replica
that didn't mint the ticket, eternal 202 from the one that did), so this test
failed — the reproduction of the "stuck at login page forever" facet. A fix
that makes ticket fulfillment replica-safe (a shared/DB-backed or stateless
ticket) turns the poll into a 200 and this test green.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.auth._accounts_server import _terminate, public_loopback_url
from tests.e2e_ui.auth._fake_idp import FakeIdP, fake_idp
from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
)

# How long the app-side poll keeps trying before the test calls the handoff
# dead. The real shell polls for 300 s; a shared-ticket fix answers within a
# poll or two, so a short window keeps the failing run fast while leaving
# generous slack for a passing one.
_POLL_WINDOW_S = 20.0
_POLL_INTERVAL_S = 0.5

# Hop-by-hop / recomputed headers the proxy must not forward verbatim.
_STRIP_REQUEST_HEADERS = {"host", "connection", "keep-alive", "accept-encoding", "content-length"}
_STRIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}


class _RoundRobinProxy:
    """A minimal connectionless round-robin HTTP load balancer.

    Forwards each request — independently, exactly like an LB with no session
    affinity — to the next backend in turn. Requests are proxied with
    ``trust_env=False`` so the CI egress proxy can never intercept loopback
    traffic, and redirects are passed through untouched (the browser must see
    the IdP 302 itself).
    """

    def __init__(self, backend_ports: list[int]) -> None:
        self._rr = itertools.cycle(range(len(backend_ports)))
        self._rr_lock = threading.Lock()
        self._client = httpx.Client(
            trust_env=False, timeout=httpx.Timeout(30.0, read=15.0), follow_redirects=False
        )
        backend_bases = [f"http://127.0.0.1:{port}" for port in backend_ports]
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            # One response per connection: no keep-alive bookkeeping to get
            # wrong, and Chromium is happy to reconnect.
            protocol_version = "HTTP/1.0"

            def log_message(self, format: str, *args: object) -> None:
                pass  # keep pytest output readable

            def _forward(self) -> None:
                with proxy._rr_lock:
                    backend = backend_bases[next(proxy._rr)]
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in _STRIP_REQUEST_HEADERS
                }
                # Preserve the browser-facing Host so the backend sees the
                # public origin; ask the backend for identity encoding so the
                # bytes we relay match the lengths we compute.
                headers["Host"] = self.headers.get("Host", "")
                headers["Accept-Encoding"] = "identity"
                try:
                    resp = proxy._client.request(
                        self.command, backend + self.path, content=body, headers=headers
                    )
                except httpx.HTTPError:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = resp.content
                self.send_response(resp.status_code)
                for key, value in resp.headers.multi_items():
                    if key.lower() in _STRIP_RESPONSE_HEADERS:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                # A BrokenPipeError means the browser gave up on the
                # response; nothing to relay.
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(payload)

            do_GET = _forward
            do_POST = _forward
            do_HEAD = _forward
            do_PUT = _forward
            do_PATCH = _forward
            do_DELETE = _forward
            do_OPTIONS = _forward

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True  # an SSE stream must not block shutdown
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._client.close()


@dataclass
class ReplicatedOIDCServer:
    """Two OIDC-mode replicas of one deployment behind a round-robin LB.

    :param base_url: The LB on loopback (``http://127.0.0.1:<port>``) — the
        app-leg base URL, what the shell's ``URLSession`` would call.
    :param public_url: The LB via the public-looking loopback alias — the
        browser-leg base URL.
    :param idp: The fake IdP both replicas authenticate against.
    """

    base_url: str
    public_url: str
    idp: FakeIdP


def _spawn_replicated_oidc_server(
    mock_llm_server_url: str, server_tmp
) -> Iterator[ReplicatedOIDCServer]:
    """Spawn a two-replica OIDC deployment behind a round-robin LB; yield a handle.

    Faithful to a managed deployment: the replicas share the database, the
    session/state ``cookie_secret``, the IdP client and the public redirect
    URI (the LB origin); only process memory — where ``_cli_tickets`` lives —
    is per-replica, which is precisely the property under test. Replicas boot
    sequentially so schema migration on the shared database runs once, settled.
    """
    with fake_idp() as idp:
        replica_ports = [_find_free_port(), _find_free_port()]
        cookie_secret = secrets.token_hex(32)
        db_path = server_tmp / "shared.db"
        agent_yaml_path = server_tmp / "hello_world.yaml"
        agent_yaml_path.write_text(_TEST_AGENT_YAML)
        pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

        proxy = _RoundRobinProxy(replica_ports)
        base_url = f"http://127.0.0.1:{proxy.port}"
        public_url = public_loopback_url(base_url)
        redirect_uri = f"{public_url}/auth/callback"

        procs: list[subprocess.Popen[bytes]] = []
        log_handles = []
        try:
            for index, port in enumerate(replica_ports):
                artifact_dir = server_tmp / f"artifacts-{index}"
                artifact_dir.mkdir(parents=True, exist_ok=True)
                log_path = server_tmp / f"replica-{index}.log"
                server_env = {
                    **os.environ,
                    "PYTHONPATH": pythonpath,
                    "OMNIGENT_AUTH_PROVIDER": "oidc",
                    "OMNIGENT_AUTH_ENABLED": "1",
                    "OMNIGENT_LOCAL_SINGLE_USER": "",
                    "OMNIGENT_OIDC_ISSUER": idp.issuer,
                    "OMNIGENT_OIDC_CLIENT_ID": idp.client_id,
                    "OMNIGENT_OIDC_CLIENT_SECRET": idp.client_secret,
                    "OMNIGENT_OIDC_REDIRECT_URI": redirect_uri,
                    "OMNIGENT_OIDC_COOKIE_SECRET": cookie_secret,
                    "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
                    "OPENAI_API_KEY": "mock-key",
                    "ANTHROPIC_API_KEY": "",
                }
                log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen
                log_handles.append(log_handle)
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from omnigent.cli import main; main()",
                        "server",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--database-uri",
                        f"sqlite:///{db_path}",
                        "--artifact-location",
                        str(artifact_dir),
                        "--agent",
                        str(agent_yaml_path),
                    ],
                    env=server_env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                procs.append(proc)

                deadline = time.monotonic() + _HEALTH_TIMEOUT_S
                ready = False
                last_error = "not polled yet"
                probe = httpx.Client(trust_env=False)
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        last_error = f"replica exited early with code {proc.returncode}"
                        break
                    try:
                        health = probe.get(f"http://127.0.0.1:{port}/health", timeout=2)
                        if health.status_code == 200:
                            ready = True
                            break
                    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                    time.sleep(_HEALTH_POLL_INTERVAL_S)
                probe.close()
                if not ready:
                    log_handle.flush()
                    log_text = log_path.read_text() if log_path.exists() else ""
                    raise RuntimeError(
                        f"OIDC replica {index} not healthy within {_HEALTH_TIMEOUT_S:.0f}s "
                        f"(last_error={last_error}).\n{log_text[-3000:]}"
                    )

            yield ReplicatedOIDCServer(base_url=base_url, public_url=public_url, idp=idp)
        finally:
            proxy.close()
            for proc in procs:
                _terminate(proc)
            for log_handle in log_handles:
                log_handle.close()


@pytest.fixture(scope="module")
def replicated_oidc_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ReplicatedOIDCServer]:
    """A two-replica OIDC deployment behind a round-robin LB."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_managed_handoff")
    yield from _spawn_replicated_oidc_server(mock_llm_server_url, server_tmp)


def _poll_like_the_shell(base_url: str, ticket: str) -> tuple[str | None, list[int]]:
    """Poll ``/auth/cli-poll`` with the iOS shell's exact semantics.

    Mirrors ``OidcLoginManager.pollForToken``: 200 returns the token, 202 keeps
    polling, 410 is terminal — the shell returns nil and gives up silently.

    :param base_url: The LB base URL (the app leg).
    :param ticket: The ticket id from ``POST /auth/cli-login``.
    :returns: ``(token or None, observed status codes)``.
    """
    outcomes: list[int] = []
    deadline = time.monotonic() + _POLL_WINDOW_S
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_S)
            resp = client.get(f"{base_url}/auth/cli-poll", params={"ticket": ticket}, timeout=10)
            outcomes.append(resp.status_code)
            if resp.status_code == 200:
                token = resp.json().get("token")
                return (str(token) if token else None), outcomes
            if resp.status_code == 410:
                return None, outcomes  # the shell's terminal, silent give-up
    return None, outcomes


def test_browser_login_hands_the_session_back_to_the_app_through_the_lb(
    page: Page,
    replicated_oidc_server: ReplicatedOIDCServer,
) -> None:
    """Completing the system-browser sign-in must yield the app its session.

    Drives the reported journey at the contract boundary: the app requests a
    login ticket through the LB, the system browser completes the real OIDC
    sign-in through the LB (this succeeds — the browser lands authenticated),
    and the app polls for its session token exactly as the shell does. Before
    the fix the token never arrived (the process-local ticket is invisible to
    the other replica), which is the reported "stuck at login page forever".

    :param page: Playwright page fixture — the system-browser leg.
    :param replicated_oidc_server: The two-replica deployment behind the LB.
    :returns: None.
    """
    server = replicated_oidc_server

    # 1. The app leg: the shell's ticket request (URLSession — no cookies, no
    #    affinity), exactly OidcLoginManager.requestTicket.
    with httpx.Client(trust_env=False) as client:
        ticket_resp = client.post(f"{server.base_url}/auth/cli-login", timeout=10)
    assert ticket_resp.status_code == 200, (
        f"POST /auth/cli-login through the LB must return a ticket; got "
        f"{ticket_resp.status_code}: {ticket_resp.text[:200]}"
    )
    ticket_body = ticket_resp.json()
    ticket = ticket_body["ticket"]
    login_url = ticket_body["login_url"]
    assert login_url.startswith("/"), f"login_url must be same-origin relative: {login_url!r}"

    # 2. The system-browser leg: open the ticket login URL, exactly what the
    #    shell hands to the system browser. The LB routes it to some replica,
    #    which 302s to the IdP sign-in page.
    page.goto(f"{server.public_url}{login_url}")
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    expect(continue_link).to_contain_text(server.idp.email)

    # 3. The shell starts polling the moment the browser opens; one pending
    #    poll lands while the user is still on the IdP page (and advances the
    #    LB's round-robin exactly as real interleaved traffic would).
    with httpx.Client(trust_env=False) as client:
        pending = client.get(
            f"{server.base_url}/auth/cli-poll", params={"ticket": ticket}, timeout=10
        )
    assert pending.status_code in (202, 410), (
        f"pre-completion poll should be pending; got {pending.status_code}"
    )

    # 4. The user signs in at the IdP. The callback (routed to whichever
    #    replica the LB picks) validates the cookie-borne state and completes
    #    the browser leg. Which page it shows depends on whether that replica
    #    can see the ticket: a replica that can confirms fulfillment ("Login
    #    successful"); one that cannot falls back to a plain browser login and
    #    lands in the authenticated app. Either way the SIGN-IN succeeds — the
    #    handoff contract is asserted on the app leg below.
    continue_link.click()
    browser_leg_done = page.locator('[data-testid="sidebar-brand"]').or_(
        page.get_by_role("heading", name="Login successful")
    )
    expect(browser_leg_done).to_be_visible(timeout=20_000)

    # 5. The app leg again: poll with the shell's semantics. The browser
    #    handed its half over; the app must now receive the session token —
    #    this is the handoff that leaves the iOS app "stuck at login page
    #    forever" when it never completes.
    token, outcomes = _poll_like_the_shell(server.base_url, ticket)
    assert token, (
        "the browser sign-in completed, but the app's /auth/cli-poll never "
        f"returned the session token (observed poll statuses: {outcomes}). "
        "On a replicated deployment the process-local cli ticket "
        "(_cli_tickets in omnigent/server/routes/auth.py) is fulfilled only "
        "on the replica that minted it and polls landing elsewhere get 410 — "
        "which the iOS shell treats as terminal and gives up on silently, "
        "leaving the app at the login page forever"
    )
