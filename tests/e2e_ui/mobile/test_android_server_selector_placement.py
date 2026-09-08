"""E2E: Android server selection must not overlap the header's Chat/Terminal pill.

On a phone-width screen the Android shell floats its native server-switcher
pill top-center over the WebView (``switchButton`` in
``web/android/app/src/main/java/ai/omnigent/android/MainActivity.kt``:
``Gravity.TOP | CENTER_HORIZONTAL``, always visible, WRAP_CONTENT with no
width cap). The web SPA renders the Chat/Terminal ("Web"/"TUI") view switcher
in the chat header's floating right cluster on every shell (``ViewModeToggle``
in ``web/src/shell/ViewModeToggle.tsx``), inside the same top band. The two
collide: the server selector extends over the adjacent view-mode pill,
obscuring both controls.

The iOS shell had the same defect and was fixed by moving server selection
into the navigation drawer (the SPA's ``SidebarServerPicker``) and having the
web request the floating pill hidden over the main surface on shells that
host the picker bridge (see ``test_ios_server_selector_placement.py``). That
treatment never reached Android: every ``setNativeServerSwitcherHidden``
caller is gated on ``isIOSShell()`` (``web/src/pages/ChatPage.tsx``,
``web/src/hooks/useNativeServerSwitcher.ts``), so under ``kind: "android"``
the web never asks the shell to hide the pill even when the shell can.

The pill itself is native Android chrome that Playwright cannot render, but
its placement is a web-visible contract:

* the web's own stylesheet publishes the pill's floating footprint
  (``--omnigent-android-switcher-margin`` / ``--omnigent-android-switcher-height``
  in ``web/src/index.css``, used by the Android scroll fade), and the shell
  pins the pill at ``status bar + 8dp``, horizontally centered, sized to the
  server host label;
* visibility is shell-default-ON: the pill shows unless the web pushes
  ``setServerSwitcherHidden(true)`` over the bridge; and
* a shell that hosts the sidebar server picker exposes ``getServerPicker`` /
  ``switchServer`` / ``openServerSetup``, which the SPA renders as the
  sidebar's server row (``web/src/shell/SidebarServerPicker.tsx``).

These tests drive the SPA the way the Android WebView shell does — phone
viewport, an injected ``window.omnigentNative`` bridge with the server-picker
methods a picker-hosting shell provides, and the status-bar safe-area inset
applied the way ``MainActivity.emitInsets`` applies it — and assert the fixed
design's two halves: while the chat surface is shown the web must ask the
shell to keep the floating server switcher hidden (instead of letting it
float over the header band where the view-mode pill lives), and server
selection must instead be reachable from the navigation drawer, switching
servers through the shell bridge.
"""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, Route, ViewportSize, expect

# Baseline Android phone width (Galaxy-class, 360dp) — the narrow end where
# the centered pill and the header's right control cluster collide hardest.
_MOBILE_VIEWPORT: ViewportSize = {"width": 360, "height": 800}

# Typical Android status-bar inset, CSS px. On device the shell measures the
# system bars and applies them inline (MainActivity.emitInsets); Chromium
# cannot emulate that, so the test injects the same value into the same vars.
_SAFE_TOP_PX = 24.0

# The native pill's frame, mirroring MainActivity.kt: the insets listener pins
# ``topMargin = statusBar + 8dp``; the TextView (12sp label, 6dp vertical
# padding) stands ~25dp tall — the same footprint the web build publishes as
# --omnigent-android-switcher-margin/-height for its scroll fade. The label is
# the server's host[:port] (``hostLabelOf``) drawn at 12sp with 12dp of
# horizontal padding each side, WRAP_CONTENT with no maximum width.
_SWITCHER_MARGIN_PX = 8.0
_SWITCHER_HEIGHT_PX = 25.0
_SWITCHER_H_PADDING_PX = 24.0

# Another server the shell remembers, offered by the sidebar picker's
# "Recents" section. Host label as the picker renders it.
_ALT_SERVER_URL = "https://alt-server.example.test:8443/"
_ALT_SERVER_HOST = "alt-server.example.test:8443"

# Stand-in for the Android WebView bridge of a shell that hosts the sidebar
# server picker. Runs before any app script (add_init_script) so nativeApi()
# in nativeBridge.ts sees an Android shell: `kind` drives isAndroidShell(),
# setServerSwitcherHidden records every visibility push so the test can
# assert what the web told the shell, the server-picker trio mirrors the
# payload a picker-hosting shell provides from managed config + recents, and
# the rest keep unrelated native calls (badge / notify / insets / view mode)
# from throwing under the stub. onNativeInsets subscribes without pushing:
# the Android shell has no floating-bar footprint emit (its pill footprint is
# the stylesheet contract above).
_ANDROID_SHELL_INIT_SCRIPT = """
window.__switcherHiddenCalls = [];
window.__switchServerCalls = [];
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  setServerSwitcherHidden: function (hidden) {
    window.__switcherHiddenCalls.push(hidden);
  },
  getServerPicker: function () {
    return Promise.resolve({
      currentOrigin: location.origin,
      managedServers: [],
      recentServers: [location.origin + "/", "__ALT_SERVER__"],
    });
  },
  switchServer: function (url) {
    window.__switchServerCalls.push(url);
    return Promise.resolve();
  },
  openServerSetup: function () {},
  setViewMode: function () {},
  onViewModeChanged: function () { return function () {}; },
};
""".replace("__ALT_SERVER__", _ALT_SERVER_URL)


def _mark_terminal_first(base_url: str, session_id: str) -> None:
    """Stamp the session terminal-first (``omnigent.ui = terminal``).

    The Chat/Terminal switcher only exists for terminal-first sessions, so the
    label is the journey's precondition — the same one ``omnigent claude`` /
    ``omnigent codex`` sessions carry.

    :param base_url: Spawned server base URL.
    :param session_id: Session to label.
    """
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    )
    response.raise_for_status()


def _route_agent_terminal(page: Page, session_id: str) -> None:
    """Serve a deterministic agent terminal pane for the session.

    The switcher's placement does not need a live PTY, only the resource shape
    the runner publishes — mirrors ``test_terminal_view_url.py``.

    :param page: Page whose network to intercept.
    :param session_id: Session whose terminals list to stub.
    """

    def _serve(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "terminal_tui_main",
                            "type": "terminal",
                            "session_id": session_id,
                            "name": "tui:main",
                            "metadata": {
                                "terminal_name": "tui",
                                "session_key": "main",
                                "running": True,
                            },
                        }
                    ],
                    "first_id": "terminal_tui_main",
                    "last_id": "terminal_tui_main",
                    "has_more": False,
                }
            ),
        )

    terminal_list = re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals\?.*")
    page.route(terminal_list, _serve)


def _switcher_band(page: Page) -> dict[str, float]:
    """Compute the native server-switcher pill's floating frame, in CSS px.

    Mirrors the shell's layout: horizontally centered, ``topMargin`` of the
    status-bar inset plus 8dp, ~25dp tall, sized to the current server's
    host[:port] label at 12sp plus 12dp horizontal padding per side. The
    label width is measured in-page at the pill's font so the frame tracks
    the label the shell would actually draw for this server.

    :param page: Page whose viewport and origin define the pill.
    :returns: The frame as ``{"left", "top", "right", "bottom"}``.
    """
    label_width = float(
        page.evaluate(
            """() => {
              const canvas = document.createElement("canvas");
              const ctx = canvas.getContext("2d");
              ctx.font = "12px Roboto, sans-serif";
              return ctx.measureText(location.host).width;
            }"""
        )
    )
    inner_width = float(page.evaluate("() => window.innerWidth"))
    width = label_width + _SWITCHER_H_PADDING_PX
    left = (inner_width - width) / 2
    top = _SAFE_TOP_PX + _SWITCHER_MARGIN_PX
    return {"left": left, "top": top, "right": left + width, "bottom": top + _SWITCHER_HEIGHT_PX}


def _as_rect(box: dict[str, float]) -> dict[str, float]:
    """Convert a Playwright bounding box to a left/top/right/bottom rect.

    :param box: ``{"x", "y", "width", "height"}`` from ``bounding_box()``.
    :returns: The rect as ``{"left", "top", "right", "bottom"}``.
    """
    return {
        "left": box["x"],
        "top": box["y"],
        "right": box["x"] + box["width"],
        "bottom": box["y"] + box["height"],
    }


def _intersects(a: dict[str, float], b: dict[str, float]) -> bool:
    """Whether two left/top/right/bottom rects overlap.

    :param a: First rect.
    :param b: Second rect.
    :returns: True when the rects share any area.
    """
    return (
        a["left"] < b["right"]
        and b["left"] < a["right"]
        and a["top"] < b["bottom"]
        and b["top"] < a["bottom"]
    )


def _open_android_terminal_first_session(page: Page, base_url: str, session_id: str) -> None:
    """Open the terminal-first session under the injected Android shell.

    :param page: Playwright page fixture (fresh context per test).
    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id to open.
    :returns: None. Leaves the chat view loaded with the status-bar inset
        applied and the header view-mode toggle rendered.
    """
    _mark_terminal_first(base_url, session_id)
    _route_agent_terminal(page, session_id)
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    # Gate: the SPA recognized the Android shell (isAndroidShell() -> AppShell tag).
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")

    # Inject the status-bar safe-area inset the way MainActivity.emitInsets
    # does: inline on the document root, into both the app's base inset var
    # and the Android-specific fold var (Android WebView reports
    # env(safe-area-inset-top) as 0, so the shell always supplies it).
    page.evaluate(
        """(safeTop) => {
          const s = document.documentElement.style;
          s.setProperty("--omnigent-safe-top", `${safeTop}px`);
          s.setProperty("--omnigent-android-safe-area-top", `${safeTop}px`);
        }""",
        _SAFE_TOP_PX,
    )

    composer = page.locator('textarea[aria-label="Message the agent"]')
    expect(composer).to_be_visible(timeout=60_000)


def test_android_server_switcher_stays_clear_of_view_mode_toggle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Android shell's server switcher must not overlap the view-mode pill.

    Drives the reported journey: open a terminal-first session in the Android
    app on a phone-width screen (injected bridge + status-bar inset), let the
    chat header render its Chat/Terminal ("Web"/"TUI") switcher, and check
    where the native server pill floats. With server selection moved to the
    navigation drawer, the web must request the always-visible native pill
    hidden over the chat surface; while the web leaves it visible, its frame
    must not sit inside the chat header nor over the view-mode toggle. Before
    the fix the web never drives the Android switcher bridge at all, so the
    pill stays visible, floating over the header band and extending across
    the adjacent view-mode pill — the overlap in the bug report's screenshot.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _open_android_terminal_first_session(page, base_url, session_id)

    toggle = page.get_by_test_id("view-mode-toggle")
    expect(toggle).to_be_visible(timeout=8_000)
    header = page.locator(".chat-header")
    expect(header).to_be_visible()

    # Give the mount-time bridge pushes a beat to land, then read what the
    # web told the shell. The Android pill is shell-default-ON: it floats
    # unless the web's latest push asked it hidden.
    page.wait_for_timeout(500)
    hidden_calls = page.evaluate("() => window.__switcherHiddenCalls")
    switcher_visible = not (hidden_calls and hidden_calls[-1] is True)

    pill = _switcher_band(page)
    header_box = header.bounding_box()
    toggle_box = toggle.bounding_box()
    assert header_box is not None and toggle_box is not None
    print(f"[android-switcher] pill frame: {pill}")
    print(f"[android-switcher] header: {_as_rect(header_box)}")
    print(f"[android-switcher] view-mode toggle: {_as_rect(toggle_box)}")
    print(f"[android-switcher] setServerSwitcherHidden pushes: {hidden_calls}")

    violations: list[str] = []
    if switcher_visible and _intersects(pill, _as_rect(header_box)):
        violations.append(
            f"the server-switcher frame {pill} floats inside the chat header "
            f"{_as_rect(header_box)} (selector rendered over the header band)"
        )
    if switcher_visible and _intersects(pill, _as_rect(toggle_box)):
        violations.append(
            f"the server-switcher frame {pill} overlaps the Chat/Terminal "
            f"view-mode pill {_as_rect(toggle_box)} (the overlap in the bug "
            "report's screenshot; a longer server hostname widens the pill "
            "further across it)"
        )

    assert not violations, (
        "Android server switcher must not float over the chat header's "
        "view-mode pill: " + "; ".join(violations)
    )

    # The web must also have asked the shell to keep the pill hidden — the
    # positive half of the contract, so this can't pass merely because the
    # bridge was never driven (today's failure mode: no pushes at all, the
    # Android shell keeps its always-visible default).
    assert hidden_calls and all(hidden_calls), (
        "the web must request the native switcher hidden on an Android shell "
        f"with the sidebar picker; got setServerSwitcherHidden calls: {hidden_calls}"
    )


def test_android_server_selection_lives_in_the_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Server selection is reachable from the Android navigation drawer.

    The fixed design's other half: with the floating pill hidden, the sidebar
    must carry the server picker (the desktop/iOS treatment, adapted to the
    drawer). Opens the drawer from the chat header, picks another recent
    server from the picker's menu, and asserts the switch is requested
    through the shell bridge — so hiding the pill can never leave Android
    with no server-selection affordance at all.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _open_android_terminal_first_session(page, base_url, session_id)

    # The drawer starts closed on a phone viewport; the chat header's toggle
    # is the way in.
    page.get_by_role("button", name="Open sidebar").click()

    picker = page.get_by_test_id("sidebar-server-picker")
    expect(picker).to_be_visible()
    picker.click()

    # The picker's menu offers the other remembered server; choosing it must
    # ask the shell to switch, with the exact URL the picker payload carried.
    page.get_by_role("menuitem", name=_ALT_SERVER_HOST).click()
    page.wait_for_function(
        "() => window.__switchServerCalls.length > 0",
        timeout=10_000,
    )
    assert page.evaluate("() => window.__switchServerCalls") == [_ALT_SERVER_URL], (
        "selecting a server in the sidebar picker must reach the shell bridge "
        "with the exact URL the picker payload carried"
    )
