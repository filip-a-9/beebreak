"""Matchmaking: queue entries, matches, participants, blocks.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("ends_at", _ts(), nullable=False),
        sa.Column("ended_at", _ts(), nullable=True),
        sa.Column("end_reason", sa.String(16), nullable=True),
        sa.Column("shared_tags", sa.String(200), nullable=True),
        sa.Column("shared_category", sa.String(16), nullable=True),
        sa.CheckConstraint("status IN ('active', 'ended')", name="ck_matches_status"),
        sa.CheckConstraint(
            "end_reason IN ('ended', 'blocked', 'expired', 'disabled')",
            name="ck_matches_end_reason",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_matches"),
    )
    op.create_table(
        "queue_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("presence_id", sa.Uuid(), nullable=True),
        sa.Column("task_category", sa.String(16), nullable=True),
        sa.Column("stack_tags", sa.String(200), nullable=True),
        sa.Column("remaining_time_bucket", sa.String(16), nullable=True),
        sa.Column("admitted_at", _ts(), nullable=False),
        sa.Column("expires_at", _ts(), nullable=False),
        sa.Column("ended_at", _ts(), nullable=True),
        sa.Column("match_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'matched', 'cancelled', 'expired')",
            name="ck_queue_entries_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_queue_entries_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name="fk_queue_entries_device_id_devices"
        ),
        sa.ForeignKeyConstraint(
            ["match_id"], ["matches.id"], name="fk_queue_entries_match_id_matches"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_queue_entries"),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_queue_entries_user_idempotency"
        ),
    )
    op.create_index(
        "uq_queue_entries_user_queued",
        "queue_entries",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
        sqlite_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_queue_entries_status_expires", "queue_entries", ["status", "expires_at"]
    )
    op.create_table(
        "match_participants",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["match_id"], ["matches.id"], name="fk_match_participants_match_id_matches"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_match_participants_user_id_users"
        ),
        sa.PrimaryKeyConstraint("match_id", "user_id", name="pk_match_participants"),
    )
    op.create_index(
        "uq_match_participants_user_active",
        "match_participants",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )
    op.create_table(
        "blocks",
        sa.Column("blocker_id", sa.Uuid(), nullable=False),
        sa.Column("blocked_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.CheckConstraint("blocker_id <> blocked_id", name="ck_blocks_not_self"),
        sa.ForeignKeyConstraint(
            ["blocker_id"], ["users.id"], name="fk_blocks_blocker_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["blocked_id"], ["users.id"], name="fk_blocks_blocked_id_users"
        ),
        sa.PrimaryKeyConstraint("blocker_id", "blocked_id", name="pk_blocks"),
    )


def downgrade() -> None:
    op.drop_table("blocks")
    op.drop_index("uq_match_participants_user_active", table_name="match_participants")
    op.drop_table("match_participants")
    op.drop_index("ix_queue_entries_status_expires", table_name="queue_entries")
    op.drop_index("uq_queue_entries_user_queued", table_name="queue_entries")
    op.drop_table("queue_entries")
    op.drop_table("matches")
