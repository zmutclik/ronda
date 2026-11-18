"""agent installers

Installer agent (setup .exe Windows, tar.gz Linux) untuk pemasangan lewat skrip /install.ps1 dan /install.sh.

Revision ID: b7d3e9f1a2c4
Revises: 3f9a6c2d8e1b
Create Date: 2026-11-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'b7d3e9f1a2c4'
down_revision: Union[str, Sequence[str], None] = '3f9a6c2d8e1b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_installers',
        sa.Column('id', mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column('platform', sa.String(length=10), nullable=False),
        sa.Column('version', sa.String(length=20), nullable=False),
        sa.Column('sha256', sa.CHAR(length=64), nullable=False),
        sa.Column('file_path', sa.String(length=255), nullable=False),
        sa.Column('size_bytes', mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column('uploaded_by', mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("platform IN ('windows','linux')", name='ck_installer_platform'),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name='fk_installer_user'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'version', name='uq_installer_platform_version'),
    )


def downgrade() -> None:
    op.drop_table('agent_installers')
