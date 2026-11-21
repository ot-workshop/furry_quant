"""Explicit SQLAlchemy Core schema. Dates describe business time; fetched_at does not certify PIT."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()


def timestamp(name, **kwargs):
    return Column(name, DateTime(timezone=True), **kwargs)


data_release = Table(
    "data_release",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("source", String(40), nullable=False),
    Column("batch_id", String(100), nullable=False, unique=True),
    timestamp("created_at", nullable=False, server_default=func.now()),
    Column("changed_rows", Integer, nullable=False),
    Column("details", JSONB, nullable=False, server_default="{}"),
)


def provenance():
    return [
        Column("source", String(40), nullable=False),
        Column("provider_id", String(100)),
        Column("quality", String(30), nullable=False, server_default="single_source"),
        timestamp("fetched_at", nullable=False),
        timestamp("published_at"),
        Column("release_id", BigInteger, ForeignKey("data_release.id"), nullable=False),
    ]


security = Table(
    "security",
    metadata,
    Column("ts_code", String(16), primary_key=True),
    Column("name", Text, nullable=False),
    Column("exchange", String(16)),
    Column("board", String(24)),
    Column("list_date", Date),
    Column("delist_date", Date),
    *provenance(),
)
security_alias = Table(
    "security_alias",
    metadata,
    Column("ts_code", String(16), primary_key=True),
    Column("alias", Text, primary_key=True),
    Column("valid_from", Date, primary_key=True),
    Column("valid_to", Date),
    *provenance(),
)
trade_calendar = Table(
    "trade_calendar",
    metadata,
    Column("exchange", String(16), primary_key=True),
    Column("trade_date", Date, primary_key=True),
    Column("is_open", Boolean, nullable=False),
    Column("previous_open", Date),
    *provenance(),
)


def daily_table(name, columns):
    table = Table(
        name,
        metadata,
        Column("ts_code", String(16), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        *columns,
        *provenance(),
    )
    Index(f"ix_{name}_date", table.c.trade_date)
    return table


daily_bar = daily_table(
    "daily_bar",
    [Column(field, Float) for field in ("open", "high", "low", "close", "pre_close", "volume", "amount", "change")]
    + [
        Column("name", Text),
        Column("industry", Text),
        Column("price_source", String(40)),
        Column("validation_level", String(40)),
        Column("reviewed_unbounded_session", Boolean),
    ],
)
adjustment = daily_table(
    "adjustment",
    [
        Column("adj_factor", Float, nullable=False),
        Column("adjustment_source", String(40)),
        CheckConstraint("adj_factor > 0", name="ck_adjustment_positive"),
    ],
)
valuation = daily_table(
    "valuation",
    [
        Column(field, Float)
        for field in ("pe", "pe_ttm", "pb", "ps", "ps_ttm", "total_mv", "circ_mv", "turnover_rate", "dv_ttm")
    ]
    + [Column("valuation_source", String(40)), Column("valuation_carried", Boolean)]
    + [Column(f"{field}_asof", Date) for field in ("valuation", "pe_ttm", "pb", "total_mv")]
    + [timestamp(f"{field}_available_at") for field in ("valuation", "pe_ttm", "pb", "total_mv")],
)
trading_status = daily_table(
    "trading_status",
    [
        Column("suspended", Boolean),
        Column("is_st", Boolean),
        Column("up_limit", Float),
        Column("down_limit", Float),
        Column("status_reason", Text),
        Column("limit_source", String(40)),
        Column("limit_available", Boolean),
    ],
)
index_member = Table(
    "index_member",
    metadata,
    Column("index_code", String(16), primary_key=True),
    Column("ts_code", String(16), primary_key=True),
    Column("effective_from", Date, primary_key=True),
    Column("effective_to", Date),
    Column("snapshot_date", Date),
    Column("weight", Float),
    Column("timing_basis", String(60), nullable=False),
    *provenance(),
    CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_member_interval"),
)
industry_member = Table(
    "industry_member",
    metadata,
    Column("classification", String(30), primary_key=True),
    Column("ts_code", String(16), primary_key=True),
    Column("effective_from", Date, primary_key=True),
    Column("effective_to", Date),
    Column("industry_code", String(30), nullable=False),
    Column("industry_name", Text),
    Column("timing_basis", String(60), nullable=False),
    *provenance(),
    CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_industry_interval"),
)
benchmark = daily_table(
    "benchmark",
    [
        Column("close", Float, nullable=False),
        Column("pre_close", Float),
        Column("return_type", String(30), nullable=False),
    ],
)

data_revision = Table(
    "data_revision",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("release_id", BigInteger, ForeignKey("data_release.id"), nullable=False),
    Column("table_name", String(40), nullable=False),
    Column("row_key", JSONB, nullable=False),
    Column("old_values", JSONB),
    Column("new_values", JSONB, nullable=False),
    Column("changed_fields", JSONB, nullable=False),
    Column("source", String(40), nullable=False),
    timestamp("fetched_at", nullable=False),
)
Index("ix_revision_release", data_revision.c.release_id)

source_check = Table(
    "source_check",
    metadata,
    Column("source", String(40), primary_key=True),
    Column("component", String(40), primary_key=True),
    Column("partition_key", String(160), primary_key=True),
    timestamp("checked_at", nullable=False),
    Column("status", String(30), nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("details", JSONB, nullable=False, server_default="{}"),
)

research_task = Table(
    "research_task",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("idempotency_key", String(80), nullable=False, unique=True),
    Column("fingerprint", String(64), nullable=False),
    Column("kind", String(30), nullable=False, server_default="research"),
    Column("request", JSONB, nullable=False),
    Column("execution", JSONB, nullable=False),
    Column("name", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("stage", String(30), nullable=False),
    Column("message", Text, nullable=False),
    Column("completed", Integer, nullable=False),
    Column("total", Integer, nullable=False),
    timestamp("created_at", nullable=False, server_default=func.now()),
    timestamp("started_at"),
    timestamp("finished_at"),
    Column("worker_id", String(100)),
    timestamp("heartbeat_at"),
    Column("result", JSONB),
    Column("error", Text),
    Column("error_details", JSONB),
    CheckConstraint(
        "status IN ('queued','running','succeeded','failed','interrupted','cancelled','paused')", name="ck_task_status"
    ),
)
Index("ix_task_queue", research_task.c.status, research_task.c.created_at)

update_partition = Table(
    "update_partition",
    metadata,
    Column("task_id", String(40), ForeignKey("research_task.id"), primary_key=True),
    Column("component", String(40), primary_key=True),
    Column("partition_key", String(160), primary_key=True),
    Column("status", String(30), nullable=False),
    Column("release_id", BigInteger, ForeignKey("data_release.id")),
    Column("row_count", Integer, nullable=False, server_default="0"),
    Column("reason", Text),
    timestamp("updated_at", nullable=False, server_default=func.now()),
)
provider_budget = Table(
    "provider_budget",
    metadata,
    Column("provider", String(40), primary_key=True),
    Column("budget_date", Date, primary_key=True),
    Column("requests", Integer, nullable=False),
    timestamp("last_request_at"),
)

dataset = Table(
    "dataset",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("request", JSONB, nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("relative_path", Text, nullable=False),
    Column("status", String(20), nullable=False),
    timestamp("created_at", nullable=False, server_default=func.now()),
)
research_run = Table(
    "research_run",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("category", String(30), primary_key=True),
    Column("dataset_id", String(100), ForeignKey("dataset.id")),
    Column("name", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("summary", JSONB, nullable=False),
    Column("configuration", JSONB, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("document_sha256", String(64), nullable=False),
    timestamp("created_at", nullable=False, server_default=func.now()),
)
run_metrics = Table(
    "run_metrics",
    metadata,
    Column("run_id", String(100), primary_key=True),
    Column("name", String(80), primary_key=True),
    Column("value", Float),
    Column("definition", Text),
)
run_equity = Table(
    "run_equity",
    metadata,
    Column("run_id", String(100), primary_key=True),
    Column("trade_date", Date, primary_key=True),
    Column("equity", Float, nullable=False),
    Column("benchmark", Float),
    Column("cash", Float),
    Column("total_value", Float),
    Column("daily_return", Float),
    Column("turnover", Float),
    Column("cost", Float),
    Column("details", JSONB, nullable=False),
)
run_trade = Table(
    "run_trade",
    metadata,
    Column("run_id", String(100), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("trade_date", Date, nullable=False),
    Column("signal_date", Date),
    Column("ts_code", String(16), nullable=False),
    Column("side", String(10), nullable=False),
    Column("quantity", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("commission", Float),
    Column("stamp_tax", Float),
    Column("impact_cost", Float),
    Column("total_cost", Float),
    Column("details", JSONB, nullable=False),
)
Index("ix_run_trade_date", run_trade.c.run_id, run_trade.c.trade_date)
run_artifact = Table(
    "run_artifact",
    metadata,
    Column("run_id", String(100), primary_key=True),
    Column("category", String(30), primary_key=True),
    Column("name", String(80), primary_key=True),
    Column("relative_path", Text, nullable=False),
    Column("row_count", BigInteger),
    Column("sha256", String(64), nullable=False),
    Column("format", String(20), nullable=False),
)


# ---------------------------------------------------------------- forward registration
# Two tables only. Evidence lives inside the registration row (registration proof and the
# score snapshot) and inside each attempt row (the attempt's own evidence), so there is no
# separate evidence table. See docs/forward-registration.md for the time model.

forward_registration = Table(
    "forward_registration",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("idempotency_key", String(80), nullable=False, unique=True),
    Column("fingerprint", String(64), nullable=False),
    Column("request", JSONB, nullable=False),
    Column("name", Text, nullable=False),
    Column("status", String(24), nullable=False),
    Column("signal_date", Date, nullable=False),
    Column("entry_date", Date, nullable=False),
    Column("exit_date", Date, nullable=False),
    Column("calendar_version", String(40), nullable=False),
    Column("calendar_fingerprint", String(64), nullable=False),
    Column("pool_codes", JSONB, nullable=False),
    Column("pool_size", Integer, nullable=False),
    Column("top_k", Integer, nullable=False),
    Column("factor", String(32), nullable=False),
    Column("factor_window", Integer, nullable=False),
    Column("factor_direction", String(8), nullable=False),
    Column("data_rules", JSONB, nullable=False),
    Column("strategy_rules", JSONB, nullable=False),
    Column("dataset_id", String(100)),
    Column("code_sha256", String(64), nullable=False),
    # registration proof (two-phase, see docs): lock-seen sample, staging sample, sealed sample
    timestamp("written_at"),
    timestamp("staging_at"),
    timestamp("confirmed_at"),
    Column("confirmed_evidence_sha256", String(64)),
    # snapshots bound later: score data captured after the signal close, labels after maturity
    Column("score_snapshot", JSONB),
    Column("label_snapshot", JSONB),
    Column("result", JSONB),
    timestamp("result_recorded_at"),
    Column("invalid_reason", Text),
    timestamp("created_at", nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('registered','awaiting_score','scoring','scored','awaiting_exit',"
        "'awaiting_quote','verifying','verified','missed','expired','failed')",
        name="ck_forward_registration_status",
    ),
    CheckConstraint("pool_size > 0 AND top_k > 0 AND top_k < pool_size", name="ck_forward_registration_k"),
    CheckConstraint("factor_direction IN ('high','low')", name="ck_forward_registration_direction"),
    CheckConstraint("signal_date < entry_date AND entry_date <= exit_date", name="ck_forward_registration_dates"),
)
Index("ix_forward_registration_status", forward_registration.c.status, forward_registration.c.created_at)
Index("ix_forward_registration_signal", forward_registration.c.signal_date)

forward_attempt = Table(
    "forward_attempt",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("registration_id", String(40), ForeignKey("forward_registration.id"), nullable=False),
    Column("kind", String(10), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("idempotency_key", String(80), nullable=False, unique=True),
    Column("task_id", String(40), ForeignKey("research_task.id")),
    timestamp("deadline_at", nullable=False),
    timestamp("lock_seen_at"),
    timestamp("staging_at"),
    timestamp("sealed_at"),
    Column("evidence_sha256", String(64)),
    Column("evidence_row_id", String(64)),
    Column("evidence", JSONB),
    Column("error", Text),
    timestamp("created_at", nullable=False, server_default=func.now()),
    CheckConstraint("kind IN ('score','verify')", name="ck_forward_attempt_kind"),
    CheckConstraint(
        "status IN ('queued','running','succeeded','failed','missed','cancelled','interrupted')",
        name="ck_forward_attempt_status",
    ),
    CheckConstraint("attempt_no > 0", name="ck_forward_attempt_no"),
)
Index(
    "uq_forward_attempt_active",
    forward_attempt.c.registration_id,
    forward_attempt.c.kind,
    unique=True,
    postgresql_where=forward_attempt.c.status.in_(("queued", "running")),
)
Index(
    "uq_forward_attempt_succeeded",
    forward_attempt.c.registration_id,
    forward_attempt.c.kind,
    unique=True,
    postgresql_where=forward_attempt.c.status == "succeeded",
)
Index(
    "uq_forward_attempt_no",
    forward_attempt.c.registration_id,
    forward_attempt.c.kind,
    forward_attempt.c.attempt_no,
    unique=True,
)
Index("uq_forward_attempt_queue", forward_attempt.c.status, forward_attempt.c.created_at)

MARKET_TABLES = {
    t.name: t
    for t in (
        security,
        security_alias,
        trade_calendar,
        daily_bar,
        adjustment,
        valuation,
        trading_status,
        index_member,
        industry_member,
        benchmark,
    )
}
