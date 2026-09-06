"""add telegram_message_id for idempotency + create notes table

Revision ID: c3d4e5f6a7b8
Revises: 29ef2f3a7f5d
Create Date: 2026-09-03 15:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = '29ef2f3a7f5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOTES_COLUMNS = [
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('telegram_user_id', sa.BigInteger(), nullable=False),
    sa.Column('note_type', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('person', sa.String(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.Column('raw_message', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if 'notes' not in insp.get_table_names():
        op.create_table('notes', *NOTES_COLUMNS)
        op.create_index('ix_notes_id', 'notes', ['id'], unique=False)
        op.create_index('ix_notes_telegram_user_id', 'notes', ['telegram_user_id'], unique=False)
        op.create_index('ix_notes_telegram_message_id', 'notes', ['telegram_message_id'], unique=True)

    trans_cols = [c['name'] for c in insp.get_columns('transactions')]
    if 'telegram_message_id' not in trans_cols:
        with op.batch_alter_table('transactions') as batch_op:
            batch_op.add_column(sa.Column('telegram_message_id', sa.BigInteger(), nullable=True))
        op.create_index('ix_transactions_telegram_message_id', 'transactions', ['telegram_message_id'], unique=True)
    if 'deleted_at' not in trans_cols:
        with op.batch_alter_table('transactions') as batch_op:
            batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))

    task_cols = [c['name'] for c in insp.get_columns('tasks')]
    if 'telegram_message_id' not in task_cols:
        with op.batch_alter_table('tasks') as batch_op:
            batch_op.add_column(sa.Column('telegram_message_id', sa.BigInteger(), nullable=True))
        op.create_index('ix_tasks_telegram_message_id', 'tasks', ['telegram_message_id'], unique=True)
    if 'deleted_at' not in task_cols:
        with op.batch_alter_table('tasks') as batch_op:
            batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_index('ix_notes_telegram_message_id', table_name='notes')
    op.drop_index('ix_notes_telegram_user_id', table_name='notes')
    op.drop_index('ix_notes_id', table_name='notes')
    op.drop_table('notes')

    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_index('ix_tasks_telegram_message_id')
        batch_op.drop_column('telegram_message_id')

    with op.batch_alter_table('transactions') as batch_op:
        batch_op.drop_index('ix_transactions_telegram_message_id')
        batch_op.drop_column('telegram_message_id')
