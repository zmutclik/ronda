"""host manual specs

Kolom hosts.manual_specs: daftar field spesifikasi (manufacturer, model, serial_number) yang diubah manual
di tab Aksi, agar data inventaris dari agent tidak menimpanya.

Revision ID: 5c2e8a1f7b3d
Revises: 94180f35d60a
Create Date: 2026-10-16 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5c2e8a1f7b3d'
down_revision: Union[str, Sequence[str], None] = '94180f35d60a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('hosts', sa.Column('manual_specs', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('hosts', 'manual_specs')
