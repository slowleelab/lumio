"""审计/质量聚合时间列索引

管理控制台按时间窗口聚合 dialogue_log / decision_log (RAG 质量看板、闭环统计),
两表此前只有 (session_id, timestamp) / (action, created_at) 复合索引,
裸时间范围扫描会退化为全表扫描。数据量增长后聚合接口明显变慢, 补单列索引。

Revision ID: a3b4c5d6e7f8
Revises: e41f0a2b3c4d
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "e41f0a2b3c4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_dialogue_log_timestamp", "dialogue_log", ["timestamp"])
    op.create_index("ix_decision_log_created", "decision_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_decision_log_created", table_name="decision_log")
    op.drop_index("ix_dialogue_log_timestamp", table_name="dialogue_log")
