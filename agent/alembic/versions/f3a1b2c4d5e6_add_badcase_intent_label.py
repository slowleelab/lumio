"""badcase 加 intent_label (问题轮意图, 业务分类维度)

Revision ID: f3a1b2c4d5e6
Revises: e8f9a0b1c2d3
Create Date: 2026-09-15

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a1b2c4d5e6"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("badcase", sa.Column("intent_label", sa.String(64), nullable=True))
    op.create_index("ix_badcase_intent_label", "badcase", ["intent_label"])


def downgrade() -> None:
    op.drop_index("ix_badcase_intent_label", table_name="badcase")
    op.drop_column("badcase", "intent_label")
