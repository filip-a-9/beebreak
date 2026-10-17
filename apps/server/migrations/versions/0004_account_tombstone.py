"""Account tombstone: keyed hash of the email of a deleted disabled account.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deleted_account_tombstones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email_hmac", sa.LargeBinary(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_hmac"),
    )


def downgrade() -> None:
    op.drop_table("deleted_account_tombstones")
