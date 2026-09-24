"""油气供应领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed
from .planning import canonical_json


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
CRUDE_GRADES = {"BRENT", "WTI", "DUBAI", "ESPO", "URAL", "CUSTOM"}
PRODUCTS = {"crude", "gasoline-92", "gasoline-95", "diesel", "jet-fuel", "condensate"}
ROUTE_KINDS = {"pipeline", "terminal", "refinery", "storage", "truck-rack"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    price_index: str
    trade_date: str
    close_usd: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        price_index = required_text(raw.get("price_index"), "price_index", 16).upper()
        if price_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("price_index 必须是 BRENT、WTI、DUBAI、ESPO 或 URAL")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            price_index=price_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_usd=decimal_value(raw.get("close_usd"), "close_usd", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_barrels: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_barrels=decimal_value(
                raw.get("capacity_barrels"), "capacity_barrels", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("线路起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_barrels: Decimal
    unit_cost_usd: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
            unit_cost_usd=decimal_value(
                raw.get("unit_cost_usd"), "unit_cost_usd", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_barrels: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_barrels=decimal_value(
                raw.get("requested_barrels"), "requested_barrels", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    price_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            price_index_drop_percent=decimal_value(
                raw.get("price_index_drop_percent", 0),
                "price_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )


REVISION_KINDS = {"extend", "end_early", "amend", "revoke"}
APPEAL_DECISIONS = {"upheld", "partially_upheld", "rejected"}


def _utc_text_field(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


def _evidence_field(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValidationFailed("evidence 必须是非空 JSON 对象")
    try:
        canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("evidence 必须可以序列化为 JSON") from exc
    return value


def _capacity_percent_field(value: object) -> Decimal:
    return decimal_value(
        value, "capacity_percent", minimum=Decimal("0"), maximum=Decimal("100")
    )


@dataclass(frozen=True, slots=True)
class ForceMajeureDeclaration:
    case_id: str
    route_id: str
    evidence: Mapping[str, Any]
    starts_at: str
    ends_at: str
    capacity_percent: Decimal
    appeal_deadline: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ForceMajeureDeclaration":
        return cls(
            case_id=identifier(raw.get("case_id"), "case_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            evidence=_evidence_field(raw.get("evidence")),
            starts_at=_utc_text_field(raw.get("starts_at"), "starts_at"),
            ends_at=_utc_text_field(raw.get("ends_at"), "ends_at"),
            capacity_percent=_capacity_percent_field(raw.get("capacity_percent")),
            appeal_deadline=_utc_text_field(raw.get("appeal_deadline"), "appeal_deadline"),
        )


@dataclass(frozen=True, slots=True)
class ForceMajeureRevision:
    kind: str
    evidence: Mapping[str, Any] | None
    starts_at: str | None
    ends_at: str | None
    capacity_percent: Decimal | None
    appeal_deadline: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ForceMajeureRevision":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in REVISION_KINDS:
            raise ValidationFailed("kind 必须是 extend、end_early、amend 或 revoke")
        evidence = raw.get("evidence")
        capacity = raw.get("capacity_percent")
        return cls(
            kind=kind,
            evidence=None if evidence is None else _evidence_field(evidence),
            starts_at=(
                None if raw.get("starts_at") is None else _utc_text_field(raw.get("starts_at"), "starts_at")
            ),
            ends_at=(
                None if raw.get("ends_at") is None else _utc_text_field(raw.get("ends_at"), "ends_at")
            ),
            capacity_percent=None if capacity is None else _capacity_percent_field(capacity),
            appeal_deadline=(
                None
                if raw.get("appeal_deadline") is None
                else _utc_text_field(raw.get("appeal_deadline"), "appeal_deadline")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    reservation_id: str
    lot_id: str
    nomination_id: str
    quantity_barrels: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservationRequest":
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class AppealRequest:
    appeal_id: str
    nomination_id: str
    reason: str
    requested_barrels: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AppealRequest":
        return cls(
            appeal_id=identifier(raw.get("appeal_id"), "appeal_id"),
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            reason=required_text(raw.get("reason"), "reason", 512),
            requested_barrels=decimal_value(
                raw.get("requested_barrels"), "requested_barrels", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class AppealDecision:
    decision: str
    granted_barrels: Decimal | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AppealDecision":
        decision = required_text(raw.get("decision"), "decision", 24)
        if decision not in APPEAL_DECISIONS:
            raise ValidationFailed("decision 必须是 upheld、partially_upheld 或 rejected")
        granted = raw.get("granted_barrels")
        parsed = None if granted is None else decimal_value(
            granted, "granted_barrels", minimum=Decimal("0.001")
        )
        if decision == "partially_upheld" and parsed is None:
            raise ValidationFailed("部分核准必须提供 granted_barrels")
        note = raw.get("note", "")
        return cls(decision=decision, granted_barrels=parsed, note=required_text(note, "note", 512) if note else "")
