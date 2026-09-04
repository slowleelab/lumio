"""Badcase 资产表 — 事后优化闭环核心 (目标架构 ⑧)

五路信号源采集 → 粗筛去重 → LLM 自动归因 → 修复策略路由 (四张分流表)
→ 回归评测集。结构化记录方案 §7.4 要求的全部字段。

Revision ID: b5c6d7e8f9a0
Revises: a3b4c5d6e7f8
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "badcase",
        sa.Column("id", sa.Uuid(native_uuid=False), primary_key=True),
        # ── 溯源 ──
        sa.Column("trace_id", sa.String(64), nullable=False),  # 贯穿八层的请求标识
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=True),
        sa.Column("channel", sa.String(16), nullable=True),
        # ── 信号源 (五路, 方案 §3.1) ──
        sa.Column(
            "signal_source",
            sa.String(32),
            nullable=False,
            comment="negative_feedback/transfer/agent_revoke/behavior_anomaly/compliance_alert",
        ),
        sa.Column("signal_detail", sa.JSON, nullable=True),
        # ── 现场快照 (八层中间产物引用, 方案 §3.2) ──
        sa.Column("user_input", sa.Text, nullable=False),
        sa.Column("bot_output", sa.Text, nullable=True),
        sa.Column("snapshot", sa.JSON, nullable=True, comment="八层中间产物 JSON (intent/route/rag/generate/compliance)"),
        # ── 归因 (模块 A) ──
        sa.Column(
            "root_cause_layer",
            sa.String(16),
            nullable=True,
            comment="layer_1..layer_7/uncertain; 归因只归到第一处偏离层",
        ),
        sa.Column(
            "root_cause_category",
            sa.String(16),
            nullable=True,
            comment="semantic/knowledge/process/coverage/uncertain (语义/知识/流程/语料覆盖)",
        ),
        sa.Column("attribution_evidence", sa.Text, nullable=True),
        sa.Column("attribution_confidence", sa.Float, nullable=True),
        sa.Column("attribution_model", sa.String(64), nullable=True),
        sa.Column("needs_human_review", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("human_confirmed_layer", sa.String(16), nullable=True, comment="人工裁决覆盖"),
        # ── 修复路由 (四张分流表, 方案 §5.1) ──
        sa.Column(
            "fix_table",
            sa.String(16),
            nullable=True,
            comment="A_knowledge/B_intent/C_rule/D_model/none",
        ),
        sa.Column(
            "fix_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="pending/fixing/canary/deployed/rejected",
        ),
        sa.Column("fix_note", sa.Text, nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # ── 粗筛去重 (方案 §4.1 相似合并) ──
        sa.Column("input_embedding", sa.JSON, nullable=True, comment="去重用向量 (可选落库)"),
        sa.Column("dedup_group_id", sa.String(64), nullable=True, index=True),
        # ── 时间 ──
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_badcase_session", "badcase", ["session_id"])
    op.create_index("ix_badcase_signal", "badcase", ["signal_source"])
    op.create_index("ix_badcase_fix_status", "badcase", ["fix_status"])
    op.create_index("ix_badcase_root_layer", "badcase", ["root_cause_layer"])
    op.create_index("ix_badcase_created", "badcase", ["created_at"])
    op.create_index("ix_badcase_trace", "badcase", ["trace_id"])


def downgrade() -> None:
    op.drop_table("badcase")
