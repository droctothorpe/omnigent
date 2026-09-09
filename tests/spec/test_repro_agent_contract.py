from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPRO_AGENT_INSTRUCTIONS = _REPO_ROOT / "dev" / "repro-agent" / "AGENTS.md"
_RECORDING_LANES = _REPO_ROOT / "dev" / "recording-lanes.md"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_recording_mandate_attaches_recorder_to_reproduction_owned_server() -> None:
    """A reproduction's own live stack must be attempted as a recording target.

    When the journey needs a server the stock ``tests/e2e_ui`` fixture can't
    spawn, the web lane isn't unfilmable — the recorder attaches to the server
    the reproduction already built. The mandate must say so, or agents skip
    the lane by citing the stock fixture's limits without ever attempting the
    attach.
    """
    instructions = _normalized(_REPRO_AGENT_INSTRUCTIONS)

    assert "own live stack" in instructions
    assert "point the recorder at the server you already have running" in instructions
    assert (
        "only an attempted attach that failed, with the command and error quoted"
        in instructions
    )


def test_recording_lanes_show_how_to_film_against_a_caller_owned_server() -> None:
    """The lane doc must carry the mechanics for a caller-owned server.

    ``--ui-base-url`` skips the fixture's own build/spawn and drives the URL
    it's given, so a bespoke stand-in stack is filmable; skipping the lane is
    justified only by a quoted failed attempt, never by the stock fixture's
    limits.
    """
    lanes = _normalized(_RECORDING_LANES)

    assert "A live server your reproduction built is a recording target" in lanes
    assert "--ui-base-url http://127.0.0.1:<port>" in lanes
    assert "OMNIGENT_E2E_ALLOW_DEV_BASE_URL" in lanes
    assert (
        "quote the failing command and its error in `recording_unavailable_reason`"
        in lanes
    )


def test_static_text_escape_excludes_transient_error_moments() -> None:
    """An error appearing mid-journey is watchable, not static text.

    The static-text escape covers outcomes where nothing on the surface moves;
    a mid-turn error flashing in a live session (and the session recovering)
    is a temporal moment both docs must classify as filmable.
    """
    for path in (_REPRO_AGENT_INSTRUCTIONS, _RECORDING_LANES):
        assert "is a screen changing, not static text" in _normalized(path), path
