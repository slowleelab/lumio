"""add_classifier_sample_table

闭环 P1 感知层: 分类样本表 (TrapCollector 采样落库).
记录被感知采样捕获的分类样本 (低置信/贴近阈值/规则-BERT 分歧/慢路径),
用于失败样本回流 + 漂移监控 + 有界留存.

GDPR: customer_id 索引用于客户级批量删除; created_at 用于留存清理.
Revision ID: a1b2c3d4e5f6
Revises: c7d8e9f0a1b2
Create Date: 2026-08-15 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "5f63f057bc85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    from sqlalchemy import inspect

    inspector = inspect(conn)
    if "classifier_sample" in inspector.get_table_names():
        return

    op.execute(
        sa.text("""
        CREATE TABLE classifier_sample (
            id UUID PRIMARY KEY,
            session_id VARCHAR(64),
            customer_id VARCHAR(64),
            text TEXT NOT NULL,
            fast_source VARCHAR(16) NOT NULL,
            fast_intent VARCHAR(32) NOT NULL,
            fast_confidence REAL NOT NULL,
            rule_intent VARCHAR(32),
            final_source VARCHAR(16) NOT NULL,
            final_intent VARCHAR(32) NOT NULL,
            final_confidence REAL NOT NULL,
            margin REAL NOT NULL DEFAULT 0.0,
            reasons JSON NOT NULL DEFAULT '[]'::json,
            divergence BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    )

    op.create_index("ix_classifier_sample_customer_created", "classifier_sample", ["customer_id", "created_at"])
    op.create_index("ix_classifier_sample_intent_created", "classifier_sample", ["final_intent", "created_at"])
    op.create_index("ix_classifier_sample_created", "classifier_sample", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_classifier_sample_created", table_name="classifier_sample")
    op.drop_index("ix_classifier_sample_intent_created", table_name="classifier_sample")
    op.drop_index("ix_classifier_sample_customer_created", table_name="classifier_sample")
    op.drop_table("classifier_sample")
