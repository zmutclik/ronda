"""enrollment key max_uses

Key sekali pakai (tombol Salin di Pendaftaran Agent): max_uses = 1, berlaku 2 jam; setelah dipakai/kedaluwarsa
dihapus oleh maintenance worker.
NULL = tanpa batas (perilaku key lama).

Revision ID: d5f2a9c3e7b1
Revises: c4e8a1f6b2d9
Create Date: 2026-11-18 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'd5f2a9c3e7b1'
down_revision: Union[str, Sequence[str], None] = 'c4e8a1f6b2d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('enrollment_keys', sa.Column('max_uses', mysql.INTEGER(unsigned=True), nullable=True))


def downgrade() -> None:
    op.drop_column('enrollment_keys', 'max_uses')
