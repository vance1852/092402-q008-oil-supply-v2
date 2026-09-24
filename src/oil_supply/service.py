"""报价、库存、线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    AppealDecision,
    AppealRequest,
    ForceMajeureDeclaration,
    ForceMajeureRevision,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    ReservationRequest,
    Route,
    SupplyScenario,
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


ZERO = Decimal("0")
HUNDRED = Decimal("100")

ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {
        "nomination.write",
        "allocation.run",
        "transfer.write",
        "inventory.write",
        "appeal.file",
        "force_majeure.read",
    },
    "risk": {
        "outage.write",
        "scenario.approve",
        "report.read",
        "force_majeure.write",
        "force_majeure.process",
        "appeal.decide",
        "force_majeure.read",
    },
    "auditor": {"report.read", "audit.read", "force_majeure.read"},
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
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

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
            "SELECT v.capacity_percent FROM force_majeure_versions v "
            "JOIN force_majeure_cases c ON c.case_id=v.case_id "
            "WHERE c.route_id=? AND c.state='open' AND v.starts_at<=? AND v.ends_at>=? "
            "AND v.version_no=(SELECT max(version_no) FROM force_majeure_versions WHERE case_id=c.case_id) "
            "ORDER BY v.case_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages += [Decimal(row["capacity_percent"]) for row in fm_rows]
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
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可发运版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        allocated = Decimal(nomination["allocated_barrels"])
        available = Decimal(lot["available_barrels"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("库存批次与线路起点或油品不匹配")
        held_rows = self.connection.execute(
            "SELECT * FROM inventory_reservations WHERE nomination_id=? AND lot_id=? AND state='held' "
            "ORDER BY reservation_id",
            (nomination_id, lot_id),
        ).fetchall()
        held_total = sum((Decimal(row["held_barrels"]) for row in held_rows), ZERO)
        remainder = quantize_volume(allocated - min(held_total, allocated))
        if available < remainder:
            raise Conflict("库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            to_consume = min(held_total, allocated)
            for held_row in held_rows:
                if to_consume <= ZERO:
                    break
                held = Decimal(held_row["held_barrels"])
                take = min(held, to_consume)
                to_consume = quantize_volume(to_consume - take)
                new_held = quantize_volume(held - take)
                cursor = self.connection.execute(
                    "UPDATE inventory_reservations SET held_barrels=?,state=?,revision=revision+1 "
                    "WHERE reservation_id=? AND revision=?",
                    (
                        decimal_text(new_held),
                        "consumed" if new_held == ZERO else "held",
                        held_row["reservation_id"],
                        held_row["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise Conflict("库存预留已被并发修改")
            self.connection.execute(
                "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - remainder)), lot_id, lot["revision"]),
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

    def reserve_inventory(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        request = ReservationRequest.from_dict(raw)
        nomination = self.connection.execute(
            "SELECT * FROM nominations WHERE nomination_id=?", (request.nomination_id,)
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated":
            raise InvalidState("只有已分配提名可以预留库存")
        lot = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE lot_id=?", (request.lot_id,)
        ).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        route = self.route(nomination["route_id"])
        if lot["facility_id"] != route["origin_id"] or lot["product"] != route["product"]:
            raise Conflict("库存批次与线路起点或油品不匹配")
        quantity = quantize_volume(request.quantity_barrels)
        held_total = self._held_total(request.nomination_id)
        if held_total + quantity > Decimal(nomination["allocated_barrels"]):
            raise Conflict("预留总量超过已分配数量")
        available = Decimal(lot["available_barrels"])
        if available < quantity:
            raise Conflict("库存不足以预留")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                    (decimal_text(quantize_volume(available - quantity)), request.lot_id, lot["revision"]),
                )
                if cursor.rowcount != 1:
                    raise Conflict("库存批次已被并发修改")
                self.connection.execute(
                    "INSERT INTO inventory_reservations(reservation_id,lot_id,nomination_id,quantity_barrels,"
                    "held_barrels,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        request.reservation_id,
                        request.lot_id,
                        request.nomination_id,
                        decimal_text(quantity),
                        decimal_text(quantity),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "inventory_reservation",
                    request.reservation_id,
                    "inventory.reserved",
                    actor_id,
                    {
                        "nomination_id": request.nomination_id,
                        "lot_id": request.lot_id,
                        "quantity_barrels": decimal_text(quantity),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("预留编号已经存在") from exc
        return {
            "reservation_id": request.reservation_id,
            "nomination_id": request.nomination_id,
            "lot_id": request.lot_id,
            "held_barrels": decimal_text(quantity),
            "state": "held",
        }

    def _held_total(self, nomination_id: str) -> Decimal:
        rows = self.connection.execute(
            "SELECT held_barrels FROM inventory_reservations WHERE nomination_id=? AND state='held'",
            (nomination_id,),
        ).fetchall()
        return sum((Decimal(row["held_barrels"]) for row in rows), ZERO)

    def _fm_case(self, case_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM force_majeure_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFound("不可抗力案件不存在")
        return row

    def _fm_latest_version(self, case_id: str) -> sqlite3.Row:
        return self.connection.execute(
            "SELECT * FROM force_majeure_versions WHERE case_id=? ORDER BY version_no DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    @staticmethod
    def _fm_version_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version_no": row["version_no"],
            "kind": row["kind"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "capacity_percent": row["capacity_percent"],
            "appeal_deadline": row["appeal_deadline"],
            "evidence": json.loads(row["evidence_json"]),
            "evidence_sha256": row["evidence_sha256"],
            "frozen_by": row["frozen_by"],
            "frozen_at": row["frozen_at"],
        }

    def _freeze_version(
        self,
        case_id: str,
        kind: str,
        evidence: Mapping[str, Any],
        starts_at: str,
        ends_at: str,
        capacity_percent: Decimal,
        appeal_deadline: str,
        actor_id: str,
    ) -> tuple[int, int, str]:
        latest = self._fm_latest_version(case_id)
        version_no = 1 if latest is None else int(latest["version_no"]) + 1
        evidence_json = canonical_json(evidence)
        evidence_sha256 = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        cursor = self.connection.execute(
            "INSERT INTO force_majeure_versions(case_id,version_no,kind,evidence_json,evidence_sha256,starts_at,"
            "ends_at,capacity_percent,appeal_deadline,supersedes_version_id,frozen_by,frozen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                version_no,
                kind,
                evidence_json,
                evidence_sha256,
                starts_at,
                ends_at,
                decimal_text(capacity_percent),
                appeal_deadline,
                None if latest is None else latest["version_id"],
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid), version_no, evidence_sha256

    def declare_force_majeure(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "force_majeure.write")
        declaration = ForceMajeureDeclaration.from_dict(raw)
        self.route(declaration.route_id)
        start = parse_utc(declaration.starts_at, "starts_at")
        end = parse_utc(declaration.ends_at, "ends_at")
        if end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        deadline = parse_utc(declaration.appeal_deadline, "appeal_deadline")
        if deadline <= self.clock.now():
            raise ValidationFailed("appeal_deadline 必须晚于当前时间")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO force_majeure_cases(case_id,route_id,opened_by,opened_at) VALUES(?,?,?,?)",
                    (declaration.case_id, declaration.route_id, actor_id, self._now()),
                )
                _, version_no, evidence_sha256 = self._freeze_version(
                    declaration.case_id,
                    "declare",
                    declaration.evidence,
                    utc_text(start),
                    utc_text(end),
                    declaration.capacity_percent,
                    utc_text(deadline),
                    actor_id,
                )
                self._audit(
                    "force_majeure",
                    declaration.case_id,
                    "force_majeure.declared",
                    actor_id,
                    {
                        "route_id": declaration.route_id,
                        "version_no": version_no,
                        "evidence_sha256": evidence_sha256,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("案件编号已经存在") from exc
        return {
            "case_id": declaration.case_id,
            "route_id": declaration.route_id,
            "state": "open",
            "version_no": version_no,
            "evidence_sha256": evidence_sha256,
        }

    def revise_force_majeure(self, actor_id: str, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "force_majeure.write")
        revision = ForceMajeureRevision.from_dict(raw)
        with transaction(self.connection, immediate=True):
            case = self._fm_case(case_id)
            if case["state"] != "open":
                raise InvalidState("案件已撤销，不能再冻结新版本")
            latest = self._fm_latest_version(case_id)
            latest_starts = parse_utc(latest["starts_at"], "starts_at")
            latest_ends = parse_utc(latest["ends_at"], "ends_at")
            starts = latest_starts if revision.starts_at is None else parse_utc(revision.starts_at, "starts_at")
            ends = latest_ends if revision.ends_at is None else parse_utc(revision.ends_at, "ends_at")
            capacity = (
                Decimal(latest["capacity_percent"])
                if revision.capacity_percent is None
                else revision.capacity_percent
            )
            deadline = revision.appeal_deadline or latest["appeal_deadline"]
            evidence = revision.evidence if revision.evidence is not None else json.loads(latest["evidence_json"])
            if revision.kind == "extend":
                if revision.ends_at is None or ends <= latest_ends:
                    raise ValidationFailed("延长必须提供更晚的 ends_at")
                if starts != latest_starts:
                    raise ValidationFailed("延长不能改变 starts_at")
            elif revision.kind == "end_early":
                if revision.ends_at is None or ends >= latest_ends:
                    raise ValidationFailed("提前结束必须提供更早的 ends_at")
                if starts != latest_starts:
                    raise ValidationFailed("提前结束不能改变 starts_at")
            elif revision.kind == "amend":
                if starts != latest_starts or ends != latest_ends:
                    raise ValidationFailed("amend 不改变影响窗口，请使用 extend 或 end_early")
                if (
                    revision.capacity_percent is None
                    and revision.evidence is None
                    and revision.appeal_deadline is None
                ):
                    raise ValidationFailed("amend 必须调整容量上限、证据或申诉截止")
            elif revision.kind == "revoke":
                if (
                    revision.starts_at is not None
                    or revision.ends_at is not None
                    or revision.capacity_percent is not None
                ):
                    raise ValidationFailed("revoke 不接受窗口或容量参数，容量上限将恢复为 100")
                capacity = HUNDRED
            if ends <= starts:
                raise ValidationFailed("ends_at 必须晚于 starts_at")
            if revision.appeal_deadline is not None and parse_utc(revision.appeal_deadline, "appeal_deadline") <= self.clock.now():
                raise ValidationFailed("appeal_deadline 必须晚于当前时间")
            _, version_no, evidence_sha256 = self._freeze_version(
                case_id,
                revision.kind,
                evidence,
                utc_text(starts),
                utc_text(ends),
                capacity,
                utc_text(parse_utc(deadline, "appeal_deadline")),
                actor_id,
            )
            if revision.kind == "revoke":
                self.connection.execute(
                    "UPDATE force_majeure_cases SET state='revoked',revision=revision+1 WHERE case_id=?",
                    (case_id,),
                )
            self._audit(
                "force_majeure",
                case_id,
                "force_majeure.version_frozen",
                actor_id,
                {"version_no": version_no, "kind": revision.kind, "evidence_sha256": evidence_sha256},
            )
        return {
            "case_id": case_id,
            "version_no": version_no,
            "kind": revision.kind,
            "evidence_sha256": evidence_sha256,
            "state": "revoked" if revision.kind == "revoke" else "open",
        }

    def _fm_candidates(self, case_id: str, route_id: str) -> list[dict[str, Any]]:
        """案件的削减候选：当前仍持有配额的提名，以及曾被本案件削减过的提名。"""
        candidates: dict[str, dict[str, Any]] = {}
        baseline_rows = self.connection.execute(
            "SELECT nomination_id,allocated_before FROM force_majeure_curtailments "
            "WHERE case_id=? AND item_id IN ("
            "  SELECT min(item_id) FROM force_majeure_curtailments WHERE case_id=? GROUP BY nomination_id"
            ")",
            (case_id, case_id),
        ).fetchall()
        for row in baseline_rows:
            nomination = self.connection.execute(
                "SELECT * FROM nominations WHERE nomination_id=?", (row["nomination_id"],)
            ).fetchone()
            if nomination["state"] in ("allocated", "cancelled"):
                candidates[row["nomination_id"]] = {
                    "nomination_id": row["nomination_id"],
                    "service_date": nomination["service_date"],
                    "priority": int(nomination["priority"]),
                    "submitted_at": nomination["submitted_at"],
                    "baseline": Decimal(row["allocated_before"]),
                    "current": Decimal(nomination["allocated_barrels"]),
                }
        allocated_rows = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND state='allocated'", (route_id,)
        ).fetchall()
        for nomination in allocated_rows:
            if nomination["nomination_id"] not in candidates:
                candidates[nomination["nomination_id"]] = {
                    "nomination_id": nomination["nomination_id"],
                    "service_date": nomination["service_date"],
                    "priority": int(nomination["priority"]),
                    "submitted_at": nomination["submitted_at"],
                    "baseline": Decimal(nomination["allocated_barrels"]),
                    "current": Decimal(nomination["allocated_barrels"]),
                }
        return sorted(
            candidates.values(),
            key=lambda item: (item["service_date"], item["priority"], item["submitted_at"], item["nomination_id"]),
        )

    def _fm_in_transit(self, route_id: str) -> dict[str, Decimal]:
        rows = self.connection.execute(
            "SELECT n.service_date,t.loaded_barrels FROM transfers t "
            "JOIN nominations n ON n.nomination_id=t.nomination_id "
            "WHERE n.route_id=? AND t.state='in_transit'",
            (route_id,),
        ).fetchall()
        totals: dict[str, Decimal] = {}
        for row in rows:
            totals[row["service_date"]] = totals.get(row["service_date"], ZERO) + Decimal(row["loaded_barrels"])
        return totals

    def _release_reservations(self, nomination_id: str, amount: Decimal) -> Decimal:
        """释放提名持有的库存预留并退回批次可用量，返回实际释放数量。必须在事务内调用。"""
        remaining = quantize_volume(amount)
        released = ZERO
        rows = self.connection.execute(
            "SELECT * FROM inventory_reservations WHERE nomination_id=? AND state='held' ORDER BY reservation_id",
            (nomination_id,),
        ).fetchall()
        for row in rows:
            if remaining <= ZERO:
                break
            held = Decimal(row["held_barrels"])
            take = min(held, remaining)
            remaining = quantize_volume(remaining - take)
            new_held = quantize_volume(held - take)
            cursor = self.connection.execute(
                "UPDATE inventory_reservations SET held_barrels=?,state=?,revision=revision+1 "
                "WHERE reservation_id=? AND revision=?",
                (
                    decimal_text(new_held),
                    "released" if new_held == ZERO else "held",
                    row["reservation_id"],
                    row["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict("库存预留已被并发修改")
            lot = self.connection.execute(
                "SELECT * FROM inventory_lots WHERE lot_id=?", (row["lot_id"],)
            ).fetchone()
            cursor = self.connection.execute(
                "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (
                    decimal_text(quantize_volume(Decimal(lot["available_barrels"]) + take)),
                    row["lot_id"],
                    lot["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict("库存批次已被并发修改")
            released += take
        return quantize_volume(released)

    def apply_force_majeure(self, actor_id: str, case_id: str, version_no: int) -> dict[str, Any]:
        self._require(actor_id, "force_majeure.process")
        with transaction(self.connection, immediate=True):
            case = self._fm_case(case_id)
            version = self.connection.execute(
                "SELECT * FROM force_majeure_versions WHERE case_id=? AND version_no=?",
                (case_id, version_no),
            ).fetchone()
            if version is None:
                raise NotFound("案件版本不存在")
            latest = self._fm_latest_version(case_id)
            if int(latest["version_no"]) != int(version_no):
                raise InvalidState("只能应用案件的最新版本")
            existing = self.connection.execute(
                "SELECT run_id,result_json FROM force_majeure_runs WHERE version_id=?",
                (version["version_id"],),
            ).fetchone()
            if existing is not None:
                return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
            route = self.connection.execute(
                "SELECT * FROM routes WHERE route_id=?", (case["route_id"],)
            ).fetchone()
            candidates = self._fm_candidates(case_id, route["route_id"])
            start_date = version["starts_at"][:10]
            end_date = version["ends_at"][:10]
            in_transit = self._fm_in_transit(route["route_id"])
            plan: list[dict[str, Any]] = []
            dates: dict[str, dict[str, Any]] = {}
            for service_date in sorted({item["service_date"] for item in candidates}):
                in_window = start_date <= service_date <= end_date
                group = [item for item in candidates if item["service_date"] == service_date]
                if in_window:
                    ceiling: Decimal | None = self._capacity_for_date(route, service_date)
                    targets = {
                        row["nomination_id"]: Decimal(row["allocated_barrels"])
                        for row in allocate_capacity(
                            ceiling,
                            [
                                AllocationRequest(
                                    item["nomination_id"], item["baseline"], item["priority"], item["submitted_at"]
                                )
                                for item in group
                            ],
                        )
                    }
                else:
                    ceiling = None
                    targets = {item["nomination_id"]: item["baseline"] for item in group}
                before_total = sum((item["current"] for item in group), ZERO)
                after_total = sum(targets.values(), ZERO)
                curtailed = ZERO
                restored = ZERO
                for item in group:
                    target = quantize_volume(targets[item["nomination_id"]])
                    delta = target - item["current"]
                    if delta < ZERO:
                        curtailed += -delta
                    elif delta > ZERO:
                        restored += delta
                    plan.append({**item, "target": target})
                dates[service_date] = {
                    "service_date": service_date,
                    "in_window": in_window,
                    "ceiling": None if ceiling is None else decimal_text(ceiling),
                    "allocated_before": decimal_text(quantize_volume(before_total)),
                    "allocated_after": decimal_text(quantize_volume(after_total)),
                    "curtailed": decimal_text(quantize_volume(curtailed)),
                    "restored": decimal_text(quantize_volume(restored)),
                    "in_transit": decimal_text(quantize_volume(in_transit.get(service_date, ZERO))),
                }
            items: list[dict[str, Any]] = []
            released_by_date: dict[str, Decimal] = {}
            for entry in plan:
                target = entry["target"]
                current = entry["current"]
                if target == current:
                    continue
                released = ZERO
                if target < current:
                    released = self._release_reservations(entry["nomination_id"], current - target)
                self.connection.execute(
                    "UPDATE nominations SET allocated_barrels=?,state=?,revision=revision+1 WHERE nomination_id=?",
                    (decimal_text(target), "cancelled" if target == ZERO else "allocated", entry["nomination_id"]),
                )
                items.append({
                    "nomination_id": entry["nomination_id"],
                    "service_date": entry["service_date"],
                    "allocated_before": decimal_text(quantize_volume(current)),
                    "allocated_after": decimal_text(target),
                    "released_barrels": decimal_text(released),
                })
                released_by_date[entry["service_date"]] = released_by_date.get(entry["service_date"], ZERO) + released
            totals = {"before": ZERO, "after": ZERO, "curtailed": ZERO, "restored": ZERO, "released": ZERO, "transit": ZERO}
            date_rows = []
            for service_date in sorted(dates):
                row = dates[service_date]
                released = released_by_date.get(service_date, ZERO)
                row["released_inventory"] = decimal_text(quantize_volume(released))
                date_rows.append(row)
                totals["before"] += Decimal(row["allocated_before"])
                totals["after"] += Decimal(row["allocated_after"])
                totals["curtailed"] += Decimal(row["curtailed"])
                totals["restored"] += Decimal(row["restored"])
                totals["released"] += released
                totals["transit"] += Decimal(row["in_transit"])
            result = {
                "case_id": case_id,
                "version_no": int(version_no),
                "dates": date_rows,
                "totals": {
                    "allocated_before": decimal_text(quantize_volume(totals["before"])),
                    "allocated_after": decimal_text(quantize_volume(totals["after"])),
                    "curtailed": decimal_text(quantize_volume(totals["curtailed"])),
                    "restored": decimal_text(quantize_volume(totals["restored"])),
                    "released_inventory": decimal_text(quantize_volume(totals["released"])),
                    "in_transit": decimal_text(quantize_volume(totals["transit"])),
                },
                "items": items,
            }
            input_sha256 = digest({
                "case_id": case_id,
                "version_id": version["version_id"],
                "candidates": [
                    {key: (decimal_text(value) if isinstance(value, Decimal) else value) for key, value in entry.items()}
                    for entry in plan
                ],
            })
            cursor = self.connection.execute(
                "INSERT INTO force_majeure_runs(case_id,version_id,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (case_id, version["version_id"], input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            for item in items:
                self.connection.execute(
                    "INSERT INTO force_majeure_curtailments(run_id,case_id,nomination_id,service_date,"
                    "allocated_before,allocated_after,released_barrels,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        case_id,
                        item["nomination_id"],
                        item["service_date"],
                        item["allocated_before"],
                        item["allocated_after"],
                        item["released_barrels"],
                        self._now(),
                    ),
                )
            self._audit(
                "force_majeure",
                case_id,
                "force_majeure.applied",
                actor_id,
                {
                    "run_id": run_id,
                    "version_no": int(version_no),
                    "curtailed": result["totals"]["curtailed"],
                    "restored": result["totals"]["restored"],
                    "released_inventory": result["totals"]["released_inventory"],
                },
            )
            return {"run_id": run_id, **result, "replayed": False}

    def file_appeal(self, actor_id: str, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "appeal.file")
        appeal = AppealRequest.from_dict(raw)
        case = self._fm_case(case_id)
        if case["state"] != "open":
            raise InvalidState("案件已撤销，不能申诉")
        nomination = self.connection.execute(
            "SELECT * FROM nominations WHERE nomination_id=?", (appeal.nomination_id,)
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["route_id"] != case["route_id"]:
            raise ValidationFailed("提名不属于案件线路")
        items = self.connection.execute(
            "SELECT allocated_before,allocated_after FROM force_majeure_curtailments "
            "WHERE case_id=? AND nomination_id=?",
            (case_id, appeal.nomination_id),
        ).fetchall()
        if not any(Decimal(row["allocated_after"]) < Decimal(row["allocated_before"]) for row in items):
            raise InvalidState("提名未被该案件削减，不能申诉")
        latest = self._fm_latest_version(case_id)
        if parse_utc(latest["appeal_deadline"], "appeal_deadline") < self.clock.now():
            raise InvalidState("申诉截止已过")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO force_majeure_appeals(appeal_id,case_id,nomination_id,shipper_id,reason,"
                    "requested_barrels,filed_by,filed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        appeal.appeal_id,
                        case_id,
                        appeal.nomination_id,
                        nomination["shipper_id"],
                        appeal.reason,
                        decimal_text(quantize_volume(appeal.requested_barrels)),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "appeal",
                    appeal.appeal_id,
                    "appeal.filed",
                    actor_id,
                    {"case_id": case_id, "nomination_id": appeal.nomination_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该提名已提交过申诉") from exc
        return {
            "appeal_id": appeal.appeal_id,
            "case_id": case_id,
            "nomination_id": appeal.nomination_id,
            "state": "filed",
        }

    def decide_appeal(self, actor_id: str, appeal_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "appeal.decide")
        decision = AppealDecision.from_dict(raw)
        with transaction(self.connection, immediate=True):
            appeal = self.connection.execute(
                "SELECT * FROM force_majeure_appeals WHERE appeal_id=?", (appeal_id,)
            ).fetchone()
            if appeal is None:
                raise NotFound("申诉不存在")
            if appeal["state"] != "filed":
                raise InvalidState("申诉已处理，不能重复决定")
            case = self._fm_case(appeal["case_id"])
            nomination = self.connection.execute(
                "SELECT * FROM nominations WHERE nomination_id=?", (appeal["nomination_id"],)
            ).fetchone()
            route = self.connection.execute(
                "SELECT * FROM routes WHERE route_id=?", (case["route_id"],)
            ).fetchone()
            baseline_row = self.connection.execute(
                "SELECT allocated_before FROM force_majeure_curtailments WHERE case_id=? AND nomination_id=? "
                "ORDER BY item_id LIMIT 1",
                (appeal["case_id"], appeal["nomination_id"]),
            ).fetchone()
            baseline = Decimal(baseline_row["allocated_before"])
            current = Decimal(nomination["allocated_barrels"])
            deficit = max(ZERO, baseline - current)
            ceiling = self._capacity_for_date(route, nomination["service_date"])
            peers = self.connection.execute(
                "SELECT allocated_barrels FROM nominations WHERE route_id=? AND service_date=? AND state='allocated'",
                (case["route_id"], nomination["service_date"]),
            ).fetchall()
            total_allocated = sum((Decimal(row["allocated_barrels"]) for row in peers), ZERO)
            headroom = max(ZERO, ceiling - total_allocated)
            requested = Decimal(appeal["requested_barrels"])
            if decision.decision == "rejected":
                granted = ZERO
            elif decision.decision == "upheld":
                granted = min(requested, deficit, headroom)
            else:
                granted = min(decision.granted_barrels, requested, deficit, headroom)
            granted = quantize_volume(granted)
            cursor = self.connection.execute(
                "UPDATE force_majeure_appeals SET state=?,granted_barrels=?,decision_note=?,decided_by=?,decided_at=? "
                "WHERE appeal_id=? AND state='filed'",
                (decision.decision, decimal_text(granted), decision.note, actor_id, self._now(), appeal_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("申诉已处理，不能重复决定")
            if granted > ZERO:
                self.connection.execute(
                    "UPDATE nominations SET allocated_barrels=?,state='allocated',revision=revision+1 "
                    "WHERE nomination_id=?",
                    (decimal_text(quantize_volume(current + granted)), appeal["nomination_id"]),
                )
            self._audit(
                "appeal",
                appeal_id,
                "appeal.decided",
                actor_id,
                {
                    "case_id": appeal["case_id"],
                    "nomination_id": appeal["nomination_id"],
                    "decision": decision.decision,
                    "granted_barrels": decimal_text(granted),
                },
            )
        return {
            "appeal_id": appeal_id,
            "state": decision.decision,
            "granted_barrels": decimal_text(granted),
            "remaining_deficit": decimal_text(quantize_volume(deficit - granted)),
            "headroom_barrels": decimal_text(quantize_volume(headroom - granted)),
        }

    def get_force_majeure(self, actor_id: str, case_id: str) -> dict[str, Any]:
        self._require(actor_id, "force_majeure.read")
        case = self._fm_case(case_id)
        versions = self.connection.execute(
            "SELECT * FROM force_majeure_versions WHERE case_id=? ORDER BY version_no", (case_id,)
        ).fetchall()
        runs = self.connection.execute(
            "SELECT r.*,v.version_no FROM force_majeure_runs r "
            "JOIN force_majeure_versions v ON v.version_id=r.version_id "
            "WHERE r.case_id=? ORDER BY r.run_id",
            (case_id,),
        ).fetchall()
        applied = {row["version_no"] for row in runs}
        appeals = self.connection.execute(
            "SELECT * FROM force_majeure_appeals WHERE case_id=? ORDER BY filed_at,appeal_id", (case_id,)
        ).fetchall()
        return {
            "case_id": case_id,
            "route_id": case["route_id"],
            "state": case["state"],
            "opened_by": case["opened_by"],
            "opened_at": case["opened_at"],
            "versions": [
                {**self._fm_version_dict(row), "applied": row["version_no"] in applied}
                for row in versions
            ],
            "runs": [
                {
                    "run_id": row["run_id"],
                    "version_no": row["version_no"],
                    "applied_by": row["created_by"],
                    "applied_at": row["created_at"],
                    **json.loads(row["result_json"]),
                }
                for row in runs
            ],
            "appeals": [dict(row) for row in appeals],
        }

    def nomination_trace(self, actor_id: str, nomination_id: str) -> dict[str, Any]:
        self._require(actor_id, "force_majeure.read")
        nomination = self.connection.execute(
            "SELECT * FROM nominations WHERE nomination_id=?", (nomination_id,)
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        case_rows = self.connection.execute(
            "SELECT DISTINCT case_id FROM force_majeure_curtailments WHERE nomination_id=? ORDER BY case_id",
            (nomination_id,),
        ).fetchall()
        cases = []
        for case_row in case_rows:
            case_id = case_row["case_id"]
            case = self._fm_case(case_id)
            items = self.connection.execute(
                "SELECT c.*,v.version_no,r.created_at AS applied_at FROM force_majeure_curtailments c "
                "JOIN force_majeure_runs r ON r.run_id=c.run_id "
                "JOIN force_majeure_versions v ON v.version_id=r.version_id "
                "WHERE c.case_id=? AND c.nomination_id=? ORDER BY c.item_id",
                (case_id, nomination_id),
            ).fetchall()
            curtailments = []
            restorations = []
            for item in items:
                entry = {
                    "version_no": item["version_no"],
                    "allocated_before": item["allocated_before"],
                    "allocated_after": item["allocated_after"],
                    "released_barrels": item["released_barrels"],
                    "applied_at": item["applied_at"],
                }
                if Decimal(item["allocated_after"]) < Decimal(item["allocated_before"]):
                    curtailments.append(entry)
                elif Decimal(item["allocated_after"]) > Decimal(item["allocated_before"]):
                    restorations.append(entry)
            appeal = self.connection.execute(
                "SELECT * FROM force_majeure_appeals WHERE case_id=? AND nomination_id=?",
                (case_id, nomination_id),
            ).fetchone()
            order_rows = self.connection.execute(
                "SELECT nomination_id,shipper_id,priority,submitted_at,allocated_barrels,state FROM nominations "
                "WHERE route_id=? AND service_date=? ORDER BY priority,submitted_at,nomination_id",
                (case["route_id"], nomination["service_date"]),
            ).fetchall()
            cases.append({
                "case_id": case_id,
                "case_state": case["state"],
                "latest_version": self._fm_version_dict(self._fm_latest_version(case_id)),
                "contract_order": [
                    {"position": index + 1, **dict(row)} for index, row in enumerate(order_rows)
                ],
                "curtailments": curtailments,
                "restorations": restorations,
                "appeal": None if appeal is None else dict(appeal),
            })
        return {"nomination": dict(nomination), "force_majeure": cases}

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
