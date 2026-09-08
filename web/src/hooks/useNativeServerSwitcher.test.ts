import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  isSurfaceFrontmost,
  serverSwitcherHiddenForSurface,
  useNativeServerSwitcherForMainSurface,
} from "./useNativeServerSwitcher";

// The native Liquid Glass Chat/Terminal bar floats over the web view, so DOM
// stacking can't hide it — its visibility rides on `isSurfaceFrontmost`. A
// Radix menu drops `pointer-events: none` on <body>, making the centre probe
// fall through to the document root; that's normally a transient layer we keep
// the surface "frontmost" through. But the session kebab menu lives INSIDE the
// mobile sidebar overlay, so opening it must NOT re-float the bar over the
// sidebar. These tests pin that regression.

const VIEWPORT = 400;

function stubHitTest(topElement: Element | null): void {
  // jsdom doesn't implement elementFromPoint, so assign it outright.
  (document as unknown as { elementFromPoint: () => Element | null }).elementFromPoint = () =>
    topElement;
}

function rect(overrides: Partial<DOMRect>): DOMRect {
  return {
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    width: 0,
    height: 0,
    toJSON: () => ({}),
    ...overrides,
  } as DOMRect;
}

function makeSurface(): HTMLElement {
  const el = document.createElement("div");
  // Full-viewport chat surface centred under the probe point.
  el.getBoundingClientRect = () =>
    rect({ top: 0, left: 0, right: VIEWPORT, bottom: 800, width: VIEWPORT, height: 800 });
  document.body.appendChild(el);
  return el;
}

function makeOpenSidebar(): HTMLElement {
  const aside = document.createElement("aside");
  aside.className = "conversations-sidebar";
  // Open == no `data-collapsed`; full-screen overlay covering the probe.
  aside.getBoundingClientRect = () =>
    rect({ top: 0, left: 0, right: VIEWPORT, bottom: 800, width: VIEWPORT, height: 800 });
  document.body.appendChild(aside);
  return aside;
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete (document as unknown as Record<string, unknown>).elementFromPoint;
  document.body.innerHTML = "";
});

describe("isSurfaceFrontmost", () => {
  it("returns false when a menu's body fall-through sits over an open sidebar", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    makeOpenSidebar();
    // Radix set pointer-events:none on <body>, so the hit test falls through.
    stubHitTest(document.body);

    expect(isSurfaceFrontmost(surface)).toBe(false);
  });

  it("stays frontmost through a menu when the sidebar is collapsed", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    const sidebar = makeOpenSidebar();
    sidebar.setAttribute("data-collapsed", "");
    stubHitTest(document.body);

    expect(isSurfaceFrontmost(surface)).toBe(true);
  });

  it("stays frontmost through a menu when no sidebar is present", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    stubHitTest(document.body);

    expect(isSurfaceFrontmost(surface)).toBe(true);
  });

  it("returns false when the menu popper covers the probe over an open sidebar", () => {
    vi.stubGlobal("innerWidth", VIEWPORT);
    const surface = makeSurface();
    makeOpenSidebar();
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    document.body.appendChild(menu);
    stubHitTest(menu);

    expect(isSurfaceFrontmost(surface)).toBe(false);
  });
});

// Server selection moved into the sidebar picker on shells that host it, so
// the floating pill must never be requested over the main surface there — it
// used to crowd the chat header's title and floating controls on a notched
// iPhone. Shells without the picker bridge (older iOS builds) keep the pill
// as their only selection affordance, following the frontmost signal.
describe("serverSwitcherHiddenForSurface", () => {
  function setIOSBridge(withServerPicker: boolean): void {
    (window as unknown as Record<string, unknown>).omnigentNative = {
      kind: "ios",
      setBadgeCount: () => {},
      notify: () => Promise.resolve(false),
      setServerSwitcherHidden: () => {},
      ...(withServerPicker
        ? {
            getServerPicker: () => Promise.resolve(null),
            switchServer: () => Promise.resolve(),
            openServerSetup: () => {},
          }
        : {}),
    };
  }

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  });

  it("keeps the pill hidden over a frontmost surface on a shell with the sidebar picker", () => {
    setIOSBridge(true);
    expect(serverSwitcherHiddenForSurface(true)).toBe(true);
    expect(serverSwitcherHiddenForSurface(false)).toBe(true);
  });

  it("follows the frontmost signal on a shell without the sidebar picker", () => {
    setIOSBridge(false);
    expect(serverSwitcherHiddenForSurface(true)).toBe(false);
    expect(serverSwitcherHiddenForSurface(false)).toBe(true);
  });
});

// The Android shell floats the same kind of native pill as iOS (its top-center
// server switcher, shell-default-visible), so the main-surface hook must drive
// it too — the old iOS-only gate left Android's pill floating over the chat
// header's Chat/Terminal toggle, with no hide push ever sent.
describe("useNativeServerSwitcherForMainSurface", () => {
  function installBridge(kind: "ios" | "android" | "electron"): boolean[] {
    const pushes: boolean[] = [];
    (window as unknown as Record<string, unknown>).omnigentNative = {
      kind,
      setBadgeCount: () => {},
      notify: () => Promise.resolve(false),
      setServerSwitcherHidden: (hidden: boolean) => pushes.push(hidden),
      getServerPicker: () => Promise.resolve(null),
      switchServer: () => Promise.resolve(),
      openServerSetup: () => {},
    };
    return pushes;
  }

  function stubAnimationFrame(): void {
    // jsdom's rAF timing is irrelevant here — run the frontmost probe inline.
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      cb(0);
      return 0;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
  }

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  });

  it("asks the Android shell to keep its pill hidden over the main surface", () => {
    stubAnimationFrame();
    const pushes = installBridge("android");

    const { unmount } = renderHook(() => useNativeServerSwitcherForMainSurface(null, true));
    expect(pushes.length).toBeGreaterThan(0);
    expect(pushes.every(Boolean)).toBe(true);

    unmount();
    expect(pushes.at(-1)).toBe(true);
  });

  it("still drives the iOS shell's pill the same way", () => {
    stubAnimationFrame();
    const pushes = installBridge("ios");

    const { unmount } = renderHook(() => useNativeServerSwitcherForMainSurface(null, true));
    unmount();
    expect(pushes.length).toBeGreaterThan(0);
    expect(pushes.every(Boolean)).toBe(true);
  });

  it("never drives a shell without a floating pill", () => {
    stubAnimationFrame();
    const pushes = installBridge("electron");

    const { unmount } = renderHook(() => useNativeServerSwitcherForMainSurface(null, true));
    unmount();
    expect(pushes).toEqual([]);
  });
});
