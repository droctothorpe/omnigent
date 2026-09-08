"""Tests for :class:`omnigent.server.cli_ticket_store.CliTicketStore`.

The store backs the ticket-based login handoff (``/auth/cli-login`` →
``/auth/cli-poll``) with the shared database so a replicated deployment
can fulfill and redeem a ticket on any replica. These cover the
lifecycle invariants that make that safe: pending → fulfilled →
claimed-exactly-once, expiry gates on both fulfillment and redemption,
and purge (unit, against a real SQLite DB).
"""

from __future__ import annotations

from pathlib import Path

from omnigent.server.cli_ticket_store import CliTicketStore
from omnigent.server.device_grant_store import hash_secret

_KEY = b"k" * 32
_NOW = 1_700_000_000
_TTL = 300


def _store(tmp_path: Path) -> CliTicketStore:
    return CliTicketStore(f"sqlite:///{tmp_path}/tickets.db")


def _ticket(store: CliTicketStore, raw: str = "raw-ticket-id") -> str:
    """Create a live ticket and return its hash (the store-facing key)."""
    ticket_hash = hash_secret(raw, _KEY)
    store.create_ticket(ticket_hash, created_at=_NOW, expires_at=_NOW + _TTL)
    return ticket_hash


def test_poll_is_pending_until_fulfilled(tmp_path: Path) -> None:
    """A freshly minted ticket polls pending, carrying no identity."""
    store = _store(tmp_path)
    ticket_hash = _ticket(store)

    assert store.poll(ticket_hash, now_epoch_seconds=_NOW) == ("pending", None)


def test_fulfilled_ticket_is_claimed_exactly_once(tmp_path: Path) -> None:
    """Fulfillment hands the identity to exactly one subsequent poll."""
    store = _store(tmp_path)
    ticket_hash = _ticket(store)

    assert store.fulfill(ticket_hash, user_id="alice@example.com", now_epoch_seconds=_NOW)
    assert store.poll(ticket_hash, now_epoch_seconds=_NOW) == ("claimed", "alice@example.com")
    # Single-use: the winning poll consumed the ticket.
    assert store.poll(ticket_hash, now_epoch_seconds=_NOW) == ("not_found", None)


def test_fulfill_is_single_shot_and_needs_a_live_ticket(tmp_path: Path) -> None:
    """Unknown, already-fulfilled, and expired tickets refuse fulfillment."""
    store = _store(tmp_path)
    assert not store.fulfill("unknown-hash", user_id="a@b.c", now_epoch_seconds=_NOW)

    ticket_hash = _ticket(store)
    assert store.fulfill(ticket_hash, user_id="alice@example.com", now_epoch_seconds=_NOW)
    # A replayed callback must not rebind the identity.
    assert not store.fulfill(ticket_hash, user_id="mallory@evil.tld", now_epoch_seconds=_NOW)
    assert store.poll(ticket_hash, now_epoch_seconds=_NOW) == ("claimed", "alice@example.com")

    expired_hash = _ticket(store, raw="stale-ticket")
    assert not store.fulfill(
        expired_hash, user_id="alice@example.com", now_epoch_seconds=_NOW + _TTL + 1
    )


def test_poll_treats_expired_tickets_as_gone(tmp_path: Path) -> None:
    """An expired ticket polls not_found — even when it was fulfilled."""
    store = _store(tmp_path)
    ticket_hash = _ticket(store)
    assert store.fulfill(ticket_hash, user_id="alice@example.com", now_epoch_seconds=_NOW)

    assert store.poll(ticket_hash, now_epoch_seconds=_NOW + _TTL + 1) == ("not_found", None)
    # The expired row was dropped, not left behind.
    assert store.poll(ticket_hash, now_epoch_seconds=_NOW) == ("not_found", None)


def test_purge_expired_removes_only_expired_tickets(tmp_path: Path) -> None:
    """purge_expired drops stale rows and leaves live ones pollable."""
    store = _store(tmp_path)
    live_hash = _ticket(store, raw="live-ticket")
    stale_hash = hash_secret("stale-ticket", _KEY)
    store.create_ticket(stale_hash, created_at=_NOW - 2 * _TTL, expires_at=_NOW - _TTL)

    assert store.purge_expired(now_epoch_seconds=_NOW) == 1
    assert store.poll(stale_hash, now_epoch_seconds=_NOW) == ("not_found", None)
    assert store.poll(live_hash, now_epoch_seconds=_NOW) == ("pending", None)


def test_two_store_instances_share_one_database(tmp_path: Path) -> None:
    """Tickets are visible across store instances — the replica property.

    Two instances on one database stand in for two server replicas: a
    ticket minted through one is pollable, fulfillable, and redeemable
    through the other.
    """
    store_a = _store(tmp_path)
    store_b = CliTicketStore(f"sqlite:///{tmp_path}/tickets.db")

    ticket_hash = _ticket(store_a)
    assert store_b.poll(ticket_hash, now_epoch_seconds=_NOW) == ("pending", None)
    assert store_b.fulfill(ticket_hash, user_id="alice@example.com", now_epoch_seconds=_NOW)
    assert store_a.poll(ticket_hash, now_epoch_seconds=_NOW) == ("claimed", "alice@example.com")
    assert store_b.poll(ticket_hash, now_epoch_seconds=_NOW) == ("not_found", None)
