"""Persistent client-side state for ``omnigent codex`` sessions.

The native Codex wrapper records the cwd used to create a session so a
later ``omnigent codex --resume <conv_id>`` can launch Codex from the
same workspace. This state is intentionally client-side: local
filesystem paths belong to the user's machine and should not be stored
on the shared Omnigent server.

Layout (per conversation):

    ~/.omnigent/codex-native/<sha256(conv_id)[:32]>/launch.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from omnigent.process_logging import data_dir

_STATE_ROOT_ENV_VAR = "OMNIGENT_CODEX_NATIVE_STATE_DIR"
_logger = logging.getLogger(__name__)
_LAUNCH_FILE = "launch.json"
_DISCOVERED_MODELS_FILE = "discovered-models.json"
_ID_HASH_CHARS = 32


@dataclass(frozen=True)
class CodexNativeLaunchState:
    """
    Persisted state about how a codex-native session was launched.

    :param working_directory: Absolute filesystem path the wrapper
        was invoked from when the session was created, e.g.
        ``"/home/me/repo"``.
    """

    working_directory: str


def _codex_native_state_root() -> Path:
    """
    Return the root directory for persistent codex-native state.

    Honors :data:`_STATE_ROOT_ENV_VAR` for tests and advanced local
    setups. Otherwise follows ``OMNIGENT_DATA_DIR``, falling back to
    ``~/.omnigent/codex-native``.

    :returns: Absolute path to the state root.
    """
    override = os.environ.get(_STATE_ROOT_ENV_VAR)
    if override:
        return Path(override)
    return data_dir() / "codex-native"


def _state_dir_for_conversation_id(conversation_id: str) -> Path:
    """
    Return the per-conversation persistent state directory.

    Hashing the conversation id prevents path traversal if a server
    ever returned an attacker-controlled id such as ``"../etc"``.

    Sessions created before ids dropped the ``conv_`` prefix hashed the
    prefixed string, so their directories live under the legacy digest; when
    the bare-digest directory is absent, the legacy one is returned (never
    renamed — files inside may embed their own absolute path).

    :param conversation_id: Omnigent conversation id, bare 32-char hex
        (a legacy ``conv_``-prefixed form is accepted and normalised).
    :returns: Absolute directory path; not guaranteed to exist.
    """
    bare = conversation_id.removeprefix("conv_")
    root = _codex_native_state_root()
    state_dir = root / hashlib.sha256(bare.encode("utf-8")).hexdigest()[:_ID_HASH_CHARS]
    if not state_dir.exists():
        legacy = root / hashlib.sha256(f"conv_{bare}".encode()).hexdigest()[:_ID_HASH_CHARS]
        if legacy.exists():
            return legacy
    return state_dir


def write_launch_state(conversation_id: str, working_directory: str) -> None:
    """
    Persist a session's launch state at creation time.

    Same-value writes are idempotent. Different-value writes are
    refused and logged because changing the recorded cwd for an
    existing session would make future resume checks incorrect.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param working_directory: Absolute launch cwd, e.g.
        ``"/home/me/repo"``.
    :returns: None.
    :raises ValueError: If *working_directory* is empty or relative.
    """
    if not working_directory:
        raise ValueError("working_directory must be a non-empty absolute path")
    if not Path(working_directory).is_absolute():
        raise ValueError("working_directory must be a non-empty absolute path")
    state_dir = _state_dir_for_conversation_id(conversation_id)
    existing = read_launch_state(conversation_id)
    if existing is not None and existing.working_directory != working_directory:
        _logger.warning(
            "codex-native launch state mismatch for %s: existing=%r new=%r; "
            "keeping existing value",
            conversation_id,
            existing.working_directory,
            working_directory,
        )
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / _LAUNCH_FILE
    payload = {
        "conversation_id": conversation_id,
        "working_directory": working_directory,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def read_launch_state(conversation_id: str) -> CodexNativeLaunchState | None:
    """
    Load a session's launch state, or ``None`` if not recorded.

    Missing, unreadable, or malformed state is treated as absent so
    legacy and cross-machine resumes continue to behave as before.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Parsed state, or ``None`` if missing / malformed.
    """
    target = _state_dir_for_conversation_id(conversation_id) / _LAUNCH_FILE
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        _logger.warning(
            "codex-native launch state read failed for %s",
            conversation_id,
            exc_info=True,
        )
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning(
            "codex-native launch state JSON is malformed for %s; ignoring",
            conversation_id,
        )
        return None
    if not isinstance(payload, dict):
        return None
    working_directory = payload.get("working_directory")
    if not isinstance(working_directory, str) or not working_directory:
        return None
    return CodexNativeLaunchState(working_directory=working_directory)


def read_discovered_codex_models(host: str) -> tuple[str, ...]:
    """Codex ids a live workspace listing previously reported for *host*.

    Written only from a successful Unity Catalog listing, so unlike
    externally-written ucode state the ids carry the spelling the workspace
    actually serves. Missing, unreadable, or malformed state reads as empty.

    :param host: Workspace origin, e.g. ``"https://example.com"``.
    :returns: The persisted served ids, or ``()`` when none are recorded.
    """
    target = _codex_native_state_root() / _DISCOVERED_MODELS_FILE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, json.JSONDecodeError):
        _logger.warning("discovered codex models read failed for %s", host, exc_info=True)
        return ()
    if not isinstance(raw, dict):
        return ()
    models = raw.get(host.rstrip("/"))
    if not isinstance(models, list):
        return ()
    return tuple(str(model) for model in models)


def write_discovered_codex_models(host: str, models: Iterable[str]) -> None:
    """Record the codex ids a live listing reported for *host*.

    Best-effort: the record is an optimization for later pinned launches, so
    failures are logged and swallowed rather than failing this launch.

    :param host: Workspace origin, e.g. ``"https://example.com"``.
    :param models: Served ids exactly as the listing reported them.
    :returns: None.
    """
    root = _codex_native_state_root()
    target = root / _DISCOVERED_MODELS_FILE
    try:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw[host.rstrip("/")] = list(models)
        root.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        _logger.warning("discovered codex models write failed for %s", host, exc_info=True)
