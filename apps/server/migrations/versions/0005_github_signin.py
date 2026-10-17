"""GitHub sign-in: github_id on users and invites, device-flow challenge columns.

Email becomes optional on users, invites and challenges; each row has an email
or a GitHub identity (users: at least one, invites: exactly one, challenges:
an email code or a GitHub poll interval).

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "email_canonical", existing_type=sa.String(254), nullable=True
        )
        batch.add_column(sa.Column("github_id", sa.BigInteger(), nullable=True))
        batch.create_unique_constraint("uq_users_github_id", ["github_id"])
        batch.create_check_constraint(
            "ck_users_identity", "email_canonical IS NOT NULL OR github_id IS NOT NULL"
        )
    with op.batch_alter_table("beta_invites") as batch:
        batch.alter_column(
            "email_canonical", existing_type=sa.String(254), nullable=True
        )
        batch.alter_column("code_hmac", existing_type=sa.LargeBinary(32), nullable=True)
        batch.add_column(sa.Column("github_id", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_beta_invites_identity",
            "(email_canonical IS NULL) <> (github_id IS NULL)",
        )
    with op.batch_alter_table("auth_challenges") as batch:
        batch.alter_column(
            "email_canonical", existing_type=sa.String(254), nullable=True
        )
        batch.alter_column("code_hmac", existing_type=sa.LargeBinary(32), nullable=True)
        batch.add_column(sa.Column("github_device_code", sa.String(64), nullable=True))
        batch.add_column(sa.Column("github_interval_s", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("github_next_poll_at", _ts(), nullable=True))
        batch.create_check_constraint(
            "ck_auth_challenges_method",
            "(email_canonical IS NOT NULL AND code_hmac IS NOT NULL) "
            "OR github_interval_s IS NOT NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM users WHERE github_id IS NOT NULL")).first():
        # Dropping the column would orphan the accounts (and their devices).
        raise RuntimeError("cannot downgrade: GitHub accounts exist")
    # Ephemeral or unredeemable without GitHub sign-in: drop them.
    op.execute(
        "DELETE FROM auth_challenges "
        "WHERE github_interval_s IS NOT NULL OR email_canonical IS NULL"
    )
    op.execute("DELETE FROM beta_invites WHERE email_canonical IS NULL")
    with op.batch_alter_table("auth_challenges") as batch:
        batch.drop_constraint("ck_auth_challenges_method", type_="check")
        batch.drop_column("github_next_poll_at")
        batch.drop_column("github_interval_s")
        batch.drop_column("github_device_code")
        batch.alter_column(
            "code_hmac", existing_type=sa.LargeBinary(32), nullable=False
        )
        batch.alter_column(
            "email_canonical", existing_type=sa.String(254), nullable=False
        )
    with op.batch_alter_table("beta_invites") as batch:
        batch.drop_constraint("ck_beta_invites_identity", type_="check")
        batch.drop_column("github_id")
        batch.alter_column(
            "code_hmac", existing_type=sa.LargeBinary(32), nullable=False
        )
        batch.alter_column(
            "email_canonical", existing_type=sa.String(254), nullable=False
        )
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("ck_users_identity", type_="check")
        batch.drop_constraint("uq_users_github_id", type_="unique")
        batch.drop_column("github_id")
        batch.alter_column(
            "email_canonical", existing_type=sa.String(254), nullable=False
        )
