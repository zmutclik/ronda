"""folder access

Folder terbatas: folders.is_restricted dan tabel folder_access (user yang boleh melihat folder terbatas beserta PC-nya).

Revision ID: 3f9a6c2d8e1b
Revises: 7d4b1c9e2a6f
Create Date: 2026-11-06 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = '3f9a6c2d8e1b'
down_revision: Union[str, Sequence[str], None] = '7d4b1c9e2a6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('folders', sa.Column('is_restricted', sa.Boolean(), server_default=sa.text('0'), nullable=False))
    op.create_table(
        'folder_access',
        sa.Column('folder_id', mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column('user_id', mysql.INTEGER(unsigned=True), nullable=False),
        sa.ForeignKeyConstraint(['folder_id'], ['folders.id'], name='fk_folder_access_folder', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_folder_access_user', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('folder_id', 'user_id'),
    )
    op.create_index(op.f('ix_folder_access_user_id'), 'folder_access', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_table('folder_access')
    op.drop_column('folders', 'is_restricted')
