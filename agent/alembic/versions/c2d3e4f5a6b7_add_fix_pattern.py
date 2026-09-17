"""问题治理 (共性聚合层) — alembic c2d3e4f5a6b7

fix_pattern 只存治理方案; 问题组由 badcase (intent_label × root_cause_layer)
实时聚合派生, 无需数据回填。

Revision ID: c2d3e4f5a6b7
Revises: b9f8e7d6c5a4
Create Date: 2026-09-17

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "c2d3e4f5a6b7"
down_revision = "b9f8e7d6c5a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fix_pattern",
        sa.Column("id", sa.Uuid(native_uuid=False), primary_key=True),
        sa.Column("group_key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(120), nullable=False, server_default=""),
        sa.Column("intent_label", sa.String(64), nullable=True),
        sa.Column("root_cause_layer", sa.String(16), nullable=True),
        sa.Column("fix_table", sa.String(16), nullable=True),
        sa.Column("plan_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_owner", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("ix_fix_pattern_group_key", "fix_pattern", ["group_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_fix_pattern_group_key", table_name="fix_pattern")
    op.drop_table("fix_pattern")
