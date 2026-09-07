import { describe, expect, it } from "vitest";

import { highlightedBodyFacadeModules } from "./eagerHighlightedBodyFacade";

// Raw text of the same files, to bound what the eager pin drags into the
// static bundle: the facade must stay a tiny re-export shim so pinning it
// eagerly cannot bloat the entry bundle.
const rawFacades: Record<string, unknown> = import.meta.glob(
  "/node_modules/streamdown/dist/highlighted-body-*.js",
  { eager: true, query: "?raw", import: "default" },
);

describe("eagerHighlightedBodyFacade", () => {
  it("pins at least one facade module (a streamdown upgrade may move or rename it)", () => {
    expect(Object.keys(highlightedBodyFacadeModules).length).toBeGreaterThan(0);
  });

  it("every pinned module re-exports streamdown's highlighted-body component", () => {
    for (const [path, mod] of Object.entries(highlightedBodyFacadeModules)) {
      const kind = typeof (mod as { HighlightedCodeBlockBody?: unknown }).HighlightedCodeBlockBody;
      expect(kind === "function" || kind === "object", `${path} must re-export the body`).toBe(
        true,
      );
    }
  });

  it("stays a tiny shim, so pinning it eagerly cannot bloat the entry chunk", () => {
    for (const [path, raw] of Object.entries(rawFacades)) {
      expect(typeof raw, path).toBe("string");
      expect((raw as string).length, `${path} should be a small re-export shim`).toBeLessThan(4096);
    }
  });
});
