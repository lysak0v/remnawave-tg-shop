"""panel user numeric id (Remnawave 3.x)

Revision ID: 0004_panel_user_id
Revises: 0003_promo_curr_act_not_null
Create Date: 2026-10-25 00:00:00.000000

Remnawave 3.0 removed users.uuid and addresses users by numeric id.
Adds users.panel_user_id / subscriptions.panel_user_id and relaxes
subscriptions.panel_user_uuid to nullable. The legacy uuid columns are kept
(read-only history); ids are filled by a separate backfill step.
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_panel_user_id"
down_revision: Union[str, Sequence[str], None] = "0003_promo_curr_act_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set:
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(inspector, table: str) -> set:
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column("users", sa.Column("panel_user_id", sa.BigInteger(), nullable=True))
        op.create_index("ix_users_panel_user_id", "users", ["panel_user_id"], unique=True)
        op.add_column("subscriptions", sa.Column("panel_user_id", sa.BigInteger(), nullable=True))
        op.create_index("ix_subscriptions_panel_user_id", "subscriptions", ["panel_user_id"], unique=False)
        op.alter_column("subscriptions", "panel_user_uuid", existing_type=sa.String(), nullable=True)
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("users"):
        if "panel_user_id" not in _columns(inspector, "users"):
            op.add_column("users", sa.Column("panel_user_id", sa.BigInteger(), nullable=True))
        if "ix_users_panel_user_id" not in _indexes(inspector, "users"):
            op.create_index("ix_users_panel_user_id", "users", ["panel_user_id"], unique=True)

    if inspector.has_table("subscriptions"):
        sub_columns = _columns(inspector, "subscriptions")
        if "panel_user_id" not in sub_columns:
            op.add_column("subscriptions", sa.Column("panel_user_id", sa.BigInteger(), nullable=True))
        if "ix_subscriptions_panel_user_id" not in _indexes(inspector, "subscriptions"):
            op.create_index("ix_subscriptions_panel_user_id", "subscriptions", ["panel_user_id"], unique=False)
        if "panel_user_uuid" in sub_columns:
            op.alter_column("subscriptions", "panel_user_uuid", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # Rows created on panel 3.x have no uuid, so NOT NULL is not restored on
    # subscriptions.panel_user_uuid. Downgrading only makes sense together with
    # restoring the pre-migration dump of the bot database.
    bind = op.get_bind() if not context.is_offline_mode() else None
    inspector = sa.inspect(bind) if bind is not None else None

    def has_index(table: str, name: str) -> bool:
        return inspector is None or name in _indexes(inspector, table)

    def has_column(table: str, name: str) -> bool:
        return inspector is None or name in _columns(inspector, table)

    if has_index("subscriptions", "ix_subscriptions_panel_user_id"):
        op.drop_index("ix_subscriptions_panel_user_id", table_name="subscriptions")
    if has_column("subscriptions", "panel_user_id"):
        op.drop_column("subscriptions", "panel_user_id")
    if has_index("users", "ix_users_panel_user_id"):
        op.drop_index("ix_users_panel_user_id", table_name="users")
    if has_column("users", "panel_user_id"):
        op.drop_column("users", "panel_user_id")
