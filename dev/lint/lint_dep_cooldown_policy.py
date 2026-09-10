#!/usr/bin/env python3
"""Fail when the dependency-cooldown policy is missing or placed where the
resolver silently ignores it.

The scenario this guards: pnpm only honors `minimumReleaseAge` when it sits in
the top-level `settings:` block of `pnpm-workspace.yaml`. Nested anywhere else
it parses fine but is never read, so the seven-day cooldown is silently off and
a freshly published (possibly compromised) version can be pinned. That is
exactly the regression that landed once already — the setting existed but in a
spot pnpm didn't look, so nothing enforced it.

This is a cheap, offline config guard: it asserts the cooldown knobs are present
AND effective on both ecosystems. It does NOT verify individual package ages —
`verify_dependency_ages.py` does that against the registries.

Checks:
- pnpm-workspace.yaml `settings.minimumReleaseAge` >= 10080 (7 days, in minutes).
- uv.toml `exclude-newer` present and >= 7 days (relative `P{n}D` form) or a
  fixed date (accepted as-is).
- uv.toml `required-version` present (so an old uv that can't honor the cooldown
  fails loudly rather than resolving without it).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT / "pnpm-workspace.yaml"
UV_TOML = REPO_ROOT / "uv.toml"

# 7 days. pnpm expresses minimumReleaseAge in minutes; uv uses an ISO-8601 span.
WEEK_MINUTES = 7 * 24 * 60


def _check_pnpm(problems: list[str]) -> None:
    if not WORKSPACE.exists():
        problems.append(f"{WORKSPACE.name}: file not found")
        return
    workspace = yaml.safe_load(WORKSPACE.read_text()) or {}
    settings = workspace.get("settings")
    if not isinstance(settings, dict) or "minimumReleaseAge" not in settings:
        # Catch the exact trap: the key nested somewhere the resolver ignores.
        misplaced = "minimumReleaseAge" in WORKSPACE.read_text()
        hint = (
            " (found the key elsewhere in the file — it must live under the "
            "top-level `settings:` block or pnpm ignores it)"
            if misplaced
            else ""
        )
        problems.append(
            f"{WORKSPACE.name}: settings.minimumReleaseAge is missing{hint}"
        )
        return
    value = settings["minimumReleaseAge"]
    if not isinstance(value, int) or value < WEEK_MINUTES:
        problems.append(
            f"{WORKSPACE.name}: settings.minimumReleaseAge is {value!r}, "
            f"expected an integer >= {WEEK_MINUTES} (7 days in minutes)"
        )


def _exclude_newer_ok(value: object) -> bool:
    """Accept a fixed date, or a relative `P{n}D` / `P{n}W` span >= 7 days."""
    if not isinstance(value, str):
        return False
    m = re.fullmatch(r"P(\d+)([DW])", value.strip())
    if m:
        days = int(m.group(1)) * (7 if m.group(2) == "W" else 1)
        return days >= 7
    # A fixed ISO date (e.g. "2026-01-01") or timestamp is a deliberate pin.
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", value.strip()))


def _check_uv(problems: list[str]) -> None:
    if not UV_TOML.exists():
        problems.append(f"{UV_TOML.name}: file not found")
        return
    data = tomllib.loads(UV_TOML.read_text())
    if "exclude-newer" not in data:
        problems.append(f"{UV_TOML.name}: exclude-newer is missing")
    elif not _exclude_newer_ok(data["exclude-newer"]):
        problems.append(
            f"{UV_TOML.name}: exclude-newer is {data['exclude-newer']!r}, "
            "expected a span >= 7 days (e.g. \"P7D\") or a fixed date"
        )
    if "required-version" not in data:
        problems.append(
            f"{UV_TOML.name}: required-version is missing — without it an old uv "
            "can resolve while ignoring the cooldown"
        )


def main() -> int:
    problems: list[str] = []
    _check_pnpm(problems)
    _check_uv(problems)

    if problems:
        print(
            "Dependency-cooldown policy is not effective. The seven-day supply-chain\n"
            "cooldown must be configured where each resolver actually reads it:\n"
        )
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
