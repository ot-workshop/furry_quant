"""Create the forward-registration tables (mirrors persistence/schema.py)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_forward_registration"
down_revision = "0001_postgres"
branch_labels = None
depends_on = None


def _ts(name: str, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.create_table(
        "forward_registration",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("idempotency_key", sa.String(80), nullable=False, unique=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("exit_date", sa.Date(), nullable=False),
        sa.Column("calendar_version", sa.String(40), nullable=False),
        sa.Column("calendar_fingerprint", sa.String(64), nullable=False),
        sa.Column("pool_codes", postgresql.JSONB(), nullable=False),
        sa.Column("pool_size", sa.Integer(), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("factor", sa.String(32), nullable=False),
        sa.Column("factor_window", sa.Integer(), nullable=False),
        sa.Column("factor_direction", sa.String(8), nullable=False),
        sa.Column("data_rules", postgresql.JSONB(), nullable=False),
        sa.Column("strategy_rules", postgresql.JSONB(), nullable=False),
        sa.Column("dataset_id", sa.String(100)),
        sa.Column("code_sha256", sa.String(64), nullable=False),
        _ts("written_at"),
        _ts("staging_at"),
        _ts("confirmed_at"),
        sa.Column("confirmed_evidence_sha256", sa.String(64)),
        sa.Column("score_snapshot", postgresql.JSONB()),
        sa.Column("label_snapshot", postgresql.JSONB()),
        sa.Column("result", postgresql.JSONB()),
        _ts("result_recorded_at"),
        sa.Column("invalid_reason", sa.Text()),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('registered','awaiting_score','scoring','scored','awaiting_exit',"
            "'awaiting_quote','verifying','verified','missed','expired','failed')",
            name="ck_forward_registration_status",
        ),
        sa.CheckConstraint(
            "pool_size > 0 AND top_k > 0 AND top_k < pool_size", name="ck_forward_registration_k"
        ),
        sa.CheckConstraint(
            "factor_direction IN ('high','low')", name="ck_forward_registration_direction"
        ),
        sa.CheckConstraint(
            "signal_date < entry_date AND entry_date <= exit_date", name="ck_forward_registration_dates"
        ),
    )
    op.create_index(
        "ix_forward_registration_status",
        "forward_registration",
        ["status", "created_at"],
    )
    op.create_index("ix_forward_registration_signal", "forward_registration", ["signal_date"])

    op.create_table(
        "forward_attempt",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "registration_id",
            sa.String(40),
            sa.ForeignKey("forward_registration.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False, unique=True),
        sa.Column("task_id", sa.String(40), sa.ForeignKey("research_task.id")),
        _ts("deadline_at", nullable=False),
        _ts("lock_seen_at"),
        _ts("staging_at"),
        _ts("sealed_at"),
        sa.Column("evidence_sha256", sa.String(64)),
        sa.Column("evidence_row_id", sa.String(64)),
        sa.Column("evidence", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('score','verify')", name="ck_forward_attempt_kind"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','missed','cancelled','interrupted')",
            name="ck_forward_attempt_status",
        ),
        sa.CheckConstraint("attempt_no > 0", name="ck_forward_attempt_no"),
    )
    op.create_index(
        "uq_forward_attempt_active",
        "forward_attempt",
        ["registration_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "uq_forward_attempt_succeeded",
        "forward_attempt",
        ["registration_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        "uq_forward_attempt_no",
        "forward_attempt",
        ["registration_id", "kind", "attempt_no"],
        unique=True,
    )
    op.create_index("uq_forward_attempt_queue", "forward_attempt", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("forward_attempt")
    op.drop_table("forward_registration")
