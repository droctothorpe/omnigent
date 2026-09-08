"""Add cli_login_tickets table for the replica-safe CLI login handoff.

Revision ID: gc1a2b3c4d5e
Revises: gb1b2c3d4e5f
Create Date: 2026-09-08

CLI login tickets (POST /auth/cli-login → system browser →
GET /auth/cli-poll) were process-local, so a replicated deployment
behind a load balancer could only fulfill a ticket on the replica that
minted it — polls landing elsewhere got 410 and the client gave up.
One row per ticket, keyed by the HMAC-SHA256 digest of the ticket id;
no raw secret is stored. See omnigent/server/cli_ticket_store.py.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "gc1a2b3c4d5e"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the cli_login_tickets table and its expiry index."""
    op.create_table(
        "cli_login_tickets",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("ticket_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "ticket_hash"),
    )
    op.create_index(
        "ix_cli_login_tickets_expires_at",
        "cli_login_tickets",
        ["workspace_id", "expires_at"],
    )


def downgrade() -> None:
    """Drop the cli_login_tickets table and its index."""
    op.drop_index("ix_cli_login_tickets_expires_at", table_name="cli_login_tickets")
    op.drop_table("cli_login_tickets")
