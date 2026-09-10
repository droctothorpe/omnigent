"""The chat placeholder must not paint over active native composition text."""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

_QUEUED_PLACEHOLDER = "Send a follow-up (queued) — Esc to stop"


def _publish_running(base_url: str, session_id: str) -> None:
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "running", "response_id": "resp_ime_placeholder"},
        },
        timeout=10.0,
    )
    response.raise_for_status()


def _dispatch_composition(page: Page, event_type: str) -> None:
    page.get_by_label("Message the agent").evaluate(
        """(textarea, type) => {
          textarea.dispatchEvent(new CompositionEvent(type, { bubbles: true }));
        }""",
        event_type,
    )


def test_queued_placeholder_stays_hidden_until_composition_ends(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clearing a sent draft cannot expose a placeholder over marked text."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    _publish_running(base_url, session_id)
    expect(composer).to_have_attribute("placeholder", _QUEUED_PLACEHOLDER, timeout=15_000)

    _dispatch_composition(page, "compositionstart")
    expect(composer).to_have_attribute("placeholder", "")

    composer.fill("disabled")
    composer.evaluate("textarea => textarea.form?.requestSubmit()")
    expect(composer).to_have_value("")
    expect(composer).to_have_attribute("placeholder", "")

    _dispatch_composition(page, "compositionend")
    expect(composer).to_have_attribute("placeholder", _QUEUED_PLACEHOLDER)
