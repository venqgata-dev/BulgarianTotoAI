"""two drawings per session

Adds ``draws.drawing`` (1 or 2) so a historical draw session that published
two independent drawings ("I-во теглене" / "II-ро теглене") can be stored as
two rows instead of colliding on the same (game, year, draw number). The
draw-identity unique constraint is widened to include it. Existing rows
default to ``drawing=1`` (unaffected).

Also widens ``draws.source`` (16 -> 32 chars) to fit the longer provenance
labels already used by ``app.database.models.Draw.source``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17 14:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('draws', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('drawing', sa.Integer(), nullable=False, server_default='1')
        )
        batch_op.alter_column(
            'source', existing_type=sa.String(length=16), type_=sa.String(length=32)
        )
        batch_op.drop_constraint('uq_draw_game_year_number', type_='unique')
        batch_op.create_unique_constraint(
            'uq_draw_game_year_number', ['game_id', 'draw_year', 'draw_number', 'drawing']
        )


def downgrade() -> None:
    with op.batch_alter_table('draws', schema=None) as batch_op:
        batch_op.drop_constraint('uq_draw_game_year_number', type_='unique')
        batch_op.create_unique_constraint(
            'uq_draw_game_year_number', ['game_id', 'draw_year', 'draw_number']
        )
        batch_op.alter_column(
            'source', existing_type=sa.String(length=32), type_=sa.String(length=16)
        )
        batch_op.drop_column('drawing')
