"""The persisted session effort must govern the codex-native terminal launch.

A Codex reasoning effort selected in the new-session composer is persisted on
the session (``reasoning_effort``), but the runner's codex-native launch used
to drop it: the app-server and the ``--remote`` TUI booted with no
``model_reasoning_effort``, so the codex thread ran at the model default while
the composer kept claiming the selected effort. These tests drive the real
``_auto_create_codex_terminal`` through the launch harness and pin the
effort's ride-along on both the app-server build and the TUI argv (the TUI
loads its own config and does not inherit the app-server's ``-c`` flags).
"""

from __future__ import annotations

import pytest

from tests.runner.test_codex_model_pick_fallback import (  # noqa: F401  (pytest fixture)
    _LaunchHarness,
    codex_launch_harness,
)


@pytest.mark.asyncio
async def test_persisted_effort_rides_launch_config_overrides(
    codex_launch_harness: _LaunchHarness,  # noqa: F811  (imported fixture)
) -> None:
    """The persisted effort reaches the app-server build AND the TUI argv."""
    harness = codex_launch_harness
    harness.snapshot["reasoning_effort"] = "high"

    await harness.launch()

    assert 'model_reasoning_effort="high"' in harness.builds[0]["extra_config_overrides"], (
        "the app-server must boot the thread at the session's persisted effort"
    )
    terminal_kwargs = harness.registry.launch_auxiliary_terminal.call_args.kwargs
    assert 'model_reasoning_effort="high"' in terminal_kwargs["spec"].args, (
        "the --remote TUI resolves its own config, so the effort override "
        "must ride its -c flags too"
    )


@pytest.mark.asyncio
async def test_unsupported_persisted_effort_does_not_sink_the_launch(
    codex_launch_harness: _LaunchHarness,  # noqa: F811  (imported fixture)
) -> None:
    """A foreign persisted effort is dropped; the terminal still launches."""
    harness = codex_launch_harness
    harness.snapshot["reasoning_effort"] = "turbo"

    await harness.launch()

    assert not any(
        override.startswith("model_reasoning_effort=")
        for override in harness.builds[0]["extra_config_overrides"]
    ), "an effort codex cannot accept must not reach its config"
    assert "terminal-launch" in harness.events, "the launch itself must still succeed"


@pytest.mark.asyncio
async def test_launch_without_persisted_effort_adds_no_override(
    codex_launch_harness: _LaunchHarness,  # noqa: F811  (imported fixture)
) -> None:
    """No persisted effort means Codex keeps its own default — no override."""
    harness = codex_launch_harness

    await harness.launch()

    assert not any(
        override.startswith("model_reasoning_effort=")
        for override in harness.builds[0]["extra_config_overrides"]
    )
