// Streamdown mounts each fenced code block's highlighted body behind
// `React.lazy(() => import('./highlighted-body-<hash>.js'))`. That facade
// module only re-exports a component streamdown's static chunk already
// carries, yet fetching it at render time can fail mid-session — a tab held
// across a redeploy whose old hashed chunk is gone, or a passing network blip —
// and React.lazy caches the rejection forever, so every later code block
// degrades to the MarkdownErrorBoundary fallback ("Could not render this
// markdown.") until a full page reload.
//
// Importing the facade eagerly folds it into the static entry graph: the
// bundler resolves streamdown's dynamic import from a chunk that is already
// loaded, so rendering a code block never touches the network and cannot fail
// this way. The facade is a tiny re-export shim over a streamdown chunk the
// app already loads at boot, so pinning it eagerly adds no meaningful weight
// to the entry bundle (a baseline-vs-pinned build leaves the boot graph
// unchanged). The
// glob keys off streamdown's published dist layout, which a version bump may
// rename; eagerHighlightedBodyFacade.test.ts fails loudly when the pattern
// stops matching so the pin cannot silently vanish.
export const highlightedBodyFacadeModules: Record<string, unknown> = import.meta.glob(
  "/node_modules/streamdown/dist/highlighted-body-*.js",
  { eager: true },
);
