"""E2E: a URL hard-wrapped across lines links to the full destination.

A long URL that arrives with a line break in the middle — pasted from a
terminal or an email that hard-wrapped it, or emitted pre-wrapped by an agent —
renders with only its first line linkified: the anchor's href stops at the
line break, the continuation line is left behind as plain text, and clicking
the link opens the truncated URL. These tests assert the user-expected
behavior (the link covers the whole wrapped URL and clicking opens the full
destination), so they fail until the chat renderer joins a URL continued on
the following line.

The seeded URL is split mid-path (``…/heal`` + ``th?probe=full-dest``) so the
user harm is observable: the full URL is the server's ``/health`` endpoint
(HTTP 200), while the truncated first line alone lands on ``/heal`` — Not
Found. Both chat surfaces are covered because they assemble their markdown
plugin chains separately (assistant text uses Streamdown's defaults; user
bubbles extend them with remark-breaks), so a renderer fix must hold on both.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_COMPOSER_LABEL = "Message the agent"
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'

# Matches the anchor in both states: the truncated href ends in "/heal", and
# the expected full href contains it as a prefix of "/health?…".
_LINK_SELECTOR = 'a[href*="/heal"]'


def _wrapped_url_message(base_url: str) -> tuple[str, str]:
    """Build ``(message_text, full_url)`` with the URL hard-wrapped mid-path.

    Line one (``{base_url}/heal``) is itself a well-formed URL, so the buggy
    renderer still produces a "working" anchor — it just silently drops the
    continuation line, which is exactly the reported failure. The blank line
    after the continuation keeps the full URL unambiguous.
    """
    head = f"{base_url}/heal"
    tail = "th?probe=full-dest"
    full_url = f"{head}{tail}"
    text = f"Your export is ready:\n{head}\n{tail}\n\nOpen it to verify."
    return text, full_url


@pytest.fixture
def wrapped_link_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a deterministic assistant reply containing a hard-wrapped URL."""
    base_url, session_id = seeded_session
    text, full_url = _wrapped_url_message(base_url)
    event_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": text},
        },
        timeout=10.0,
    )
    event_resp.raise_for_status()
    yield (base_url, session_id, full_url)


def test_assistant_wrapped_url_links_to_full_destination(
    page: Page,
    wrapped_link_session: tuple[str, str, str],
) -> None:
    """Clicking an assistant message's wrapped URL opens the full destination."""
    base_url, session_id, full_url = wrapped_link_session
    page.goto(f"{base_url}/c/{session_id}")

    bubble = page.locator(_ASSISTANT_BUBBLE).last
    expect(bubble).to_be_visible(timeout=30_000)
    link = bubble.locator(_LINK_SELECTOR)
    expect(link).to_be_visible(timeout=30_000)

    # The reported bug: the anchor stops at the line break, so the click
    # lands on the truncated first-line URL (/heal → Not Found) instead of
    # the full wrapped destination (/health?probe=full-dest → 200 ok).
    with page.expect_popup() as popup_info:
        link.click()
    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded")
    expect(popup).to_have_url(full_url)

    expect(link).to_have_attribute("href", full_url)


def test_user_bubble_wrapped_url_links_to_full_destination(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A pasted multiline message's wrapped URL links whole in the user bubble."""
    base_url, session_id = seeded_session
    text, full_url = _wrapped_url_message(base_url)
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")

    bubble = page.locator(_USER_BUBBLE).last
    expect(bubble).to_be_visible(timeout=30_000)
    link = bubble.locator(_LINK_SELECTOR)
    expect(link).to_be_visible(timeout=10_000)
    expect(link).to_have_attribute("href", full_url)
