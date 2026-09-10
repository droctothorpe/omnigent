// A bare URL hard-wrapped across lines — pasted from a terminal or an email
// that wrapped it, or emitted pre-wrapped by an agent — autolinkifies only up
// to the line break: the anchor's href stops at the break and the continuation
// is left behind as plain text, so clicking opens a truncated destination.
// `joinWrappedUrls` rejoins such URLs before markdown parsing so the whole
// destination linkifies. Runs on both chat surfaces (assistant text and user
// bubbles) via `FilePathAwareMessageResponse`.

// Optional 1–3 space indent + a fence run, per CommonMark (same tracking as
// `normalizeExplicitMathDelimiters`).
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;

// A bare autolink-literal URL sitting at the very end of a line, with no
// trailing whitespace — the shape a hard wrap leaves behind.
const TRAILING_URL_RE = /(?:https?:\/\/|www\.)[^\s<>]+$/;

// A continuation candidate: a run of URL-permitted characters starting at
// column 0 of the next line. Indented lines are never joined — indentation
// signals markdown structure (an indented code block), not a wrapped URL.
const CONTINUATION_RE = /^[A-Za-z0-9._~:/?#@!$&+,;=%-]+/;

// Sentence punctuation a prose token drags along at its end ("them?", "no.").
// GFM's autolinker also trims these, so leaving them on the joined text is
// safe; they are only stripped for the signal check below.
const TRAILING_PUNCT_RE = /[.,;:!?)'"\]]+$/;

// URL structure rare at the start of prose but common inside a wrapped URL's
// tail ("th?probe=x", "a=b&c=d"). "/" is deliberately excluded: dates
// ("10/12/2024") and path-like prose start lines too often.
const CONTINUATION_SIGNAL_RE = /[?#=&%~]/;

// A URL cut mid-token: none of these characters naturally end a URL, so a
// line-final URL ending in one was almost certainly split by a hard wrap.
const DANGLING_URL_END_RE = /[-?#=&%_+]$/;

/**
 * Whether `cont` reads as the continuation of `url` rather than the start of
 * the next sentence. Either the continuation carries URL structure a prose
 * word wouldn't ("th?probe=x"), or the URL itself ends dangling mid-token
 * ("…&part=first-half-"). Both err toward *not* joining: a missed join keeps
 * today's rendering, a false join corrupts prose into a link.
 */
function isUrlContinuation(url: string, cont: string): boolean {
  const core = cont.replace(TRAILING_PUNCT_RE, "");
  // Too short to be a wrap remnant — also keeps list markers ("- item") out.
  if (core.length < 2) return false;
  return CONTINUATION_SIGNAL_RE.test(core) || DANGLING_URL_END_RE.test(url);
}

/**
 * Rejoins bare URLs that a hard wrap split across lines, so the autolinker
 * sees the whole destination. Only where doing so is safe:
 *
 * - Not inside fenced code blocks, so code examples stay verbatim.
 * - Not when the line's inline-code backticks are unbalanced — the URL may
 *   sit inside an open `` ` `` span that continues on the next line.
 * - Only when the next line starts (at column 0) with a token that reads as
 *   URL continuation, per {@link isUrlContinuation}.
 *
 * A URL wrapped across three or more lines is joined iteratively. When the
 * continuation token ends mid-line, the remainder of that line is kept as its
 * own line — only the URL fragment moves.
 */
export function joinWrappedUrls(text: string): string {
  if (!text.includes("\n")) return text;
  const lines = text.split("\n");
  const out: string[] = [];
  // The marker that opened the current fenced block, or "" outside a fence.
  let openFence = "";
  for (let i = 0; i < lines.length; i += 1) {
    let line = lines[i];
    const fence = line.match(FENCE_RE);
    if (fence) {
      const marker = fence[1];
      if (!openFence) {
        openFence = marker;
      } else if (marker[0] === openFence[0] && marker.length >= openFence.length) {
        openFence = "";
      }
      out.push(line);
      continue;
    }
    if (openFence) {
      out.push(line);
      continue;
    }
    while (i + 1 < lines.length) {
      const url = line.match(TRAILING_URL_RE)?.[0];
      if (!url) break;
      // Unbalanced backticks: the trailing URL may be inside inline code.
      if ((line.split("`").length - 1) % 2 === 1) break;
      const next = lines[i + 1];
      if (FENCE_RE.test(next)) break;
      const cont = next.match(CONTINUATION_RE)?.[0];
      if (!cont || !isUrlContinuation(url, cont)) break;
      line += cont;
      const rest = next.slice(cont.length);
      if (rest.trim() === "") {
        // The whole next line was URL: consume it and keep joining.
        i += 1;
      } else {
        // The URL ended mid-line; the remainder stays as its own line.
        lines[i + 1] = rest;
        break;
      }
    }
    out.push(line);
  }
  return out.join("\n");
}
