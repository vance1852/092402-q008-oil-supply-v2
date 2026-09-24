"""报价、库存、线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .force_majeure import (
    NominationBaseline,
    conservation_ledger,
    curtailment_targets,
    service_dates_window,
)
from .models import (
    ForceMajeureAppealRequest,
    ForceMajeureDeclaration,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    identifier,
    required_text,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ZERO_DEC = Decimal("0")
HUNDRED_DEC = Decimal("100")


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "report.read"},
    "risk": {"outage.write", "forcemajeure.write", "forcemajeure.rule", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read"},
    "shipper": {"appeal.write", "report.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM price_index_quotes WHERE price_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.price_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO price_index_quotes(price_index,trade_date,close_usd,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.price_index,
                        quote.trade_date,
                        decimal_text(quote.close_usd),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"price_index": quote.price_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("报价版本冲突") from exc
        return {"quote_id": quote_id, "price_index": quote.price_index, "trade_date": quote.trade_date}

    def price_summary(self, price_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_usd FROM price_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM price_index_quotes "
            "WHERE price_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (price_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_usd"])) for row in rows]
        if not points:
            raise NotFound("没有基准报价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "price_index": price_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_usd": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_barrels,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_barrels),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
                    "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.unit_cost_usd),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("库存批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("库存批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        summary = {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}
        reserved_row = self.connection.execute(
            "SELECT COALESCE(SUM(CAST(barrels AS REAL)),0) AS reserved FROM nomination_reservations "
            "WHERE facility_id=? AND product=?",
            (facility_id, product),
        ).fetchone()
        reserved = quantize_volume(Decimal(str(reserved_row["reserved"])))
        available = Decimal(summary["available_barrels"])
        summary["reserved_barrels"] = decimal_text(reserved)
        summary["unreserved_barrels"] = decimal_text(quantize_volume(available - reserved))
        return summary

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_barrels,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_barrels),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        fm_rows = self.connection.execute(
            "SELECT v.capacity_percent FROM force_majeure_cases c "
            "JOIN force_majeure_case_versions v ON v.case_id=c.case_id AND v.version=c.current_version "
            "WHERE c.route_id=? AND c.state='active' AND v.impact_starts_at<=? AND v.impact_ends_at>=?",
            (route["route_id"], end, start),
        ).fetchall()
        percentages.extend(Decimal(row["capacity_percent"]) for row in fm_rows)
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_barrels"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        occupied_row = self.connection.execute(
            "SELECT COALESCE(SUM(CAST(allocated_barrels AS REAL)),0) AS occupied FROM nominations "
            "WHERE route_id=? AND service_date=? AND state IN ('allocated','curtailed','in_transit')",
            (route_id, service_date),
        ).fetchone()
        available = quantize_volume(max(ZERO_DEC, available - Decimal(str(occupied_row["occupied"]))))
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_barrels"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_barrels=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_barrels"], state, item["nomination_id"]),
                )
                if state == "allocated":
                    self.connection.execute(
                        "INSERT INTO nomination_reservations(nomination_id,facility_id,product,barrels) "
                        "SELECT ?,r.origin_id,r.product,? FROM routes r WHERE r.route_id=?",
                        (item["nomination_id"], item["allocated_barrels"], route_id),
                    )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] not in {"allocated", "curtailed"} or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可发运版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        allocated = Decimal(nomination["allocated_barrels"])
        available = Decimal(lot["available_barrels"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("库存批次与线路起点或油品不匹配")
        if available < allocated:
            raise Conflict("库存不足以完成分配")
        reservation = self.connection.execute(
            "SELECT barrels FROM nomination_reservations WHERE nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if reservation is None or Decimal(reservation["barrels"]) != allocated:
            raise InvalidState("库存预留与当前分配不一致，不能发运")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "DELETE FROM nomination_reservations WHERE nomination_id=? AND barrels=?",
                (nomination_id, decimal_text(allocated)),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_barrels,"
                "expected_delivered_barrels,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_barrels": decimal_text(allocated),
            "expected_delivered_barrels": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    # ------------------------------------------------------------------
    # 不可抗力案件：冻结版本、确定性削减、申诉与恢复
    # ------------------------------------------------------------------

    AFFECTED_STATES = ("allocated", "curtailed", "in_transit", "delivered")

    def _case_row(self, case_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM force_majeure_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFound("不可抗力案件不存在")
        return row

    def _latest_case_version(self, case_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM force_majeure_case_versions WHERE case_id=? ORDER BY version DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if row is None:
            raise InvalidState("案件还没有任何版本")
        return row

    def _case_baseline_rows(self, case_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM force_majeure_baselines WHERE case_id=? "
            "ORDER BY service_date,contract_rank,nomination_id",
            (case_id,),
        ).fetchall()

    @staticmethod
    def _shipped_for(nomination_id: str, connection: sqlite3.Connection) -> Decimal:
        row = connection.execute(
            "SELECT loaded_barrels FROM transfers WHERE nomination_id=?",
            (nomination_id,),
        ).fetchone()
        return ZERO_DEC if row is None else quantize_volume(Decimal(row["loaded_barrels"]))

    def _freeze_entering_baselines(
        self, case_id: str, route_id: str, window_dates: list[str], entered_version: int
    ) -> int:
        """把新进入影响窗口的已配额提名按当前配额冻结为基线，并刷新合同排序。"""
        if not window_dates:
            self._refresh_contract_ranks(case_id)
            return 0
        placeholders = ",".join("?" for _ in window_dates)
        rows = self.connection.execute(
            f"SELECT * FROM nominations WHERE route_id=? AND service_date IN ({placeholders}) "
            f"AND state IN ({','.join('?' for _ in self.AFFECTED_STATES)}) "
            "AND nomination_id NOT IN "
            "(SELECT nomination_id FROM force_majeure_baselines WHERE case_id=?) "
            "ORDER BY service_date,priority,submitted_at,nomination_id",
            [route_id, *window_dates, *self.AFFECTED_STATES, case_id],
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "INSERT INTO force_majeure_baselines(case_id,nomination_id,shipper_id,route_id,"
                "service_date,priority,submitted_at,contract_rank,requested_barrels,"
                "baseline_barrels,entered_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    row["nomination_id"],
                    row["shipper_id"],
                    route_id,
                    row["service_date"],
                    int(row["priority"]),
                    row["submitted_at"],
                    0,
                    row["requested_barrels"],
                    row["allocated_barrels"],
                    entered_version,
                ),
            )
        self._refresh_contract_ranks(case_id)
        return len(rows)

    def _refresh_contract_ranks(self, case_id: str) -> None:
        rows = self.connection.execute(
            "SELECT nomination_id,service_date FROM force_majeure_baselines WHERE case_id=?",
            (case_id,),
        ).fetchall()
        by_date: dict[str, list[str]] = {}
        for row in rows:
            by_date.setdefault(row["service_date"], []).append(row["nomination_id"])
        for service_date, nomination_ids in by_date.items():
            peers = self.connection.execute(
                "SELECT nomination_id FROM force_majeure_baselines WHERE case_id=? AND service_date=? "
                "ORDER BY priority,submitted_at,nomination_id",
                (case_id, service_date),
            ).fetchall()
            for rank, peer in enumerate(peers, start=1):
                self.connection.execute(
                    "UPDATE force_majeure_baselines SET contract_rank=? "
                    "WHERE case_id=? AND nomination_id=?",
                    (rank, case_id, peer["nomination_id"]),
                )

    def declare_force_majeure(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "forcemajeure.write")
        declaration = ForceMajeureDeclaration.from_dict(raw)
        self.route(declaration.route_id)
        starts = utc_text(parse_utc(declaration.impact_starts_at))
        ends = utc_text(parse_utc(declaration.impact_ends_at))
        existing = self.connection.execute(
            "SELECT current_version FROM force_majeure_cases WHERE case_id=?",
            (declaration.case_id,),
        ).fetchone()
        if existing is not None:
            first = self.connection.execute(
                "SELECT * FROM force_majeure_case_versions WHERE case_id=? AND version=1",
                (declaration.case_id,),
            ).fetchone()
            evidence_hash = hashlib.sha256(
                canonical_json(dict(declaration.evidence)).encode("utf-8")
            ).hexdigest()
            same = (
                first is not None
                and first["impact_starts_at"] == starts
                and first["impact_ends_at"] == ends
                and Decimal(first["capacity_percent"]) == declaration.capacity_percent
                and hashlib.sha256(first["evidence_json"].encode("utf-8")).hexdigest() == evidence_hash
            )
            if not same:
                raise Conflict("案件编号已经存在且宣布内容不同")
            return self._version_view(declaration.case_id, 1, replayed=True)
        for row in self.connection.execute(
            "SELECT c.case_id,v.impact_starts_at AS s,v.impact_ends_at AS e "
            "FROM force_majeure_cases c "
            "JOIN force_majeure_case_versions v ON v.case_id=c.case_id AND v.version=c.current_version "
            "WHERE c.route_id=? AND c.state='active'",
            (declaration.route_id,),
        ).fetchall():
            if starts <= row["e"] and ends >= row["s"]:
                raise Conflict(f"线路已有生效案件 {row['case_id']} 与本次影响窗口重叠")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO force_majeure_cases(case_id,route_id,title,state,current_version,"
                    "appeal_deadline,created_by,created_at) VALUES(?,?,?,'active',0,?,?,?)",
                    (declaration.case_id, declaration.route_id, declaration.title,
                     utc_text(parse_utc(declaration.appeal_deadline)), actor_id, self._now()),
                )
                self._freeze_entering_baselines(
                    declaration.case_id, declaration.route_id, service_dates_window(starts, ends), 1
                )
                result = self._apply_case_version(
                    actor_id=actor_id,
                    case_id=declaration.case_id,
                    change_kind="declared",
                    starts_at=starts,
                    ends_at=ends,
                    capacity_percent=declaration.capacity_percent,
                    evidence=dict(declaration.evidence),
                    reason="宣布不可抗力",
                    idempotency_key=None,
                    freeze_others=False,
                    grants=None,
                )
                self._audit("force_majeure", declaration.case_id, "force_majeure.declared", actor_id,
                            {"version": result["version"], "route_id": declaration.route_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("案件编号已经存在") from exc
        return result

    def _apply_case_version(
        self,
        *,
        actor_id: str,
        case_id: str,
        change_kind: str,
        starts_at: str,
        ends_at: str,
        capacity_percent: Decimal,
        evidence: Mapping[str, Any],
        reason: str,
        idempotency_key: str | None,
        freeze_others: bool,
        grants: Mapping[str, Decimal] | None,
    ) -> dict[str, Any]:
        case = self._case_row(case_id)
        if idempotency_key is not None:
            replay = self.connection.execute(
                "SELECT version FROM force_majeure_case_versions WHERE case_id=? AND idempotency_key=?",
                (case_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                return self._version_view(case_id, int(replay["version"]), replayed=True)
        route = self.connection.execute(
            "SELECT * FROM routes WHERE route_id=?", (case["route_id"],)
        ).fetchone()
        nominal_capacity = Decimal(route["daily_capacity"])
        window = service_dates_window(starts_at, ends_at)
        next_version = int(case["current_version"]) + 1
        self._freeze_entering_baselines(case_id, case["route_id"], window, next_version)
        baseline_rows = self._case_baseline_rows(case_id)
        baselines = [
            NominationBaseline(
                nomination_id=row["nomination_id"],
                shipper_id=row["shipper_id"],
                service_date=row["service_date"],
                priority=int(row["priority"]),
                submitted_at=row["submitted_at"],
                baseline_barrels=Decimal(row["baseline_barrels"]),
                shipped_barrels=self._shipped_for(row["nomination_id"], self.connection),
            )
            for row in baseline_rows
        ]
        window_dates = set(window)
        window_rows = [row for row in baselines if row.service_date in window_dates]

        opening: dict[str, Decimal] = {}
        states_before: dict[str, str] = {}
        for row in baseline_rows:
            nom = self.connection.execute(
                "SELECT allocated_barrels,state FROM nominations WHERE nomination_id=?",
                (row["nomination_id"],),
            ).fetchone()
            opening[row["nomination_id"]] = Decimal(nom["allocated_barrels"])
            states_before[row["nomination_id"]] = nom["state"]

        if freeze_others:
            # 申诉裁决：其他已经生效的份额逐字冻结，只有申诉方可以恢复。
            targets = {nomination_id: value for nomination_id, value in opening.items()}
        else:
            targets = curtailment_targets(
                nominal_capacity=nominal_capacity,
                capacity_percent=capacity_percent,
                rows=window_rows,
            )
        # 离开当前窗口的提名恢复到宣布时冻结的基线。
        for row in baselines:
            if row.service_date not in window_dates:
                targets[row.nomination_id] = row.baseline_barrels

        cap = effective_capacity(nominal_capacity, [capacity_percent])
        grant_log: list[dict[str, Any]] = []
        if grants:
            for shipper_id, requested in grants.items():
                remaining_grant = quantize_volume(max(ZERO_DEC, requested))
                for row in sorted(
                    (item for item in window_rows if item.shipper_id == shipper_id),
                    key=lambda item: item.contract_key,
                ):
                    if remaining_grant <= ZERO_DEC:
                        break
                    used = sum(
                        (targets.get(item.nomination_id, ZERO_DEC) for item in window_rows
                         if item.service_date == row.service_date),
                        ZERO_DEC,
                    )
                    headroom = quantize_volume(cap - used)
                    current = targets.get(row.nomination_id, opening.get(row.nomination_id, ZERO_DEC))
                    ceiling = quantize_volume(row.baseline_barrels - row.shipped_barrels) + row.shipped_barrels
                    added = quantize_volume(
                        min(remaining_grant, max(ZERO_DEC, headroom), max(ZERO_DEC, ceiling - current))
                    )
                    if added > ZERO_DEC:
                        targets[row.nomination_id] = quantize_volume(current + added)
                        remaining_grant = quantize_volume(remaining_grant - added)
                        grant_log.append({
                            "shipper_id": shipper_id,
                            "nomination_id": row.nomination_id,
                            "restored_barrels": decimal_text(added),
                        })

        shipped_map = {
            row.nomination_id: row.shipped_barrels for row in baselines
        }
        reservations_before: dict[str, Decimal] = {}
        for row in baseline_rows:
            held = self.connection.execute(
                "SELECT barrels FROM nomination_reservations WHERE nomination_id=?",
                (row["nomination_id"],),
            ).fetchone()
            reservations_before[row["nomination_id"]] = ZERO_DEC if held is None else Decimal(held["barrels"])
        baseline_map = {row["nomination_id"]: Decimal(row["baseline_barrels"]) for row in baseline_rows}
        targets = {key: quantize_volume(value) for key, value in targets.items()}
        ledger = conservation_ledger(
            opening=opening,
            targets=targets,
            baseline=baseline_map,
            shipped=shipped_map,
            reservation_before=reservations_before,
        )
        evidence_json = canonical_json(evidence)
        input_payload = {
            "case_id": case_id,
            "change_kind": change_kind,
            "window": [starts_at, ends_at],
            "capacity_percent": decimal_text(capacity_percent),
            "evidence_sha256": hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
            "freeze_others": freeze_others,
            "opening": {key: decimal_text(value) for key, value in sorted(opening.items())},
            "shipped": {key: decimal_text(value) for key, value in sorted(shipped_map.items())},
            "baselines": [
                {"nomination_id": row["nomination_id"], "baseline": row["baseline_barrels"],
                 "rank": row["contract_rank"], "entered_version": row["entered_version"]}
                for row in baseline_rows
            ],
            "grants": grant_log,
        }
        input_sha256 = digest(input_payload)
        self.connection.execute(
            "INSERT INTO force_majeure_case_versions(case_id,version,change_kind,impact_starts_at,"
            "impact_ends_at,capacity_percent,evidence_json,input_sha256,idempotency_key,"
            "conservation_json,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, next_version, change_kind, starts_at, ends_at, decimal_text(capacity_percent),
             evidence_json, input_sha256, idempotency_key, canonical_json(ledger), reason,
             actor_id, self._now()),
        )
        for base in baseline_rows:
            nomination_id = base["nomination_id"]
            before = opening[nomination_id]
            after = targets[nomination_id]
            shipped = shipped_map[nomination_id]
            released = quantize_volume(max(ZERO_DEC, before - after))
            restored = quantize_volume(max(ZERO_DEC, after - before))
            self.connection.execute(
                "INSERT INTO force_majeure_curtailments(case_id,version,nomination_id,shipper_id,"
                "route_id,service_date,contract_rank,state_before,requested_barrels,allocated_before,"
                "allocated_after,curtail_barrels,restored_barrels,in_transit_barrels) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, next_version, nomination_id, base["shipper_id"], case["route_id"],
                 base["service_date"], base["contract_rank"], states_before[nomination_id],
                 base["requested_barrels"], decimal_text(before), decimal_text(after),
                 decimal_text(released), decimal_text(restored), decimal_text(shipped)),
            )
            held_before = reservations_before[nomination_id]
            held_after = quantize_volume(after - shipped)
            self.connection.execute(
                "INSERT INTO force_majeure_reservation_moves(case_id,version,nomination_id,"
                "reservation_before,reservation_after,released_barrels,restored_barrels) "
                "VALUES(?,?,?,?,?,?,?)",
                (case_id, next_version, nomination_id, decimal_text(held_before),
                 decimal_text(held_after),
                 decimal_text(quantize_volume(max(ZERO_DEC, held_before - held_after))),
                 decimal_text(quantize_volume(max(ZERO_DEC, held_after - held_before)))),
            )
            self._apply_nomination_version(base, after, shipped, states_before[nomination_id])
        self.connection.execute(
            "UPDATE force_majeure_cases SET current_version=? WHERE case_id=?",
            (next_version, case_id),
        )
        self._audit("force_majeure", case_id, f"force_majeure.{change_kind}", actor_id,
                    {"version": next_version, "conservation": ledger["allocation"]})
        return self._version_view(case_id, next_version, replayed=False)

    def _apply_nomination_version(
        self, base: sqlite3.Row, after: Decimal, shipped: Decimal, previous_state: str
    ) -> None:
        if shipped > ZERO_DEC:
            # 已在途或交付：状态与在途数量保持不变，只调整剩余预留。
            new_state = previous_state
        elif after >= Decimal(base["baseline_barrels"]):
            new_state = "allocated"
        else:
            new_state = "curtailed"
        held_after = quantize_volume(after - shipped)
        self.connection.execute(
            "UPDATE nominations SET allocated_barrels=?,state=?,revision=revision+1 WHERE nomination_id=?",
            (decimal_text(after), new_state, base["nomination_id"]),
        )
        if held_after <= ZERO_DEC:
            self.connection.execute(
                "DELETE FROM nomination_reservations WHERE nomination_id=?", (base["nomination_id"],)
            )
        else:
            self.connection.execute(
                "INSERT INTO nomination_reservations(nomination_id,facility_id,product,barrels) "
                "VALUES(?,(SELECT origin_id FROM routes WHERE route_id=?),"
                "(SELECT product FROM routes WHERE route_id=?),?) "
                "ON CONFLICT(nomination_id) DO UPDATE SET barrels=excluded.barrels",
                (base["nomination_id"], base["route_id"], base["route_id"], decimal_text(held_after)),
            )

    def _version_view(self, case_id: str, version: int, *, replayed: bool) -> dict[str, Any]:
        version_row = self.connection.execute(
            "SELECT * FROM force_majeure_case_versions WHERE case_id=? AND version=?",
            (case_id, version),
        ).fetchone()
        curtailments = self.connection.execute(
            "SELECT nomination_id,shipper_id,service_date,contract_rank,state_before,"
            "allocated_before,allocated_after,curtail_barrels,restored_barrels,in_transit_barrels "
            "FROM force_majeure_curtailments WHERE case_id=? AND version=? "
            "ORDER BY service_date,contract_rank,nomination_id",
            (case_id, version),
        ).fetchall()
        return {
            "case_id": case_id,
            "version": version,
            "change_kind": version_row["change_kind"],
            "impact_starts_at": version_row["impact_starts_at"],
            "impact_ends_at": version_row["impact_ends_at"],
            "capacity_percent": version_row["capacity_percent"],
            "state": self._case_row(case_id)["state"],
            "input_sha256": version_row["input_sha256"],
            "conservation": json.loads(version_row["conservation_json"]),
            "curtailments": [dict(row) for row in curtailments],
            "replayed": replayed,
        }

    def _require_active_case(self, case_id: str) -> sqlite3.Row:
        case = self._case_row(case_id)
        if case["state"] != "active":
            raise InvalidState("案件已经结束或撤销，不能再产生版本")
        return case

    def _replay_version_if_seen(self, case_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """同一版本幂等键重复处理时，直接返回已经落库的版本，不再释放任何数量。"""
        row = self.connection.execute(
            "SELECT version FROM force_majeure_case_versions WHERE case_id=? AND idempotency_key=?",
            (case_id, idempotency_key),
        ).fetchone()
        return None if row is None else self._version_view(case_id, int(row["version"]), replayed=True)

    @staticmethod
    def _validate_percentage(value: object) -> Decimal:
        percentage = Decimal(str(value))
        if not ZERO_DEC <= percentage <= HUNDRED_DEC:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        return percentage

    def extend_force_majeure(
        self,
        actor_id: str,
        case_id: str,
        impact_ends_at: str,
        capacity_percent: object,
        evidence: Mapping[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "forcemajeure.write")
        key = identifier(idempotency_key, "idempotency_key")
        replay = self._replay_version_if_seen(case_id, key)
        if replay is not None:
            return replay
        self._require_active_case(case_id)
        latest = self._latest_case_version(case_id)
        new_end = utc_text(parse_utc(impact_ends_at, "impact_ends_at"))
        if new_end <= latest["impact_ends_at"]:
            raise ValidationFailed("延长后的结束时间必须晚于当前版本结束时间")
        percentage = self._validate_percentage(capacity_percent)
        if not isinstance(evidence, Mapping) or not evidence:
            raise ValidationFailed("evidence 必须是非空证据对象")
        with transaction(self.connection, immediate=True):
            return self._apply_case_version(
                actor_id=actor_id,
                case_id=case_id,
                change_kind="extended",
                starts_at=latest["impact_starts_at"],
                ends_at=new_end,
                capacity_percent=percentage,
                evidence=dict(evidence),
                reason=required_text(reason, "reason"),
                idempotency_key=key,
                freeze_others=False,
                grants=None,
            )

    def cancel_force_majeure(
        self, actor_id: str, case_id: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        self._require(actor_id, "forcemajeure.write")
        key = identifier(idempotency_key, "idempotency_key")
        replay = self._replay_version_if_seen(case_id, key)
        if replay is not None:
            replay["state"] = "cancelled"
            return replay
        self._require_active_case(case_id)
        latest = self._latest_case_version(case_id)
        with transaction(self.connection, immediate=True):
            result = self._apply_case_version(
                actor_id=actor_id,
                case_id=case_id,
                change_kind="cancelled",
                starts_at=latest["impact_starts_at"],
                ends_at=latest["impact_ends_at"],
                capacity_percent=HUNDRED_DEC,
                evidence={"cancelled_at": self._now()},
                reason=required_text(reason, "reason"),
                idempotency_key=key,
                freeze_others=False,
                grants=None,
            )
            self.connection.execute(
                "UPDATE force_majeure_cases SET state='cancelled' WHERE case_id=?", (case_id,)
            )
            result["state"] = "cancelled"
        return result

    def end_force_majeure(
        self, actor_id: str, case_id: str, ended_at: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        self._require(actor_id, "forcemajeure.write")
        key = identifier(idempotency_key, "idempotency_key")
        replay = self._replay_version_if_seen(case_id, key)
        if replay is not None:
            replay["state"] = "ended"
            return replay
        self._require_active_case(case_id)
        latest = self._latest_case_version(case_id)
        end_text = utc_text(parse_utc(ended_at, "ended_at"))
        if not latest["impact_starts_at"] < end_text < latest["impact_ends_at"]:
            raise ValidationFailed("提前结束时间必须位于当前影响窗口内部")
        with transaction(self.connection, immediate=True):
            result = self._apply_case_version(
                actor_id=actor_id,
                case_id=case_id,
                change_kind="ended",
                starts_at=latest["impact_starts_at"],
                ends_at=end_text,
                capacity_percent=HUNDRED_DEC,
                evidence={"ended_at": end_text},
                reason=required_text(reason, "reason"),
                idempotency_key=key,
                freeze_others=False,
                grants=None,
            )
            self.connection.execute(
                "UPDATE force_majeure_cases SET state='ended' WHERE case_id=?", (case_id,)
            )
            result["state"] = "ended"
        return result

    def submit_appeal(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "appeal.write")
        appeal = ForceMajeureAppealRequest.from_dict(raw)
        if appeal.shipper_id != actor_id:
            raise Forbidden("只能为自己的托运方编号提交申诉")
        case = self._case_row(appeal.case_id)
        if case["state"] != "active":
            raise InvalidState("案件已经结束，不能再申诉")
        deadline = parse_utc(case["appeal_deadline"])
        if self.clock.now().astimezone(timezone.utc) > deadline:
            raise InvalidState("申诉截止时间已过")
        owns = self.connection.execute(
            "SELECT 1 FROM force_majeure_baselines WHERE case_id=? AND shipper_id=? LIMIT 1",
            (appeal.case_id, appeal.shipper_id),
        ).fetchone()
        if owns is None:
            raise NotFound("该托运方在案件中没有受影响配额")
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256 FROM force_majeure_appeals WHERE idempotency_key=?",
            (appeal.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同申诉内容")
            row = self.connection.execute(
                "SELECT * FROM force_majeure_appeals WHERE idempotency_key=?",
                (appeal.idempotency_key,),
            ).fetchone()
            return self._appeal_view(row)
        existing = self.connection.execute(
            "SELECT appeal_id FROM force_majeure_appeals WHERE case_id=? AND shipper_id=?",
            (appeal.case_id, appeal.shipper_id),
        ).fetchone()
        if existing is not None:
            raise Conflict("每家托运方在截止前只能申诉一次")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO force_majeure_appeals(appeal_id,case_id,shipper_id,reason,state,"
                    "submitted_version,requested_adjustment_barrels,idempotency_key,request_sha256,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,'submitted',?,?,?,?,?,?)",
                    (appeal.appeal_id, appeal.case_id, appeal.shipper_id, appeal.reason,
                     int(case["current_version"]), decimal_text(appeal.requested_barrels),
                     appeal.idempotency_key, request_digest, actor_id, self._now()),
                )
                self._audit("force_majeure", appeal.case_id, "appeal.submitted", actor_id,
                            {"appeal_id": appeal.appeal_id, "shipper_id": appeal.shipper_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("申诉编号或幂等键冲突，或该托运方已经申诉过") from exc
        row = self.connection.execute(
            "SELECT * FROM force_majeure_appeals WHERE appeal_id=?", (appeal.appeal_id,)
        ).fetchone()
        return self._appeal_view(row)

    def rule_appeal(
        self,
        actor_id: str,
        appeal_id: str,
        granted: bool,
        granted_barrels: object,
        idempotency_key: str,
        capacity_percent: object | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "forcemajeure.rule")
        appeal_row = self.connection.execute(
            "SELECT * FROM force_majeure_appeals WHERE appeal_id=?", (appeal_id,)
        ).fetchone()
        if appeal_row is None:
            raise NotFound("申诉不存在")
        key = identifier(idempotency_key, "idempotency_key")
        replay = self.connection.execute(
            "SELECT version FROM force_majeure_case_versions "
            "WHERE case_id=? AND change_kind='appeal_ruling' AND idempotency_key=?",
            (appeal_row["case_id"], key),
        ).fetchone()
        if replay is not None:
            row = self.connection.execute(
                "SELECT * FROM force_majeure_appeals WHERE appeal_id=?", (appeal_id,)
            ).fetchone()
            return {"appeal": self._appeal_view(row),
                    "version": self._version_view(appeal_row["case_id"], int(replay["version"]), replayed=True)}
        if appeal_row["state"] != "submitted":
            raise InvalidState("申诉已经裁决")
        case = self._require_active_case(appeal_row["case_id"])
        latest = self._latest_case_version(case["case_id"])
        percentage = Decimal(latest["capacity_percent"])
        if capacity_percent is not None:
            new_percentage = self._validate_percentage(capacity_percent)
            if new_percentage < percentage:
                raise ValidationFailed("裁决只能维持或上调容量上限，不能借裁决削减他人")
            percentage = new_percentage
        granted_amount = ZERO_DEC
        if granted:
            granted_amount = quantize_volume(Decimal(str(granted_barrels)))
            if granted_amount <= ZERO_DEC:
                raise ValidationFailed("同意申诉时 granted_barrels 必须为正数")
        with transaction(self.connection, immediate=True):
            result = self._apply_case_version(
                actor_id=actor_id,
                case_id=case["case_id"],
                change_kind="appeal_ruling",
                starts_at=latest["impact_starts_at"],
                ends_at=latest["impact_ends_at"],
                capacity_percent=percentage,
                evidence={"appeal_id": appeal_id, "granted": granted,
                          "granted_barrels": decimal_text(granted_amount)},
                reason=f"申诉 {appeal_id} 裁决",
                idempotency_key=key,
                freeze_others=True,
                grants={appeal_row["shipper_id"]: granted_amount} if granted else None,
            )
            actual_grant = quantize_volume(sum(
                (Decimal(row["restored_barrels"]) for row in result["curtailments"]
                 if row["shipper_id"] == appeal_row["shipper_id"]),
                ZERO_DEC,
            ))
            others_changed = any(
                Decimal(row["curtail_barrels"]) > ZERO_DEC or
                (Decimal(row["allocated_after"]) != Decimal(row["allocated_before"])
                 and row["shipper_id"] != appeal_row["shipper_id"])
                for row in result["curtailments"]
            )
            if others_changed:
                raise InvalidState("裁决不得改变其他托运方已经生效的份额")
            if granted and actual_grant < granted_amount:
                raise InvalidState("容量余量不足以恢复申请数量，请随裁决上调容量上限")
            self.connection.execute(
                "UPDATE force_majeure_appeals SET state=?,granted_barrels=?,ruled_version=?,"
                "ruled_by=?,ruled_at=? WHERE appeal_id=?",
                ("granted" if granted else "denied", decimal_text(actual_grant),
                 result["version"], actor_id, self._now(), appeal_id),
            )
            self._audit("force_majeure", case["case_id"], "appeal.ruled", actor_id,
                        {"appeal_id": appeal_id, "granted": decimal_text(actual_grant)})
        row = self.connection.execute(
            "SELECT * FROM force_majeure_appeals WHERE appeal_id=?", (appeal_id,)
        ).fetchone()
        return {"appeal": self._appeal_view(row), "version": result}

    def _appeal_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "appeal_id": row["appeal_id"],
            "case_id": row["case_id"],
            "shipper_id": row["shipper_id"],
            "state": row["state"],
            "submitted_version": row["submitted_version"],
            "ruled_version": row["ruled_version"],
            "requested_adjustment_barrels": row["requested_adjustment_barrels"],
            "granted_barrels": row["granted_barrels"],
            "reason": row["reason"],
        }

    def force_majeure_case(self, actor_id: str, case_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        case = self._case_row(case_id)
        versions = self.connection.execute(
            "SELECT version,change_kind,impact_starts_at,impact_ends_at,capacity_percent,"
            "input_sha256,evidence_json,conservation_json,reason,created_by,created_at,idempotency_key "
            "FROM force_majeure_case_versions WHERE case_id=? ORDER BY version",
            (case_id,),
        ).fetchall()
        appeals = self.connection.execute(
            "SELECT * FROM force_majeure_appeals WHERE case_id=? ORDER BY submitted_at,appeal_id",
            (case_id,),
        ).fetchall()
        return {
            "case_id": case_id,
            "route_id": case["route_id"],
            "title": case["title"],
            "state": case["state"],
            "current_version": case["current_version"],
            "appeal_deadline": case["appeal_deadline"],
            "versions": [
                {
                    **{key: row[key] for key in row.keys() if key != "conservation_json"},
                    "conservation": json.loads(row["conservation_json"]),
                }
                for row in versions
            ],
            "appeals": [self._appeal_view(row) for row in appeals],
        }

    def nomination_force_majeure_trace(self, actor_id: str, nomination_id: str) -> dict[str, Any]:
        """托运方视角：从一条提名直达案件、合同排序、申诉和恢复记录。"""
        actor = self._require(actor_id, "report.read")
        nomination = self.connection.execute(
            "SELECT * FROM nominations WHERE nomination_id=?", (nomination_id,)
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if actor["role"] == "shipper" and nomination["shipper_id"] != actor_id:
            raise Forbidden("只能查看自己托运方的提名")
        case_rows = self.connection.execute(
            "SELECT DISTINCT case_id FROM force_majeure_baselines WHERE nomination_id=? ORDER BY case_id",
            (nomination_id,),
        ).fetchall()
        cases = []
        for item in case_rows:
            case_id = item["case_id"]
            versions = self.connection.execute(
                "SELECT v.version,v.change_kind,v.impact_starts_at,v.impact_ends_at,v.capacity_percent,"
                "v.conservation_json,c.state_before,c.allocated_before,c.allocated_after,"
                "c.curtail_barrels,c.restored_barrels,c.in_transit_barrels,"
                "b.contract_rank,b.entered_version "
                "FROM force_majeure_case_versions v "
                "JOIN force_majeure_curtailments c ON c.case_id=v.case_id AND c.version=v.version "
                "JOIN force_majeure_baselines b ON b.case_id=c.case_id AND b.nomination_id=c.nomination_id "
                "WHERE c.nomination_id=? AND v.case_id=? ORDER BY v.version",
                (nomination_id, case_id),
            ).fetchall()
            appeal = self.connection.execute(
                "SELECT * FROM force_majeure_appeals WHERE case_id=? AND shipper_id=?",
                (case_id, nomination["shipper_id"]),
            ).fetchone()
            cases.append({
                "case_id": case_id,
                "state": self._case_row(case_id)["state"],
                "contract_rank": versions[0]["contract_rank"] if versions else None,
                "entered_version": versions[0]["entered_version"] if versions else None,
                "versions": [
                    {
                        "version": row["version"],
                        "change_kind": row["change_kind"],
                        "impact_window": [row["impact_starts_at"], row["impact_ends_at"]],
                        "capacity_percent": row["capacity_percent"],
                        "state_before": row["state_before"],
                        "allocated_before": row["allocated_before"],
                        "allocated_after": row["allocated_after"],
                        "curtail_barrels": row["curtail_barrels"],
                        "restored_barrels": row["restored_barrels"],
                        "in_transit_barrels": row["in_transit_barrels"],
                        "case_conservation": json.loads(row["conservation_json"]),
                    }
                    for row in versions
                ],
                "appeal": None if appeal is None else self._appeal_view(appeal),
            })
        return {
            "nomination_id": nomination_id,
            "shipper_id": nomination["shipper_id"],
            "route_id": nomination["route_id"],
            "service_date": nomination["service_date"],
            "requested_barrels": nomination["requested_barrels"],
            "allocated_barrels": nomination["allocated_barrels"],
            "state": nomination["state"],
            "revision": nomination["revision"],
            "force_majeure_cases": cases,
        }


    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_usd FROM price_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用报价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_barrels AS REAL)) available_barrels "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_usd"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_usd"]),
            price_index_drop_percent=scenario.price_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
