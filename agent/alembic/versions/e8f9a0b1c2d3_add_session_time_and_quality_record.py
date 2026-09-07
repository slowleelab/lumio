"""质检工作台整改: badcase 会话时间锚点 + quality_record 全量判定表

- badcase.session_time: 会话最后一轮对话时间 (从 dialogue_log 回填存量)。
  工作台列表按"对话发生顺序"倒序排列, 而非采集时间 — qa_scan 批量回扫时
  created_at 只是巡检时刻, 同一批会话的采集时间全部相同, 现场时序被打乱;
  去重聚合条目也只保留首次 created_at, 看不出问题最近何时复现。
- quality_record: 全量会话质检判定持久化。此前 pass/warn 只写 Redis
  (30 天 TTL), 会话清单在工作台不可见; 本表让"每一个会话都纳入质检列表"。

Revision ID: e8f9a0b1c2d3
Revises: b5c6d7e8f9a0
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "badcase",
        sa.Column(
            "session_time",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="会话时间锚点 (该会话最后一轮对话时间); 工作台按对话发生顺序排列",
        ),
    )
    # 存量回填: 以 dialogue_log 中同会话最后一轮时间为准 (无对话记录的保持 NULL)
    op.execute(
        """
        UPDATE badcase b
        SET session_time = t.max_ts
        FROM (
            SELECT session_id, MAX(timestamp) AS max_ts
            FROM dialogue_log
            GROUP BY session_id
        ) t
        WHERE b.session_id = t.session_id
        """
    )
    op.create_index("ix_badcase_session_time", "badcase", ["session_time"])

    op.create_table(
        "quality_record",
        sa.Column("id", sa.Uuid(native_uuid=False), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False, comment="pass/warn/fail"),
        sa.Column("problems", sa.JSON, nullable=True, comment="[{type,turn,reason}] 裁判指出的问题项"),
        sa.Column("summary", sa.String(255), nullable=True),
        sa.Column("preview", sa.String(160), nullable=True, comment="代表性话轮预览 (首个客户输入)"),
        sa.Column("judge_model", sa.String(64), nullable=True),
        sa.Column("turns", sa.Integer, nullable=True),
        sa.Column("session_time", sa.TIMESTAMP(timezone=True), nullable=True, comment="会话最后一轮对话时间"),
        sa.Column("scanned_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("badcase_id", sa.String(64), nullable=True, comment="fail 采集的 badcase 关联"),
    )
    op.create_index("ix_quality_record_session", "quality_record", ["session_id"])
    op.create_index("ix_quality_record_verdict", "quality_record", ["verdict"])
    op.create_index("ix_quality_record_session_time", "quality_record", ["session_time"])
    op.create_index("ix_quality_record_scanned", "quality_record", ["scanned_at"])


def downgrade() -> None:
    op.drop_table("quality_record")
    op.drop_index("ix_badcase_session_time", table_name="badcase")
    op.drop_column("badcase", "session_time")
