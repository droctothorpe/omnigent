import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthServerEscape } from "./AuthServerEscape";

// The escape hatch reaches the shell only through nativeBridge, so the bridge
// is the seam: mocking it covers a picker-capable shell, an older pill-only
// shell, and a plain browser without faking an injected bridge object.
const isNativeShell = vi.fn();
const supportsNativeServerPicker = vi.fn();
const openServerSetup = vi.fn();
const setNativeServerSwitcherHidden = vi.fn();

vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
  supportsNativeServerPicker: () => supportsNativeServerPicker(),
  openServerSetup: () => openServerSetup(),
  setNativeServerSwitcherHidden: (hidden: boolean) => setNativeServerSwitcherHidden(hidden),
}));

beforeEach(() => {
  isNativeShell.mockReset();
  supportsNativeServerPicker.mockReset();
  openServerSetup.mockReset();
  setNativeServerSwitcherHidden.mockReset();
});

afterEach(cleanup);

describe("AuthServerEscape", () => {
  it("renders nothing and touches no bridge in a plain browser", () => {
    isNativeShell.mockReturnValue(false);
    const { container } = render(<AuthServerEscape />);

    expect(container).toBeEmptyDOMElement();
    expect(setNativeServerSwitcherHidden).not.toHaveBeenCalled();
    expect(openServerSetup).not.toHaveBeenCalled();
  });

  it("offers a route to server selection on a picker-capable shell", async () => {
    isNativeShell.mockReturnValue(true);
    supportsNativeServerPicker.mockReturnValue(true);
    render(<AuthServerEscape />);

    const escape = await screen.findByRole("button", {
      name: "Connect to a different server…",
    });
    fireEvent.click(escape);

    expect(openServerSetup).toHaveBeenCalledTimes(1);
    // The shell keeps its pill hidden — the web affordance is the escape.
    expect(setNativeServerSwitcherHidden).not.toHaveBeenCalled();
  });

  it("reveals the native switcher pill on an older shell without the picker", async () => {
    isNativeShell.mockReturnValue(true);
    supportsNativeServerPicker.mockReturnValue(false);
    const { container, unmount } = render(<AuthServerEscape />);

    // The pill is the shell's own affordance; the web renders no control.
    await waitFor(() => expect(setNativeServerSwitcherHidden).toHaveBeenCalledWith(false));
    expect(container).toBeEmptyDOMElement();

    // Leaving the auth page hands the switcher back to its usual hidden state.
    unmount();
    expect(setNativeServerSwitcherHidden).toHaveBeenLastCalledWith(true);
  });
});
