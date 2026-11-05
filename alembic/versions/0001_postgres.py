"""Create the initial PostgreSQL schema from a frozen migration definition."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_postgres"
down_revision = None
branch_labels = None
depends_on = None


def _timestamp(name: str, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def _json(name: str, **kwargs):
    return sa.Column(name, postgresql.JSONB(), **kwargs)


def _provenance():
    return [
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("provider_id", sa.String(100)),
        sa.Column("quality", sa.String(30), nullable=False, server_default="single_source"),
        _timestamp("fetched_at", nullable=False),
        _timestamp("published_at"),
        sa.Column("release_id", sa.BigInteger(), sa.ForeignKey("data_release.id"), nullable=False),
    ]


def _daily_table(name: str, *columns) -> None:
    op.create_table(
        name,
        sa.Column("ts_code", sa.String(16), primary_key=True),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        *columns,
        *_provenance(),
    )
    op.create_index(f"ix_{name}_date", name, ["trade_date"])


def upgrade():
    op.create_table(
        "data_release",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("batch_id", sa.String(100), nullable=False, unique=True),
        _timestamp("created_at", nullable=False, server_default=sa.func.now()),
        sa.Column("changed_rows", sa.Integer(), nullable=False),
        _json("details", nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_table(
        "security",
        sa.Column("ts_code", sa.String(16), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("exchange", sa.String(16)),
        sa.Column("board", sa.String(24)),
        sa.Column("list_date", sa.Date()),
        sa.Column("delist_date", sa.Date()),
        *_provenance(),
    )
    op.create_table(
        "security_alias",
        sa.Column("ts_code", sa.String(16), primary_key=True),
        sa.Column("alias", sa.Text(), primary_key=True),
        sa.Column("valid_from", sa.Date(), primary_key=True),
        sa.Column("valid_to", sa.Date()),
        *_provenance(),
    )
    op.create_table(
        "trade_calendar",
        sa.Column("exchange", sa.String(16), primary_key=True),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("previous_open", sa.Date()),
        *_provenance(),
    )
    _daily_table(
        "daily_bar",
        *[
            sa.Column(field, sa.Float())
            for field in ("open", "high", "low", "close", "pre_close", "volume", "amount", "change")
        ],
        sa.Column("name", sa.Text()),
        sa.Column("industry", sa.Text()),
        sa.Column("price_source", sa.String(40)),
        sa.Column("validation_level", sa.String(40)),
        sa.Column("reviewed_unbounded_session", sa.Boolean()),
    )
    _daily_table(
        "adjustment",
        sa.Column("adj_factor", sa.Float(), nullable=False),
        sa.Column("adjustment_source", sa.String(40)),
        sa.CheckConstraint("adj_factor > 0", name="ck_adjustment_positive"),
    )
    _daily_table(
        "valuation",
        *[
            sa.Column(field, sa.Float())
            for field in ("pe", "pe_ttm", "pb", "ps", "ps_ttm", "total_mv", "circ_mv", "turnover_rate", "dv_ttm")
        ],
        sa.Column("valuation_source", sa.String(40)),
        sa.Column("valuation_carried", sa.Boolean()),
        *[sa.Column(f"{field}_asof", sa.Date()) for field in ("valuation", "pe_ttm", "pb", "total_mv")],
        *[_timestamp(f"{field}_available_at") for field in ("valuation", "pe_ttm", "pb", "total_mv")],
    )
    _daily_table(
        "trading_status",
        sa.Column("suspended", sa.Boolean()),
        sa.Column("is_st", sa.Boolean()),
        sa.Column("up_limit", sa.Float()),
        sa.Column("down_limit", sa.Float()),
        sa.Column("status_reason", sa.Text()),
        sa.Column("limit_source", sa.String(40)),
        sa.Column("limit_available", sa.Boolean()),
    )
    op.create_table(
        "index_member",
        sa.Column("index_code", sa.String(16), primary_key=True),
        sa.Column("ts_code", sa.String(16), primary_key=True),
        sa.Column("effective_from", sa.Date(), primary_key=True),
        sa.Column("effective_to", sa.Date()),
        sa.Column("snapshot_date", sa.Date()),
        sa.Column("weight", sa.Float()),
        sa.Column("timing_basis", sa.String(60), nullable=False),
        *_provenance(),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_member_interval",
        ),
    )
    op.create_table(
        "industry_member",
        sa.Column("classification", sa.String(30), primary_key=True),
        sa.Column("ts_code", sa.String(16), primary_key=True),
        sa.Column("effective_from", sa.Date(), primary_key=True),
        sa.Column("effective_to", sa.Date()),
        sa.Column("industry_code", sa.String(30), nullable=False),
        sa.Column("industry_name", sa.Text()),
        sa.Column("timing_basis", sa.String(60), nullable=False),
        *_provenance(),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_industry_interval",
        ),
    )
    _daily_table(
        "benchmark",
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("pre_close", sa.Float()),
        sa.Column("return_type", sa.String(30), nullable=False),
    )
    op.create_table(
        "data_revision",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("release_id", sa.BigInteger(), sa.ForeignKey("data_release.id"), nullable=False),
        sa.Column("table_name", sa.String(40), nullable=False),
        _json("row_key", nullable=False),
        _json("old_values"),
        _json("new_values", nullable=False),
        _json("changed_fields", nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        _timestamp("fetched_at", nullable=False),
    )
    op.create_index("ix_revision_release", "data_revision", ["release_id"])
    op.create_table(
        "source_check",
        sa.Column("source", sa.String(40), primary_key=True),
        sa.Column("component", sa.String(40), primary_key=True),
        sa.Column("partition_key", sa.String(160), primary_key=True),
        _timestamp("checked_at", nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        _json("details", nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_table(
        "research_task",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("idempotency_key", sa.String(80), nullable=False, unique=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False, server_default="research"),
        _json("request", nullable=False),
        _json("execution", nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("completed", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        _timestamp("created_at", nullable=False, server_default=sa.func.now()),
        _timestamp("started_at"),
        _timestamp("finished_at"),
        sa.Column("worker_id", sa.String(100)),
        _timestamp("heartbeat_at"),
        _json("result"),
        sa.Column("error", sa.Text()),
        _json("error_details"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','interrupted','cancelled','paused')",
            name="ck_task_status",
        ),
    )
    op.create_index("ix_task_queue", "research_task", ["status", "created_at"])
    op.create_table(
        "update_partition",
        sa.Column("task_id", sa.String(40), sa.ForeignKey("research_task.id"), primary_key=True),
        sa.Column("component", sa.String(40), primary_key=True),
        sa.Column("partition_key", sa.String(160), primary_key=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("release_id", sa.BigInteger(), sa.ForeignKey("data_release.id")),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text()),
        _timestamp("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "provider_budget",
        sa.Column("provider", sa.String(40), primary_key=True),
        sa.Column("budget_date", sa.Date(), primary_key=True),
        sa.Column("requests", sa.Integer(), nullable=False),
        _timestamp("last_request_at"),
    )
    op.create_table(
        "dataset",
        sa.Column("id", sa.String(100), primary_key=True),
        _json("request", nullable=False),
        _json("manifest", nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        _timestamp("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "research_run",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("category", sa.String(30), primary_key=True),
        sa.Column("dataset_id", sa.String(100), sa.ForeignKey("dataset.id")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        _json("summary", nullable=False),
        _json("configuration", nullable=False),
        _json("metadata", nullable=False),
        _json("payload", nullable=False),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        _timestamp("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "run_metrics",
        sa.Column("run_id", sa.String(100), primary_key=True),
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("value", sa.Float()),
        sa.Column("definition", sa.Text()),
    )
    op.create_table(
        "run_equity",
        sa.Column("run_id", sa.String(100), primary_key=True),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("benchmark", sa.Float()),
        sa.Column("cash", sa.Float()),
        sa.Column("total_value", sa.Float()),
        sa.Column("daily_return", sa.Float()),
        sa.Column("turnover", sa.Float()),
        sa.Column("cost", sa.Float()),
        _json("details", nullable=False),
    )
    op.create_table(
        "run_trade",
        sa.Column("run_id", sa.String(100), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("signal_date", sa.Date()),
        sa.Column("ts_code", sa.String(16), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("commission", sa.Float()),
        sa.Column("stamp_tax", sa.Float()),
        sa.Column("impact_cost", sa.Float()),
        sa.Column("total_cost", sa.Float()),
        _json("details", nullable=False),
    )
    op.create_index("ix_run_trade_date", "run_trade", ["run_id", "trade_date"])
    op.create_table(
        "run_artifact",
        sa.Column("run_id", sa.String(100), primary_key=True),
        sa.Column("category", sa.String(30), primary_key=True),
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("row_count", sa.BigInteger()),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("format", sa.String(20), nullable=False),
    )


def downgrade():
    for table in [
        "run_artifact",
        "run_trade",
        "run_equity",
        "run_metrics",
        "research_run",
        "dataset",
        "provider_budget",
        "update_partition",
        "research_task",
        "source_check",
        "data_revision",
        "benchmark",
        "industry_member",
        "index_member",
        "trading_status",
        "valuation",
        "adjustment",
        "daily_bar",
        "trade_calendar",
        "security_alias",
        "security",
        "data_release",
    ]:
        op.drop_table(table)
