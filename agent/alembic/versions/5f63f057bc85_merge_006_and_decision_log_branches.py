"""merge 006 and decision_log branches

Revision ID: 5f63f057bc85
Revises: 006, c7d8e9f0a1b2
Create Date: 2026-08-07 12:40:26.550900
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "5f63f057bc85"
down_revision: str | None = ("006", "c7d8e9f0a1b2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
