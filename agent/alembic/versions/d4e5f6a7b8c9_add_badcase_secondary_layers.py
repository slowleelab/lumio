"""badcase 复合根因 — alembic d4e5f6a7b8c9

secondary_layers: 次要因素层列表 (归因 3 票中 ≥2 票独立提及, 不含主根因层);
仅展示用途, 问题组聚合仍按主根因层。存量行保持 NULL。

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-09-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("badcase", sa.Column("secondary_layers", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("badcase", "secondary_layers")
