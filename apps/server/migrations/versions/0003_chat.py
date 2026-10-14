"""Chat: chat columns on matches and participants, messages, reports.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_M4_END_REASONS = "end_reason IN ('ended', 'blocked', 'expired', 'disabled')"
_M5_END_REASONS = (
    "end_reason IN ('ended', 'blocked', 'expired', 'disabled', "
    "'disconnected', 'reported')"
)


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    # Batch mode: SQLite cannot alter a check constraint in place.
    with op.batch_alter_table("matches") as batch:
        batch.add_column(
            sa.Column(
                "extensions_used", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(sa.Column("extension_requested_by", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0")
        )
        batch.create_foreign_key(
            "fk_matches_extension_requested_by_users",
            "users",
            ["extension_requested_by"],
            ["id"],
        )
        batch.drop_constraint("ck_matches_end_reason", type_="check")
        batch.create_check_constraint("ck_matches_end_reason", _M5_END_REASONS)
        batch.create_check_constraint(
            "ck_matches_extensions_used_nonneg", "extensions_used >= 0"
        )
    with op.batch_alter_table("match_participants") as batch:
        batch.add_column(sa.Column("offline_since", _ts(), nullable=True))
        batch.add_column(
            sa.Column("messages_sent", sa.Integer(), nullable=False, server_default="0")
        )

    op.create_table(
        "messages",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("client_message_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.CheckConstraint("seq >= 1", name="ck_messages_seq_positive"),
        sa.ForeignKeyConstraint(
            ["match_id"],
            ["matches.id"],
            name="fk_messages_match_id_matches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_messages_author_id_users"
        ),
        sa.PrimaryKeyConstraint("match_id", "seq", name="pk_messages"),
        sa.UniqueConstraint(
            "author_id", "client_message_id", name="uq_messages_author_client_id"
        ),
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reporter_id", sa.Uuid(), nullable=False),
        sa.Column("reported_id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("note", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("resolved_at", _ts(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'dismissed', 'actioned')", name="ck_reports_status"
        ),
        sa.CheckConstraint(
            "reason IN ('harassment', 'spam', 'sexual', 'hate', 'other')",
            name="ck_reports_reason",
        ),
        sa.CheckConstraint("reporter_id <> reported_id", name="ck_reports_not_self"),
        sa.ForeignKeyConstraint(
            ["reporter_id"], ["users.id"], name="fk_reports_reporter_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["reported_id"], ["users.id"], name="fk_reports_reported_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
        sa.UniqueConstraint(
            "reporter_id", "match_id", name="uq_reports_reporter_match"
        ),
    )
    op.create_index("ix_reports_status_created", "reports", ["status", "created_at"])
    op.create_table(
        "report_messages",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("author", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sent_at", _ts(), nullable=False),
        sa.CheckConstraint(
            "author IN ('reporter', 'reported')", name="ck_report_messages_author"
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name="fk_report_messages_report_id_reports",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("report_id", "seq", name="pk_report_messages"),
    )


def downgrade() -> None:
    op.drop_table("report_messages")
    op.drop_index("ix_reports_status_created", table_name="reports")
    op.drop_table("reports")
    op.drop_table("messages")
    with op.batch_alter_table("match_participants") as batch:
        batch.drop_column("messages_sent")
        batch.drop_column("offline_since")
    # The M4 constraint does not know the M5 reasons: map them to their closest
    # M4 meaning first.
    op.execute(
        "UPDATE matches SET end_reason = 'ended' WHERE end_reason = 'disconnected'"
    )
    op.execute(
        "UPDATE matches SET end_reason = 'blocked' WHERE end_reason = 'reported'"
    )
    with op.batch_alter_table("matches") as batch:
        batch.drop_constraint("ck_matches_extensions_used_nonneg", type_="check")
        batch.drop_constraint("ck_matches_end_reason", type_="check")
        batch.create_check_constraint("ck_matches_end_reason", _M4_END_REASONS)
        batch.drop_constraint(
            "fk_matches_extension_requested_by_users", type_="foreignkey"
        )
        batch.drop_column("last_seq")
        batch.drop_column("extension_requested_by")
        batch.drop_column("extensions_used")
