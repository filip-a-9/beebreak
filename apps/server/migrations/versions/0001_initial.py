"""Initial schema: users, invites, challenges, devices, tokens, preferences.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email_canonical", sa.String(254), nullable=False),
        sa.Column("nickname", sa.String(24), nullable=True),
        sa.Column("nickname_skeleton", sa.String(24), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email_canonical", name="uq_users_email_canonical"),
        sa.UniqueConstraint("nickname_skeleton", name="uq_users_nickname_skeleton"),
    )
    op.create_table(
        "beta_invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email_canonical", sa.String(254), nullable=False),
        sa.Column("code_hmac", sa.LargeBinary(32), nullable=False),
        sa.Column("expires_at", _ts(), nullable=False),
        sa.Column("redeemed_at", _ts(), nullable=True),
        sa.Column("redeemed_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", _ts(), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["redeemed_by"], ["users.id"], name="fk_beta_invites_redeemed_by_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_beta_invites"),
        sa.UniqueConstraint("code_hmac", name="uq_beta_invites_code_hmac"),
    )
    op.create_table(
        "auth_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email_canonical", sa.String(254), nullable=False),
        sa.Column("code_hmac", sa.LargeBinary(32), nullable=False),
        sa.Column("invite_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", _ts(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consumed_at", _ts(), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_auth_challenges_attempts_nonneg"),
        sa.ForeignKeyConstraint(
            ["invite_id"],
            ["beta_invites.id"],
            name="fk_auth_challenges_invite_id_beta_invites",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_challenges"),
    )
    op.create_index(
        "ix_auth_challenges_email_consumed",
        "auth_challenges",
        ["email_canonical", "consumed_at"],
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("access_hash", sa.LargeBinary(32), nullable=True),
        sa.Column("access_expires_at", _ts(), nullable=True),
        sa.Column("revoked_at", _ts(), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("last_seen_at", _ts(), nullable=True),
        sa.Column("connected_at", _ts(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_devices_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_devices"),
        sa.UniqueConstraint("access_hash", name="uq_devices_access_hash"),
    )
    op.create_index("ix_devices_user_id", "devices", ["user_id"])
    op.create_table(
        "refresh_tokens",
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", _ts(), nullable=False),
        sa.Column("used_at", _ts(), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name="fk_refresh_tokens_device_id_devices"
        ),
        sa.PrimaryKeyConstraint("token_hash", name="pk_refresh_tokens"),
    )
    op.create_index("ix_refresh_tokens_device_id", "refresh_tokens", ["device_id"])
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("consent_version", sa.Integer(), nullable=False),
        sa.Column("share_task_category", sa.Boolean(), nullable=False),
        sa.Column("share_stack_tags", sa.Boolean(), nullable=False),
        sa.Column("share_time_bucket", sa.Boolean(), nullable=False),
        sa.Column("consented_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_preferences_user_id_users"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_preferences"),
    )


def downgrade() -> None:
    op.drop_table("user_preferences")
    op.drop_index("ix_refresh_tokens_device_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_devices_user_id", table_name="devices")
    op.drop_table("devices")
    op.drop_index("ix_auth_challenges_email_consumed", table_name="auth_challenges")
    op.drop_table("auth_challenges")
    op.drop_table("beta_invites")
    op.drop_table("users")
