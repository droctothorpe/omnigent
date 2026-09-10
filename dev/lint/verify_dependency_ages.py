#!/usr/bin/env python3
"""Fail when a pinned dependency was published within the cooldown window.

The config guard (`lint_dep_cooldown_policy.py`) proves the cooldown knobs are
present, and each resolver applies them when it generates a lockfile. But
resolution is the *only* moment the JS cooldown is applied — `--frozen-lockfile`
installs trust the lock as-is and never re-check age. So a too-new version can
still reach the committed lockfile: resolved while the policy was misconfigured,
resolved by a machine with a stale config, or slipped in through a bypassed
install. This audit closes that gap by reading the ages back out of the lockfiles
and failing on anything younger than the window.

Two ecosystems, two data sources:
- Python (uv.lock): uv records `upload-time` for every package inline, so this
  half is fully offline — no PyPI queries.
- JS (pnpm-lock.yaml): the lock records no publish time, so we fetch each unique
  package's registry metadata (`.time[version]`) once and read the age from it.

Because the JS half hits the network over ~hundreds of packages, this is meant
to run as a scheduled (nightly) job, not a per-PR blocking gate. Transient
network failures are reported but do not fail the run unless `--strict` is given;
a confirmed too-new package always fails.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PNPM_LOCK = REPO_ROOT / "pnpm-lock.yaml"
UV_LOCK = REPO_ROOT / "uv.lock"
UV_TOML = REPO_ROOT / "uv.toml"

DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"
DEFAULT_MAX_AGE_DAYS = 7
# pnpm keys look like `name@version(peer@1)(peer@2)`; drop the peer suffix.
_PNPM_PEER_SUFFIX = re.compile(r"\(.*\)$")
_SEMVERISH = re.compile(r"^\d+\.\d+\.\d+")


class Pinned:
    __slots__ = ("ecosystem", "name", "version")

    def __init__(self, ecosystem: str, name: str, version: str) -> None:
        self.ecosystem = ecosystem
        self.name = name
        self.version = version

    def __hash__(self) -> int:
        return hash((self.ecosystem, self.name, self.version))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Pinned) and (
            self.ecosystem,
            self.name,
            self.version,
        ) == (other.ecosystem, other.name, other.version)


def _parse_iso(text: str) -> datetime | None:
    try:
        # uv/registry stamps end in 'Z'; normalize for fromisoformat.
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Lockfile parsing
# --------------------------------------------------------------------------- #
def parse_pnpm_lock() -> list[Pinned]:
    """Extract (name, version) for every registry package in pnpm-lock.yaml."""
    if not PNPM_LOCK.exists():
        return []
    data = yaml.safe_load(PNPM_LOCK.read_text()) or {}
    pins: set[Pinned] = set()
    for key in (data.get("packages") or {}):
        spec = _PNPM_PEER_SUFFIX.sub("", key)
        # Split off the version: name may be scoped (`@scope/pkg`), so the
        # version is after the LAST '@'.
        at = spec.rfind("@")
        if at <= 0:
            continue
        name, version = spec[:at], spec[at + 1 :]
        # Skip non-registry resolutions (tarball URLs, git, links, patches).
        if "://" in version or not _SEMVERISH.match(version):
            continue
        pins.add(Pinned("npm", name, version))
    return sorted(pins, key=lambda p: (p.name, p.version))


def parse_uv_lock() -> list[tuple[Pinned, datetime | None]]:
    """Extract (pin, publish_time) from uv.lock using its inline upload-time."""
    if not UV_LOCK.exists():
        return []
    data = tomllib.loads(UV_LOCK.read_text())
    out: list[tuple[Pinned, datetime | None]] = []
    for pkg in data.get("package", []):
        source = pkg.get("source") or {}
        if "registry" not in source:
            continue  # path/git/editable/virtual — not a registry release
        name = pkg.get("name")
        version = pkg.get("version")
        if not name or not version:
            continue
        # Earliest upload across the sdist and wheels is the package's age.
        times: list[datetime] = []
        sdist = pkg.get("sdist") or {}
        if isinstance(sdist, dict) and sdist.get("upload-time"):
            t = _parse_iso(sdist["upload-time"])
            if t:
                times.append(t)
        for wheel in pkg.get("wheels") or []:
            if isinstance(wheel, dict) and wheel.get("upload-time"):
                t = _parse_iso(wheel["upload-time"])
                if t:
                    times.append(t)
        out.append((Pinned("pypi", name, version), min(times) if times else None))
    return out


def load_uv_exemptions() -> set[str]:
    """Package names uv exempts from the cooldown (exclude-newer-package)."""
    if not UV_TOML.exists():
        return set()
    data = tomllib.loads(UV_TOML.read_text())
    exempt = data.get("exclude-newer-package") or {}
    return {name.lower() for name in exempt}


# --------------------------------------------------------------------------- #
# npm registry lookups
# --------------------------------------------------------------------------- #
def _fetch_npm_times(name: str, registry: str, timeout: float) -> dict[str, str]:
    # Scoped names must keep the '/' percent-encoded for the registry path.
    encoded = urllib.parse.quote(name, safe="")
    url = f"{registry.rstrip('/')}/{encoded}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    times = payload.get("time")
    return times if isinstance(times, dict) else {}


def resolve_npm_ages(
    pins: list[Pinned], registry: str, jobs: int, timeout: float
) -> tuple[dict[Pinned, datetime | None], list[str]]:
    """Map each npm pin to its publish time; collect names that couldn't be read."""
    by_name: dict[str, list[Pinned]] = {}
    for p in pins:
        by_name.setdefault(p.name, []).append(p)

    ages: dict[Pinned, datetime | None] = {}
    unresolved: list[str] = []

    def work(name: str) -> tuple[str, dict[str, str] | None]:
        for attempt in range(3):
            try:
                return name, _fetch_npm_times(name, registry, timeout)
            except (urllib.error.URLError, TimeoutError, ValueError):
                if attempt == 2:
                    return name, None
        return name, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for name, times in pool.map(work, by_name):
            if times is None:
                unresolved.append(name)
                for p in by_name[name]:
                    ages[p] = None
                continue
            for p in by_name[name]:
                stamp = times.get(p.version)
                ages[p] = _parse_iso(stamp) if stamp else None
    return ages, unresolved


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--npm-registry", default=DEFAULT_NPM_REGISTRY)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat unresolved (network-failed) packages as failures",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.max_age_days)
    violations: list[str] = []
    unresolved: list[str] = []

    # ---- Python (offline: ages come straight from uv.lock) ----
    uv_exempt = load_uv_exemptions()
    uv_pins = parse_uv_lock()
    for pin, published in uv_pins:
        if pin.name.lower() in uv_exempt:
            continue
        if published is None:
            unresolved.append(f"pypi:{pin.name}@{pin.version} (no upload-time in uv.lock)")
        elif published > cutoff:
            age = (now - published).days
            violations.append(
                f"  pypi  {pin.name}@{pin.version} — published {published:%Y-%m-%d} "
                f"({age}d ago, < {args.max_age_days}d)"
            )

    # ---- JS (fetch publish times from the npm registry) ----
    npm_pins = parse_pnpm_lock()
    npm_ages, npm_unresolved = resolve_npm_ages(
        npm_pins, args.npm_registry, args.jobs, args.timeout
    )
    unresolved.extend(f"npm:{n} (registry lookup failed)" for n in npm_unresolved)
    for pin in npm_pins:
        published = npm_ages.get(pin)
        if published is None:
            if pin.name not in npm_unresolved:
                unresolved.append(
                    f"npm:{pin.name}@{pin.version} (version not in registry time map)"
                )
        elif published > cutoff:
            age = (now - published).days
            violations.append(
                f"  npm   {pin.name}@{pin.version} — published {published:%Y-%m-%d} "
                f"({age}d ago, < {args.max_age_days}d)"
            )

    print(
        f"Dependency age audit: checked {len(uv_pins)} PyPI + {len(npm_pins)} npm "
        f"pins against a {args.max_age_days}-day cooldown."
    )

    if violations:
        print(f"\n{len(violations)} dependency(ies) newer than the cooldown window:\n")
        print("\n".join(sorted(violations)))
        print(
            "\nThese should not be pinned yet. Regenerate the lockfile with the "
            "cooldown active, or add a scoped exemption if the pin is intentional."
        )

    if unresolved:
        print(f"\n{len(unresolved)} pin(s) could not be verified:")
        print("\n".join(f"  - {u}" for u in sorted(unresolved)))

    if violations:
        return 1
    if unresolved and args.strict:
        print("\n--strict: failing because some pins could not be verified.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
