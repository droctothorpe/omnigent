// Escape hatch back to server selection for auth pages hosted in a native
// shell (iOS / Android / Electron).
//
// Auth pages (login, register) mount OUTSIDE the AppShell tree, so the
// sidebar's SidebarServerPicker never renders there — yet they still speak
// over the bridge (theme sync runs on every page), which stands down the iOS
// shell's server-switcher liveness watchdog. Without this component a user
// stuck at a login wall they can't pass (wrong server, broken deployment, no
// account) has no route back to server selection at all.
//
// Shells with the full picker bridge get an explicit web affordance that
// returns them to the shell's connect/setup page; older shells without it are
// asked to show their own floating switcher pill instead. Renders nothing in
// a plain browser, where the address bar is the escape hatch.

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  isNativeShell,
  openServerSetup,
  setNativeServerSwitcherHidden,
  supportsNativeServerPicker,
} from "@/lib/nativeBridge";

export function AuthServerEscape() {
  const [showEscape, setShowEscape] = useState(false);

  useEffect(() => {
    if (!isNativeShell()) return;
    if (supportsNativeServerPicker()) {
      setShowEscape(true);
      return;
    }
    // Older shells own the affordance: ask for their floating switcher pill.
    // The page's own bridge chatter cancelled the shell's liveness watchdog,
    // so the pill never self-reveals without this.
    setNativeServerSwitcherHidden(false);
    return () => setNativeServerSwitcherHidden(true);
  }, []);

  if (!showEscape) return null;

  return (
    <div className="text-center">
      <Button
        type="button"
        variant="link"
        className="text-muted-foreground hover:text-foreground"
        onClick={() => openServerSetup()}
        componentId="auth.connect_different_server"
      >
        Connect to a different server…
      </Button>
    </div>
  );
}
