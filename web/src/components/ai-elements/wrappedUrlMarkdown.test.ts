import { describe, expect, it } from "vitest";
import { joinWrappedUrls } from "./wrappedUrlMarkdown";

describe("joinWrappedUrls — rejoining hard-wrapped URLs", () => {
  it("joins a URL split mid-path whose tail carries query structure", () => {
    // The reported shape: the first line is itself a well-formed URL, so the
    // buggy render produced a "working" anchor to the truncated destination.
    const text =
      "Your export is ready:\nhttp://host/heal\nth?probe=full-dest\n\nOpen it to verify.";
    expect(joinWrappedUrls(text)).toBe(
      "Your export is ready:\nhttp://host/health?probe=full-dest\n\nOpen it to verify.",
    );
  });

  it("joins when the URL ends dangling mid-token", () => {
    const text = "https://host/health?probe=x&part=first-half-\nsecond-half-continuation";
    expect(joinWrappedUrls(text)).toBe(
      "https://host/health?probe=x&part=first-half-second-half-continuation",
    );
  });

  it("joins a URL wrapped across three lines", () => {
    const text = "see\nhttps://host/a?x=\n1&y=\n42 and report back";
    expect(joinWrappedUrls(text)).toBe("see\nhttps://host/a?x=1&y=42\n and report back");
  });

  it("keeps text after the continuation token on its own line", () => {
    // The trailing comma rides along; GFM's autolinker trims it off the link.
    const text = "https://host/q?a=\nbc, then tell me";
    expect(joinWrappedUrls(text)).toBe("https://host/q?a=bc,\n then tell me");
  });

  it("joins www-prefixed bare URLs too", () => {
    const text = "www.example.com/pa?probe=\nvalue";
    expect(joinWrappedUrls(text)).toBe("www.example.com/pa?probe=value");
  });

  it("leaves prose after a complete URL alone", () => {
    const text = "see http://example.com\nand then tell me";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("does not read sentence punctuation as URL structure", () => {
    // "them?" ends in a question mark; that is prose, not a query string.
    const text = "did you open http://example.com\nthem? I think so";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("leaves a slash-terminated URL followed by prose alone", () => {
    const text = "docs at https://example.com/docs/\nNext, install it";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("does not eat a list marker after a dangling URL", () => {
    const text = "https://host/a?x=\n- item one";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("never joins an indented continuation — indentation is markdown structure", () => {
    const text = "https://host/a?x=\n    indented code";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("leaves fenced code blocks verbatim", () => {
    const text = "```\nhttp://host/heal\nth?probe=full-dest\n```";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("leaves a URL inside an open inline-code span alone", () => {
    const text = "run `curl http://host/heal\nth?probe=x` now";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("ignores a blank line after a URL", () => {
    const text = "http://host/heal\n\nth?probe=x";
    expect(joinWrappedUrls(text)).toBe(text);
  });

  it("returns single-line text unchanged", () => {
    const text = "just http://example.com here";
    expect(joinWrappedUrls(text)).toBe(text);
  });
});
