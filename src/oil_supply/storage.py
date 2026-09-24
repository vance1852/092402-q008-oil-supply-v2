"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','shipper')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_usd TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES price_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(price_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON price_index_quotes(price_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_barrels TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_barrels TEXT NOT NULL,
    available_barrels TEXT NOT NULL,
    unit_cost_usd TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_barrels TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_barrels TEXT NOT NULL,
    allocated_barrels TEXT NOT NULL DEFAULT '0',
    delivered_barrels TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled','curtailed')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS nomination_reservations (
    nomination_id TEXT PRIMARY KEY REFERENCES nominations(nomination_id),
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    barrels TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS force_majeure_cases (
    case_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','cancelled','ended')),
    current_version INTEGER NOT NULL DEFAULT 0,
    appeal_deadline TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS force_majeure_case_versions (
    case_id TEXT NOT NULL REFERENCES force_majeure_cases(case_id),
    version INTEGER NOT NULL,
    change_kind TEXT NOT NULL
        CHECK(change_kind IN ('declared','extended','cancelled','ended','appeal_ruling')),
    impact_starts_at TEXT NOT NULL,
    impact_ends_at TEXT NOT NULL,
    capacity_percent TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    idempotency_key TEXT,
    conservation_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(case_id, version),
    UNIQUE(case_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS force_majeure_curtailments (
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    nomination_id TEXT NOT NULL,
    shipper_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    contract_rank INTEGER,
    state_before TEXT NOT NULL,
    requested_barrels TEXT NOT NULL,
    allocated_before TEXT NOT NULL,
    allocated_after TEXT NOT NULL,
    curtail_barrels TEXT NOT NULL,
    restored_barrels TEXT NOT NULL,
    in_transit_barrels TEXT NOT NULL,
    PRIMARY KEY(case_id, version, nomination_id)
);

CREATE INDEX IF NOT EXISTS idx_fm_curtailment_nomination
ON force_majeure_curtailments(nomination_id, case_id, version);

CREATE TABLE IF NOT EXISTS force_majeure_reservation_moves (
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    nomination_id TEXT NOT NULL,
    reservation_before TEXT NOT NULL,
    reservation_after TEXT NOT NULL,
    released_barrels TEXT NOT NULL,
    restored_barrels TEXT NOT NULL,
    PRIMARY KEY(case_id, version, nomination_id)
);

CREATE TABLE IF NOT EXISTS force_majeure_baselines (
    case_id TEXT NOT NULL,
    nomination_id TEXT NOT NULL,
    shipper_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    priority INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    contract_rank INTEGER NOT NULL,
    requested_barrels TEXT NOT NULL,
    baseline_barrels TEXT NOT NULL,
    entered_version INTEGER NOT NULL,
    PRIMARY KEY(case_id, nomination_id)
);

CREATE TABLE IF NOT EXISTS force_majeure_appeals (
    appeal_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES force_majeure_cases(case_id),
    shipper_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted' CHECK(state IN ('submitted','granted','denied')),
    submitted_version INTEGER NOT NULL,
    ruled_version INTEGER,
    requested_adjustment_barrels TEXT NOT NULL,
    granted_barrels TEXT NOT NULL DEFAULT '0',
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL,
    ruled_by TEXT REFERENCES supply_users(user_id),
    ruled_at TEXT,
    UNIQUE(case_id, shipper_id)
);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    loaded_barrels TEXT NOT NULL,
    expected_delivered_barrels TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        _migrate_version_one(connection)
        version = 1
        connection.execute("PRAGMA user_version=1")
    if version < 2:
        _migrate_version_two(connection)
        connection.execute("PRAGMA user_version=2")


def _migrate_version_one(connection: sqlite3.Connection) -> None:
    """nominations 状态增加 curtailed；SQLite 不能改 CHECK，需要重建表。"""
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='nominations'"
    ).fetchone()[0]
    if "'curtailed'" in sql:
        return
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.executescript(
        """
        CREATE TABLE nominations_migrated (
            nomination_id TEXT PRIMARY KEY,
            route_id TEXT NOT NULL REFERENCES routes(route_id),
            shipper_id TEXT NOT NULL,
            service_date TEXT NOT NULL,
            requested_barrels TEXT NOT NULL,
            allocated_barrels TEXT NOT NULL DEFAULT '0',
            delivered_barrels TEXT NOT NULL DEFAULT '0',
            priority INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'submitted'
                CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled','curtailed')),
            revision INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL UNIQUE,
            submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
            submitted_at TEXT NOT NULL
        );
        INSERT INTO nominations_migrated
        SELECT nomination_id,route_id,shipper_id,service_date,requested_barrels,allocated_barrels,
               delivered_barrels,priority,state,revision,idempotency_key,submitted_by,submitted_at
        FROM nominations;
        DROP TABLE nominations;
        ALTER TABLE nominations_migrated RENAME TO nominations;
        CREATE INDEX idx_nominations_schedule
        ON nominations(route_id, service_date, priority, submitted_at);
        """
    )
    connection.execute("PRAGMA foreign_keys=ON")


def _migrate_version_two(connection: sqlite3.Connection) -> None:
    """角色增加 shipper；同样需要重建带 CHECK 的用户表。"""
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='supply_users'"
    ).fetchone()[0]
    if "'shipper'" in sql:
        return
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.executescript(
        """
        CREATE TABLE supply_users_migrated (
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','shipper')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL
        );
        INSERT INTO supply_users_migrated
        SELECT user_id,display_name,role,active,created_at FROM supply_users;
        DROP TABLE supply_users;
        ALTER TABLE supply_users_migrated RENAME TO supply_users;
        """
    )
    connection.execute("PRAGMA foreign_keys=ON")


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
