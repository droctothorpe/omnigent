"""E2E: ``/mcp`` typed into the web composer must surface MCP status.

Guards the regression where the web composer had no ``/mcp`` built-in
(``BUILTIN_SLASH_COMMANDS``): a drafted ``/mcp`` matched no built-in and
no skill, so the submit path fell through to the plaintext send. The
literal text ``/mcp`` was dispatched to the LLM as an ordinary chat
message: the user saw their command land as a chat bubble and the model
reply to it as if it were prose, and no MCP server status of any kind
appeared. (Native Claude sessions were no better off — the bridge
deliberately escapes ``mcp`` as a dropped CLI command.)

Journey (all user-observable):

1. Start a web session on an agent whose tools include a live MCP
   server (an ``echo`` tool over streamable HTTP, named ``echomcp``).
2. Type ``/mcp`` into the composer and send it.
3. Expected: MCP status naming the configured server (``echomcp``)
   appears, and the command is NOT sent to the agent as chat text.
   On the broken build: no MCP feedback ever appears — instead a user
   chat bubble ``/mcp`` renders and the model replies to the raw text.

The mock LLM scripts the (bug-path) reply so the broken behavior is
deterministic; on a fixed build no model turn should happen at all.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _find_free_port,
    _server_state,
    configure_mock_llm,
)

# Reply the mock returns if (and only if) the broken build leaks the
# command to the model as chat text. Deliberately free of the server
# name so it can never satisfy the MCP-status assertion.
_LEAKED_COMMAND_REPLY = "I received your message as plain text."

# Minimal streamable-HTTP MCP server exposing one ``echo`` tool, run as
# a subprocess so the agent under test has a real, connected MCP server
# (the harness lists its tools when building the turn's LLM request).
_MCP_SERVER_CODE = """
import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-http", host="127.0.0.1", port=int(sys.argv[1]))

@mcp.tool()
def echo(text: str) -> str:
    return f"echo: {text}"

mcp.run(transport="streamable-http")
"""

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a terse assistant. Answer in one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

tools:
  echomcp:
    type: mcp
    url: http://127.0.0.1:{port}/mcp
"""


def _start_mcp_server(port: int, timeout_s: float = 30.0) -> subprocess.Popen[bytes]:
    """Start the echo MCP server subprocess and wait until it serves /mcp.

    Readiness is a real ``initialize`` POST (``trust_env=False`` so an
    ambient CI proxy can't intercept the loopback probe), not a fixed
    sleep — uvicorn boot time varies under CI load.

    :param port: Loopback port to serve on.
    :param timeout_s: Max seconds to wait for readiness.
    :returns: The running server process.
    :raises RuntimeError: If the server does not become ready in time.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _MCP_SERVER_CODE, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "readiness-probe", "version": "0"},
        },
    }
    deadline = time.monotonic() + timeout_s
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"MCP server exited early (code {proc.returncode})")
            try:
                resp = client.post(
                    f"http://127.0.0.1:{port}/mcp",
                    json=init_body,
                    headers={"Accept": "application/json, text/event-stream"},
                    timeout=2.0,
                )
                if resp.status_code == 200:
                    return proc
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"MCP server on port {port} not ready within {timeout_s:.0f}s")


def _stop_mcp_server(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the MCP server process, escalating to SIGKILL."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_mcp_slash_command_shows_mcp_status(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: object,
) -> None:
    """Sending ``/mcp`` must yield MCP status, not a plain chat turn.

    The primary assertion is the fixed behavior: after submitting
    ``/mcp``, the configured MCP server's name (``echomcp``) becomes
    visible somewhere on the page (inline command feedback, a status
    card — any user-visible MCP listing satisfies it). The secondary
    assertion pins the leak the broken build exhibits: the command must
    never render as a user chat bubble (i.e. it was not dispatched to
    the model as plain text).

    On the broken build the first assertion times out while the page
    shows exactly the reported failure: a ``/mcp`` user bubble and the
    model's reply to the raw text.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    :param mock_llm_server_url: Mock LLM server the runner routes to.
    :param tmp_path_factory: Pytest temp path factory (runner respawn log).
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    port = _find_free_port()
    mcp_proc = _start_mcp_server(port)
    session_id: str | None = None
    try:
        model = f"mcp-slash-{uuid.uuid4().hex[:8]}"
        # Fires only if the broken build leaks "/mcp" to the model as
        # user text; a fixed build never makes this LLM call.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _LEAKED_COMMAND_REPLY}],
            key=model,
            match="/mcp",
        )

        yaml_text = _AGENT_YAML.format(
            name=f"mcp-slash-probe-{uuid.uuid4().hex[:6]}",
            model=model,
            port=port,
        )
        session_id = _create_bundled_session(live_server, runner_id, yaml_text)

        page.goto(f"{live_server}/c/{session_id}")

        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill("/mcp")
        page.get_by_role("button", name="Send", exact=True).click()

        # Fixed behavior: MCP status naming the configured server appears.
        # Broken behavior: this never renders — the command was sent to the
        # model as plain chat text instead (visible as bubbles below).
        expect(page.get_by_text("echomcp").first).to_be_visible(timeout=20_000)

        # And the command must not have leaked into the transcript as an
        # ordinary user message.
        expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(0)
    finally:
        _stop_mcp_server(mcp_proc)
        if session_id is not None:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)
