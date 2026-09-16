"""提示词资产表 (prompt_template + prompt_version)

PromptOps: DB 为运营源 (版本 append-only + 指针切换), 代码常量为兜底。
lazy seed: 首次访问时由 prompt_registry 用本地常量自动建 v1, 无需数据迁移。

Revision ID: b9f8e7d6c5a4
Revises: f3a1b2c4d5e6
Create Date: 2026-09-16

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "b9f8e7d6c5a4"
down_revision = "f3a1b2c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_template",
        sa.Column("id", sa.Uuid(native_uuid=False), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column("active_version_id", sa.Uuid(native_uuid=False), nullable=True),
        sa.Column("source_case_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("ix_prompt_template_name", "prompt_template", ["name"], unique=True)

    op.create_table(
        "prompt_version",
        sa.Column("id", sa.Uuid(native_uuid=False), primary_key=True),
        sa.Column(
            "template_id",
            sa.Uuid(native_uuid=False),
            sa.ForeignKey("prompt_template.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("changelog", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("rollout_pct", sa.Float(), nullable=False, server_default="100.0"),
        sa.Column("source_case_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("ix_prompt_version_template", "prompt_version", ["template_id"])
    op.create_index("uq_prompt_version_seq", "prompt_version", ["template_id", "version"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_prompt_version_seq", table_name="prompt_version")
    op.drop_index("ix_prompt_version_template", table_name="prompt_version")
    op.drop_table("prompt_version")
    op.drop_index("ix_prompt_template_name", table_name="prompt_template")
    op.drop_table("prompt_template")
