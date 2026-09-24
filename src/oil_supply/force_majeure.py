"""不可抗力案件的确定性削减、恢复与数量守恒计算。

版本目标值只依赖四类冻结输入：版本窗口、容量上限、宣布时的
配额基线、以及已经发运（在途或交付）的不可变数量。任何后继版本
都用同一规则重新推导，因此重复处理同一版本不会产生额外释放。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from .planning import ZERO, decimal_text, effective_capacity, quantize_volume


@dataclass(frozen=True, slots=True)
class NominationBaseline:
    """宣布案件时冻结的单条提名配额基线。"""

    nomination_id: str
    shipper_id: str
    service_date: str
    priority: int
    submitted_at: str
    baseline_barrels: Decimal
    shipped_barrels: Decimal

    @property
    def unshipped_headroom(self) -> Decimal:
        """在途数量固定，基线之内尚未发运的部分才可削减或恢复。"""
        return quantize_volume(max(ZERO, self.baseline_barrels - self.shipped_barrels))

    @property
    def contract_key(self) -> tuple[int, str, str]:
        return (self.priority, self.submitted_at, self.nomination_id)


def service_dates_window(starts_at_iso: str, ends_at_iso: str) -> list[str]:
    """UTC 影响窗口覆盖的全部服务日期，含首尾。"""
    start = date.fromisoformat(starts_at_iso[:10])
    end = date.fromisoformat(ends_at_iso[:10])
    if end < start:
        raise ValueError("影响窗口结束不能早于开始")
    days = (end - start).days
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days + 1)]


def contract_ranking(rows: Sequence[NominationBaseline]) -> dict[str, int]:
    """同一服务日期内按优先级、提交时间和编号确定的合同排序。"""
    ranked: dict[str, int] = {}
    by_date: dict[str, list[NominationBaseline]] = {}
    for row in rows:
        by_date.setdefault(row.service_date, []).append(row)
    for peers in by_date.values():
        for position, row in enumerate(sorted(peers, key=lambda item: item.contract_key), start=1):
            ranked[row.nomination_id] = position
    return ranked


def curtailment_targets(
    *,
    nominal_capacity: Decimal,
    capacity_percent: Decimal,
    rows: Sequence[NominationBaseline],
) -> dict[str, Decimal]:
    """按合同排序把未发运配额削减到容量上限以内。

    已发运数量作为不可变下限优先占用容量；每条提名的目标等于
    在途数量加上按合同排序分到的未发运容量，因此在途货物绝不被追溯。
    """
    cap = effective_capacity(nominal_capacity, [capacity_percent])
    by_date: dict[str, list[NominationBaseline]] = {}
    for row in rows:
        by_date.setdefault(row.service_date, []).append(row)
    targets: dict[str, Decimal] = {}
    for peers in by_date.values():
        shipped_total = quantize_volume(sum((row.shipped_barrels for row in peers), ZERO))
        remaining = quantize_volume(max(ZERO, cap - shipped_total))
        for row in sorted(peers, key=lambda item: item.contract_key):
            added = quantize_volume(min(row.unshipped_headroom, max(ZERO, remaining)))
            remaining = quantize_volume(remaining - added)
            targets[row.nomination_id] = quantize_volume(row.shipped_barrels + added)
    return targets


def conservation_ledger(
    *,
    opening: Mapping[str, Decimal],
    targets: Mapping[str, Decimal],
    baseline: Mapping[str, Decimal],
    shipped: Mapping[str, Decimal],
    reservation_before: Mapping[str, Decimal],
) -> dict[str, object]:
    """汇总版本前后数量守恒差异并校验不变量。

    - closing = opening - released + restored，差额必须为零；
    - 已发运数量是预留与分配之外的固定下限，不能被削减；
    - 处理后数量不得超过宣布时冻结的基线。
    """
    opening_total = ZERO
    closing_total = ZERO
    released_total = ZERO
    restored_total = ZERO
    reserved_opening_total = ZERO
    reserved_closing_total = ZERO
    reserved_released_total = ZERO
    reserved_restored_total = ZERO
    for nomination_id, before in opening.items():
        before = quantize_volume(before)
        after = quantize_volume(targets.get(nomination_id, before))
        base = quantize_volume(baseline.get(nomination_id, before))
        on_the_way = quantize_volume(shipped.get(nomination_id, ZERO))
        held_before = quantize_volume(reservation_before.get(nomination_id, ZERO))
        if before < on_the_way or after < on_the_way:
            raise ValueError("版本数量不能低于已发运数量")
        if before != on_the_way + held_before:
            raise ValueError("版本前预留与在途数量之和不等于分配")
        if after > base:
            raise ValueError("版本目标数量超出配额基线")
        held_after = quantize_volume(after - on_the_way)
        released = quantize_volume(max(ZERO, before - after))
        restored = quantize_volume(max(ZERO, after - before))
        reserve_released = quantize_volume(max(ZERO, held_before - held_after))
        reserve_restored = quantize_volume(max(ZERO, held_after - held_before))
        opening_total += before
        closing_total += after
        released_total += released
        restored_total += restored
        reserved_opening_total += held_before
        reserved_closing_total += held_after
        reserved_released_total += reserve_released
        reserved_restored_total += reserve_restored
    variance = quantize_volume(closing_total - (opening_total - released_total + restored_total))
    reserve_variance = quantize_volume(
        reserved_closing_total
        - (reserved_opening_total - reserved_released_total + reserved_restored_total)
    )
    return {
        "allocation": {
            "opening_barrels": decimal_text(quantize_volume(opening_total)),
            "released_barrels": decimal_text(quantize_volume(released_total)),
            "restored_barrels": decimal_text(quantize_volume(restored_total)),
            "closing_barrels": decimal_text(quantize_volume(closing_total)),
            "variance_barrels": decimal_text(variance),
            "conserved": variance == ZERO,
        },
        "reservations": {
            "opening_barrels": decimal_text(quantize_volume(reserved_opening_total)),
            "released_barrels": decimal_text(quantize_volume(reserved_released_total)),
            "restored_barrels": decimal_text(quantize_volume(reserved_restored_total)),
            "closing_barrels": decimal_text(quantize_volume(reserved_closing_total)),
            "variance_barrels": decimal_text(reserve_variance),
            "conserved": reserve_variance == ZERO,
        },
    }
