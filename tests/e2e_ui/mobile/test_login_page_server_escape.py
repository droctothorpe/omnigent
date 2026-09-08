"""E2E: a login-gated server must not strand the iOS shell user.

The reported trap's third symptom: when the iOS app ends up on a server it cannot
get past (a login wall it can't complete, a broken deployment), "there's no way
to reload/switch server since the server switcher doesn't render".

The native side of the trap is in ``web/ios/Omnigent``: the floating
server-switcher pill starts hidden on every load and its liveness watchdog
(``WebViewModel.armServerSwitcherWatchdog``) stands down the moment the page
speaks over the JS bridge — from then on the *web app* owns server-selection
affordances. On shells that host the sidebar server picker (the bridge exposes
``getServerPicker``, the 0.2.0 shell shape) the SPA keeps the pill hidden and
offers selection only through ``SidebarServerPicker`` in the navigation drawer.

The web-visible half — what this test drives — is the trap itself: the SPA's
login page (``/login``, LoginPage) mounts OUTSIDE the AppShell tree, so there
is no sidebar and no ``SidebarServerPicker``; nothing on it requests the native
pill (``setServerSwitcherHidden(false)``) and nothing on it drives
``openServerSetup``/``switchServer`` — yet the page DOES speak over the bridge
(ThemeProvider pushes ``setColorScheme`` on mount), which on device cancels the
shell's 6-second watchdog escape hatch. Net: a user stuck at the login page has
no route back to server selection at all.

This drives the SPA exactly the way the WKWebView shell does — iPhone viewport
and an injected ``window.omnigentNative`` bridge that hosts the sidebar picker
(the fixed-shell shape from ``test_ios_server_selector_placement.py``) — lands
on the login page unauthenticated, and asserts the escape-hatch contract: the
login page must either ask the shell to show its native switcher
(``setServerSwitcherHidden(false)``) or render an affordance of its own that
can reach server selection. Before the fix it did neither, so this test failed —
the reproduction of the "no way to reload/switch server" facet.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, ViewportSize, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.auth._accounts_server import AccountsServer, spawn_accounts_server

# Phone-sized viewport: the iOS shell is (primarily) an iPhone surface.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Stand-in for the iOS WKWebView bridge of a shell that hosts the sidebar
# server picker (`getServerPicker` present — the 0.2.0 shell). Runs before any
# app script (add_init_script) so nativeApi() in nativeBridge.ts sees an iOS
# shell. Every method records into window.__bridgeCalls: on device, ANY
# trusted bridge message cancels the shell's server-switcher liveness watchdog
# (WebViewModel.cancelServerSwitcherWatchdog), so "the page spoke" is exactly
# the condition under which the native pill will never self-reveal.
_IOS_SHELL_INIT_SCRIPT = """
window.__bridgeCalls = [];
window.__switcherHiddenCalls = [];
window.__openServerSetupCalls = 0;
window.__switchServerCalls = [];
window.omnigentNative = {
  kind: "ios",
  setColorScheme: function () { window.__bridgeCalls.push("setColorScheme"); },
  setBadgeCount: function () { window.__bridgeCalls.push("setBadgeCount"); },
  notify: function () {
    window.__bridgeCalls.push("notify");
    return Promise.resolve(false);
  },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onNativeInsets: function (cb) {
    cb({ topBar: 36, bottomBar: 48 });
    return function () {};
  },
  setServerSwitcherHidden: function (hidden) {
    window.__bridgeCalls.push("setServerSwitcherHidden");
    window.__switcherHiddenCalls.push(hidden === true);
  },
  setSidebarOpen: function () { window.__bridgeCalls.push("setSidebarOpen"); },
  setViewMode: function () { window.__bridgeCalls.push("setViewMode"); },
  onViewModeChanged: function () { return function () {}; },
  getServerPicker: function () {
    window.__bridgeCalls.push("getServerPicker");
    return Promise.resolve({
      currentOrigin: location.origin,
      managedServers: [],
      recentServers: [location.origin + "/"],
    });
  },
  switchServer: function (url) {
    window.__switchServerCalls.push(url);
    return Promise.resolve();
  },
  openServerSetup: function () { window.__openServerSetupCalls += 1; },
};
"""

# The escape-hatch contract, as a polled predicate. True as soon as the login
# page gives the user ANY route back to server selection:
#   - it asked the shell to show the native switcher pill
#     (setServerSwitcherHidden(false)), or
#   - it renders a visible control of its own that names the server journey
#     (e.g. "Connect to a different server"), whatever bridge call it drives.
# Today's login page does neither (verified: no visible control on LoginPage /
# AuthCardShell mentions "server"), so the poll times out before the fix.
_ESCAPE_HATCH_PROBE = """
() => {
  if (window.__switcherHiddenCalls.some((hidden) => hidden === false)) return true;
  const controls = document.querySelectorAll(
    "a, button, [role='button'], [role='menuitem']"
  );
  for (const el of controls) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const label = (
      (el.getAttribute("aria-label") || "") + " " + (el.textContent || "")
    ).toLowerCase();
    if (label.includes("server")) return true;
  }
  return false;
}
"""


@pytest.fixture(scope="module")
def accounts_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AccountsServer]:
    """A dedicated login-gated (accounts-mode) server.

    The shared ``live_server`` runs single-user with auth off, so it never
    shows the login wall the reported trap needs.
    """
    server_tmp = tmp_path_factory.mktemp("e2e_ui_login_escape")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


def test_ios_login_page_offers_a_route_back_to_server_selection(
    page: Page,
    accounts_server: AccountsServer,
) -> None:
    """A user stuck at the login page must be able to reach server selection.

    Drives the reported journey: connect the iOS app to a login-gated server
    (iPhone viewport, injected shell bridge), get bounced to the SPA login
    page, and look for any way back to server selection. Before the fix there
    is none — the page speaks over the bridge (standing the native watchdog
    down) yet never requests the native switcher pill and renders no
    server-selection affordance of its own, so the user is trapped exactly as
    reported.

    :param page: Playwright page fixture (fresh context per test).
    :param accounts_server: The dedicated login-gated server.
    :returns: None.
    """
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)

    # 1. The shell pins the server and loads it; unauthenticated, the SPA's
    #    identity probe 401s and hands the browser to the login page.
    page.goto(accounts_server.public_url)
    expect(page.locator("#login-password")).to_be_visible(timeout=20_000)

    # 2. The login page speaks over the bridge (theme sync runs on every
    #    page). On device this is what cancels the shell's 6s watchdog, so
    #    from here the WEB owns the only possible escape hatch.
    page.wait_for_function("() => window.__bridgeCalls.length > 0", timeout=15_000)

    # 3. The escape-hatch contract: within a generous settle window the login
    #    page must either request the native switcher shown or render its own
    #    server-selection affordance.
    escape_hatch_offered = True
    try:
        page.wait_for_function(_ESCAPE_HATCH_PROBE, timeout=8_000)
    except PlaywrightTimeoutError:
        escape_hatch_offered = False

    switcher_calls = page.evaluate("() => window.__switcherHiddenCalls")
    bridge_calls = page.evaluate("() => window.__bridgeCalls")
    assert escape_hatch_offered, (
        "the iOS shell's login page strands the user with no route back to "
        "server selection: it spoke over the bridge (cancelling the shell's "
        f"watchdog escape hatch; calls={bridge_calls}), never requested the "
        f"native server switcher (setServerSwitcherHidden calls={switcher_calls}), "
        "and renders no server-selection affordance of its own — the "
        "'no way to reload/switch server' trap"
    )
