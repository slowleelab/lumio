"""create kb_approval_status enum + rebase column

Revision ID: e41f0a2b3c4d
Revises: a1b2c3d4e5f6
Create Date: 2026-08-21

背景: 迁移 003 把 KbDocument.approval_status 建成 varchar(32)，但 ORM 模型
(KbDocument.approval_status) 映射到 SAEnum(KbApprovalStatus, name="kb_approval_status")。
导致 PG 中从未创建该枚举类型，插入时 SA 生成 ::kb_approval_status 强转报
UndefinedObjectError: type "kb_approval_status" does not exist。

修复: 补齐枚枚举类型，并把列从 varchar(32) 改回 PG 枚举，消除模型/库漂移。
存量 22 行全部为合法枚举值 DRAFT，USING 强转安全。
"""

from __future__ import annotations

from alembic import op

revision = "e41f0a2b3c4d"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

_ENUM_DEF = "DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'SUPERSEDED', 'REJECTED', 'ARCHIVED"


def upgrade() -> None:
    op.execute(f"CREATE TYPE kb_approval_status AS ENUM ('{_ENUM_DEF}')")
    # 先丢弃 varchar 常量默认值，否则 PG 无法把字符串默认自动强转为枚举
    op.execute("ALTER TABLE kb_document ALTER COLUMN approval_status DROP DEFAULT")
    op.execute(
        "ALTER TABLE kb_document ALTER COLUMN approval_status TYPE kb_approval_status "
        "USING approval_status::kb_approval_status"
    )
    op.execute("ALTER TABLE kb_document ALTER COLUMN approval_status SET DEFAULT 'DRAFT'")


def downgrade() -> None:
    op.execute("ALTER TABLE kb_document ALTER COLUMN approval_status DROP DEFAULT")
    op.execute(
        "ALTER TABLE kb_document ALTER COLUMN approval_status TYPE character varying(32) "
        "USING approval_status::text"
    )
    op.execute("ALTER TABLE kb_document ALTER COLUMN approval_status SET DEFAULT 'DRAFT'")
    op.execute("DROP TYPE kb_approval_status")
