from pathlib import Path

from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.store import SQLiteStore


async def test_store_persists_thread_session(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    assert await store.get_session(key) is None

    await store.upsert_session(
        key,
        "conv_1",
        "title",
        owner_user_id="U1",
        host_id="host_a",
    )
    record = await store.get_session(key)
    assert record is not None
    assert record.session_id == "conv_1"
    assert record.owner_user_id == "U1"
    assert record.host_id == "host_a"

    await store.upsert_session(key, "conv_2", "title", owner_user_id="U1")
    record = await store.get_session(key)
    assert record is not None
    assert record.session_id == "conv_2"


async def test_store_user_config_round_trip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.get_user_config("T1", "U1") is None

    config = UserConfig(
        agent_id="ag_1",
        agent_name="Helper",
        workspace="/home/me/project",
        host_id="host_a",
        host_name="Host A",
    )
    await store.upsert_user_config("T1", "U1", config)
    assert await store.get_user_config("T1", "U1") == config

    # Upsert overwrites and host may be cleared back to "any".
    updated = UserConfig(
        agent_id="ag_2",
        agent_name="Other",
        workspace="/tmp/ws",
    )
    await store.upsert_user_config("T1", "U1", updated)
    assert await store.get_user_config("T1", "U1") == updated
    # A different user in the same workspace is isolated.
    assert await store.get_user_config("T1", "U2") is None


async def test_store_claim_event_dedupes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.claim_event("Ev1") is True
    assert await store.claim_event("Ev1") is False
    assert await store.claim_event(None) is True


async def test_store_unclaim_event_allows_reclaim(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.claim_event("Ev1") is True
    # Releasing the claim lets the same event id be processed again.
    await store.unclaim_event("Ev1")
    assert await store.claim_event("Ev1") is True
    # A no-op without an id, and harmless on an unknown id.
    await store.unclaim_event(None)
    await store.unclaim_event("never-seen")


async def test_store_pending_setup_message_pop_clears_and_overwrites(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    assert await store.pop_pending_setup_message("T1", "U1") is None

    await store.upsert_pending_setup_message(
        "T1", "U1", channel_id="C1", thread_ts="100.1", text="first"
    )
    # A newer message before setup completes overwrites — never queues both.
    await store.upsert_pending_setup_message(
        "T1", "U1", channel_id="C2", thread_ts="200.1", text="second"
    )
    pending = await store.pop_pending_setup_message("T1", "U1")
    assert pending is not None
    assert (pending.channel_id, pending.thread_ts, pending.text) == ("C2", "200.1", "second")
    # Pop consumes: the message is resumed at most once.
    assert await store.pop_pending_setup_message("T1", "U1") is None
    # Users are isolated.
    await store.upsert_pending_setup_message(
        "T1", "U1", channel_id="C1", thread_ts="100.1", text="mine"
    )
    assert await store.pop_pending_setup_message("T1", "U2") is None


async def test_store_clear_user_data_drops_pending_setup_message(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()

    await store.upsert_pending_setup_message(
        "T1", "U1", channel_id="C1", thread_ts="100.1", text="hello"
    )
    await store.clear_user_data("T1", "U1")
    # Logout resets everything — a stale stash must not replay after re-setup.
    assert await store.pop_pending_setup_message("T1", "U1") is None
