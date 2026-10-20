"""remote session state

Kolom remote_sessions.connected_at (browser & agent tersambung di relay) dan remote_sessions.error (galat dari agent,
mis. remote dinonaktifkan di PC atau VNC gagal dijalankan).

Revision ID: 7d4b1c9e2a6f
Revises: 5c2e8a1f7b3d
Create Date: 2026-10-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7d4b1c9e2a6f'
down_revision: Union[str, Sequence[str], None] = '5c2e8a1f7b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('remote_sessions', sa.Column('connected_at', sa.DateTime(), nullable=True))
    op.add_column('remote_sessions', sa.Column('error', sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column('remote_sessions', 'error')
    op.drop_column('remote_sessions', 'connected_at')
