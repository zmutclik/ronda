"""release uploaded_by nullable

Rilis agent yang didaftarkan worker dari agent/releases di repo (git pull) tidak punya pengunggah: uploaded_by NULL.

Revision ID: c4e8a1f6b2d9
Revises: b7d3e9f1a2c4
Create Date: 2026-11-18 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'c4e8a1f6b2d9'
down_revision: Union[str, Sequence[str], None] = 'b7d3e9f1a2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('agent_releases', 'uploaded_by', existing_type=mysql.INTEGER(unsigned=True), nullable=True)


def downgrade() -> None:
    # Rilis dari repo (uploaded_by NULL) tidak bisa dipertahankan di skema lama
    op.execute("DELETE FROM agent_releases WHERE uploaded_by IS NULL")
    op.alter_column('agent_releases', 'uploaded_by', existing_type=mysql.INTEGER(unsigned=True), nullable=False)
