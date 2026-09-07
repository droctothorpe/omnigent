"""E2E: code blocks keep rendering through a highlighted-body chunk outage.

Streamdown mounts each fenced code block's body behind ``React.lazy(() =>
import('./highlighted-body-<hash>.js'))`` — a facade chunk that only re-exports
a component the static bundle already carries. Fetching that facade at render
time meant a failed fetch (a tab held open across a redeploy whose old hashed
chunk no longer exists, or a passing network blip) degraded the streamed
message to the ``MarkdownErrorBoundary`` fallback ("Could not render this
markdown.") with its raw source — and because ``React.lazy`` caches a rejected
import forever, every later code block degraded too, until a full page reload.
The markdown itself was always valid: a refresh rendered it fine.

The facade is pinned into the static bundle by
``web/src/components/ai-elements/eagerHighlightedBodyFacade.ts``, so rendering
a code block never fetches a highlighted-body chunk: a message arriving while
the chunk endpoint is failing must render its highlighted body (via the
independent Shiki path), and so must every code block after the outage passes.
The route-abort below is the faithful stand-in for the redeploy / blip.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, Route, expect

_AGENT_NAME = "hello_world"
# Every emitted highlighted-body chunk, so this keeps matching across rebuilds
# (the hashes change on every build).
_HIGHLIGHT_CHUNKS = "**/highlighted-body-*.js"
_FALLBACK_TEXT = "Could not render this markdown."
_CODE_BODY = '[data-streamdown="code-block-body"]'
# Streamdown emits one span per highlighted token, each carrying its Shiki
# color via the `--sdm-c` custom property; their presence proves the code
# block rendered its real highlighted body rather than degrading away.
_TOKEN_SPANS = f'{_CODE_BODY} span[style*="--sdm-c"]'

_FIRST_MARKER = "First snippet arrives during the network blip."
_SECOND_MARKER = "Second snippet arrives on a healthy network."


def _code_message(marker: str, const_name: str) -> str:
    """A fenced ``ts`` block with an identifying line of prose above it."""
    return f"{marker}\n\n```ts\nconst {const_name} = 42;\n```\n"


def _post_assistant_message(base_url: str, session_id: str, response_id: str, text: str) -> None:
    """Deliver one assistant message to the session (arrives live over SSE)."""
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "response_id": response_id, "text": text},
        },
        timeout=10.0,
    ).raise_for_status()


def test_code_blocks_render_during_and_after_chunk_outage(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Code-block messages render while highlighted-body chunks are unfetchable.

    On the unfixed tree a single aborted highlighted-body fetch latches the
    whole message to "Could not render this markdown." and, because React.lazy
    caches the rejection, poisons every later code block for the session (no
    re-fetch is ever attempted). The fix pins the facade into the static entry
    graph, so no highlighted-body chunk is fetched at render time at all.
    """
    base_url, session_id = seeded_session

    # Mid-session: the page is open and healthy before anything fails.
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)

    # The blip: no highlighted-body chunk can be fetched from here on.
    page.route(_HIGHLIGHT_CHUNKS, lambda route: Route.abort(route, "failed"))
    _post_assistant_message(base_url, session_id, "resp_code_blip", _code_message(_FIRST_MARKER, "first"))

    # The message content lands either way — as rendered markdown on a healthy
    # tree, or as the fallback's raw source on the unfixed one — so wait on the
    # prose marker before judging how it rendered.
    expect(page.get_by_text(_FIRST_MARKER)).to_be_visible(timeout=30_000)

    # The bug: the aborted chunk latches the whole message to the fallback.
    # Fixed: no highlighted-body chunk is fetched, so the valid message never
    # degrades, its code block renders, and highlighting completes.
    expect(page.get_by_text(_FALLBACK_TEXT)).to_have_count(0)
    expect(page.locator(_CODE_BODY).first).to_be_visible(timeout=30_000)
    expect(page.locator(_TOKEN_SPANS).first).to_be_visible(timeout=30_000)

    # The blip passes, the user keeps chatting in the same tab: later code
    # blocks must render too (no cached rejection poisoning the session).
    page.unroute(_HIGHLIGHT_CHUNKS)
    _post_assistant_message(
        base_url, session_id, "resp_code_healthy", _code_message(_SECOND_MARKER, "second")
    )

    expect(page.get_by_text(_SECOND_MARKER)).to_be_visible(timeout=30_000)
    expect(page.locator(_CODE_BODY)).to_have_count(2, timeout=30_000)
    expect(page.locator(_TOKEN_SPANS).nth(1)).to_be_visible(timeout=30_000)

    # The session never saw the degraded state at all.
    expect(page.get_by_text(_FALLBACK_TEXT)).to_have_count(0)
