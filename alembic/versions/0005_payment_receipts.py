"""«Мой налог» receipt state on payments, for resending after LKNPD outages

Revision ID: 0005_payment_receipts
Revises: 0004_panel_user_id
Create Date: 2026-11-17 00:00:00.000000

Receipts used to be sent once right after payment and the result was only logged.
Existing payments get receipt_status NULL (nothing to resend).
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_payment_receipts"
down_revision: Union[str, Sequence[str], None] = "0004_panel_user_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("receipt_status", lambda: sa.Column("receipt_status", sa.String(), nullable=True)),
    ("receipt_uuid", lambda: sa.Column("receipt_uuid", sa.String(), nullable=True)),
    ("receipt_item_name", lambda: sa.Column("receipt_item_name", sa.String(), nullable=True)),
    ("receipt_paid_at", lambda: sa.Column("receipt_paid_at", sa.DateTime(timezone=True), nullable=True)),
    ("receipt_attempts", lambda: sa.Column("receipt_attempts", sa.Integer(), nullable=False, server_default="0")),
    ("receipt_last_error", lambda: sa.Column("receipt_last_error", sa.String(), nullable=True)),
    ("receipt_next_attempt_at",
     lambda: sa.Column("receipt_next_attempt_at", sa.DateTime(timezone=True), nullable=True)),
    ("receipt_alerted_at", lambda: sa.Column("receipt_alerted_at", sa.DateTime(timezone=True), nullable=True)),
)
_INDEX = "ix_payments_receipt_status"


def _columns(inspector, table: str) -> set:
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(inspector, table: str) -> set:
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    if context.is_offline_mode():
        for _name, column in _COLUMNS:
            op.add_column("payments", column())
        op.create_index(_INDEX, "payments", ["receipt_status"], unique=False)
        return

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("payments"):
        return
    existing = _columns(inspector, "payments")
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("payments", column())
    if _INDEX not in _indexes(inspector, "payments"):
        op.create_index(_INDEX, "payments", ["receipt_status"], unique=False)


def downgrade() -> None:
    bind = op.get_bind() if not context.is_offline_mode() else None
    inspector = sa.inspect(bind) if bind is not None else None
    if inspector is None or _INDEX in _indexes(inspector, "payments"):
        op.drop_index(_INDEX, table_name="payments")
    existing = None if inspector is None else _columns(inspector, "payments")
    for name, _column in reversed(_COLUMNS):
        if existing is None or name in existing:
            op.drop_column("payments", name)
