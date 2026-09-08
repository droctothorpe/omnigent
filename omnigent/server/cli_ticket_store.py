"""Persistence for CLI login tickets (``/auth/cli-login`` → ``/auth/cli-poll``).

Backs the ticket-based login handoff used by ``omnigent login`` and the
native shells: the client requests a ticket, the user signs in via the
browser, and the client polls for its session. The ticket used to live in
a process-local dict, which broke replicated deployments — a load
balancer with no session affinity routes the browser callback and the
client's cookieless polls to arbitrary replicas, so a ticket fulfilled on
one replica looked expired (410) everywhere else and the client gave up.

Sibling to :class:`omnigent.server.device_grant_store.DeviceGrantStore`
— same database, separate API surface, same secret discipline: the
ticket id is kept only as an HMAC-SHA256 digest (callers hash with
:func:`omnigent.server.device_grant_store.hash_secret`), and fulfillment
records only the authenticated ``user_id``. The session token and
refresh grant are minted at redemption time by whichever replica answers
the poll, so a database read never yields a usable credential.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import and_, delete, update
from sqlalchemy.engine import CursorResult

from omnigent.db.db_models import SqlCliLoginTicket, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker


class CliTicketStore:
    """SQLAlchemy-backed persistence for CLI login tickets.

    Concrete class (no ABC) — there is exactly one backend today,
    matching the other server stores so wiring in ``create_app`` is
    mechanical.

    :param storage_location: SQLAlchemy database URI. Shares the
        connection pool with the other stores via
        :func:`get_or_create_engine`.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.cli_ticket_store",
        )

    def create_ticket(
        self,
        ticket_hash: str,
        *,
        created_at: int,
        expires_at: int,
    ) -> None:
        """Persist a new pending ticket.

        :param ticket_hash: HMAC digest of the secret ticket id; the
            store never sees the raw ticket.
        :param created_at: Unix epoch seconds.
        :param expires_at: Unix epoch seconds after which the ticket is
            no longer fulfillable or redeemable.
        """
        with self._session("insert_cli_login_ticket") as session:
            session.add(
                SqlCliLoginTicket(
                    ticket_hash=ticket_hash,
                    user_id=None,
                    created_at=created_at,
                    expires_at=expires_at,
                )
            )

    def fulfill(
        self,
        ticket_hash: str,
        *,
        user_id: str,
        now_epoch_seconds: int,
    ) -> bool:
        """Atomically bind the just-authenticated identity to a pending ticket.

        A single ``UPDATE … WHERE user_id IS NULL`` + rowcount check makes
        fulfillment race-safe: a replayed callback cannot rebind a ticket,
        and an expired or unknown one is left untouched.

        :param ticket_hash: HMAC digest of the ticket id from the signed
            login state.
        :param user_id: The authenticated identity to hand to the poller.
        :param now_epoch_seconds: Unix epoch seconds, for the expiry gate.
        :returns: True when a live pending ticket was fulfilled.
        """
        with self._session("fulfill_cli_login_ticket") as session:
            result = cast(
                "CursorResult[tuple[object]]",
                session.execute(
                    update(SqlCliLoginTicket)
                    .where(
                        and_(
                            SqlCliLoginTicket.workspace_id == current_workspace_id(),
                            SqlCliLoginTicket.ticket_hash == ticket_hash,
                            SqlCliLoginTicket.user_id.is_(None),
                            SqlCliLoginTicket.expires_at >= now_epoch_seconds,
                        )
                    )
                    .values(user_id=user_id)
                ),
            )
            return result.rowcount == 1

    def poll(
        self,
        ticket_hash: str,
        *,
        now_epoch_seconds: int,
    ) -> tuple[str, str | None]:
        """Resolve a poll: pending, redeemable, or gone.

        Redemption is single-use and race-safe: the fulfilled row is
        deleted with a guarded ``DELETE`` + rowcount check, so concurrent
        polls for the same ticket yield the identity to exactly one caller.

        :param ticket_hash: HMAC digest of the ticket id being polled.
        :param now_epoch_seconds: Unix epoch seconds, for the expiry gate.
        :returns: ``(outcome, user_id)`` where outcome is ``"not_found"``
            (unknown, expired, or already redeemed), ``"pending"``, or
            ``"claimed"`` — only ``"claimed"`` carries a ``user_id``.
        """
        with self._session("poll_cli_login_ticket") as session:
            row = session.get(SqlCliLoginTicket, (current_workspace_id(), ticket_hash))
            if row is None:
                return ("not_found", None)
            if row.expires_at < now_epoch_seconds:
                session.delete(row)
                return ("not_found", None)
            if row.user_id is None:
                return ("pending", None)
            user_id = row.user_id
            result = cast(
                "CursorResult[tuple[object]]",
                session.execute(
                    delete(SqlCliLoginTicket).where(
                        and_(
                            SqlCliLoginTicket.workspace_id == current_workspace_id(),
                            SqlCliLoginTicket.ticket_hash == ticket_hash,
                            SqlCliLoginTicket.user_id.is_not(None),
                        )
                    )
                ),
            )
            if result.rowcount == 0:
                return ("not_found", None)
            return ("claimed", user_id)

    def purge_expired(self, *, now_epoch_seconds: int) -> int:
        """Delete expired tickets; returns how many were removed.

        Called opportunistically before minting a new ticket so abandoned
        login attempts don't accumulate.
        """
        with self._session("purge_expired_cli_login_tickets") as session:
            result = cast(
                "CursorResult[tuple[object]]",
                session.execute(
                    delete(SqlCliLoginTicket).where(
                        and_(
                            SqlCliLoginTicket.workspace_id == current_workspace_id(),
                            SqlCliLoginTicket.expires_at < now_epoch_seconds,
                        )
                    )
                ),
            )
            return int(result.rowcount or 0)
