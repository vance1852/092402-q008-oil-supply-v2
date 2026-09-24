from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from oil_supply.force_majeure import (
    NominationBaseline,
    conservation_ledger,
    contract_ranking,
    curtailment_targets,
    service_dates_window,
)
from oil_supply.service import SupplyService


def baseline(
    nomination_id: str,
    service_date: str = "2026-09-25",
    priority: int = 10,
    amount: str = "60",
    shipped: str = "0",
    shipper: str | None = None,
) -> NominationBaseline:
    return NominationBaseline(
        nomination_id=nomination_id,
        shipper_id=shipper or nomination_id,
        service_date=service_date,
        priority=priority,
        submitted_at=f"2026-09-24T0{priority}:00:00Z",
        baseline_barrels=Decimal(amount),
        shipped_barrels=Decimal(shipped),
    )


class CurtailmentCalculationTests(unittest.TestCase):
    def test_window_covers_each_service_date_inclusive(self) -> None:
        self.assertEqual(
            service_dates_window("2026-09-25T00:00:00Z", "2026-09-27T23:59:59Z"),
            ["2026-09-25", "2026-09-26", "2026-09-27"],
        )

    def test_contract_ranking_is_deterministic_per_date(self) -> None:
        rows = [baseline("later", priority=20), baseline("earlier", priority=10)]
        ranks = contract_ranking(rows)
        self.assertEqual(ranks, {"later": 2, "earlier": 1})

    def test_shipped_volume_is_a_floor_and_never_touched(self) -> None:
        targets = curtailment_targets(
            nominal_capacity=Decimal("100"),
            capacity_percent=Decimal("50"),
            rows=[baseline("in-transit", priority=10, amount="60", shipped="60"),
                  baseline("waiting", priority=20, amount="40", shipped="0")],
        )
        self.assertEqual(targets["in-transit"], Decimal("60"))
        self.assertEqual(targets["waiting"], Decimal("0"))

    def test_conservation_ledger_balances_release_and_restore(self) -> None:
        ledger = conservation_ledger(
            opening={"a": Decimal("60"), "b": Decimal("40")},
            targets={"a": Decimal("60"), "b": Decimal("0")},
            baseline={"a": Decimal("60"), "b": Decimal("40")},
            shipped={"a": Decimal("60"), "b": Decimal("0")},
            reservation_before={"a": Decimal("0"), "b": Decimal("40")},
        )
        self.assertTrue(ledger["allocation"]["conserved"])
        self.assertEqual(ledger["allocation"]["released_barrels"], "40.000")
        self.assertTrue(ledger["reservations"]["conserved"])
        self.assertEqual(ledger["reservations"]["released_barrels"], "40.000")

    def test_conservation_ledger_rejects_moving_in_transit_volume(self) -> None:
        with self.assertRaises(ValueError):
            conservation_ledger(
                opening={"a": Decimal("60")},
                targets={"a": Decimal("50")},
                baseline={"a": Decimal("60")},
                shipped={"a": Decimal("60")},
                reservation_before={"a": Decimal("0")},
            )


class ForceMajeureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("dispatch", "dispatcher"),
            ("risk", "risk"),
            ("audit", "auditor"),
            ("ship-a", "shipper"),
            ("ship-b", "shipper"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "fac", "name": "油田", "kind": "storage", "timezone": "UTC", "capacity_barrels": "1000000"})
        self.service.create_facility("plan", {"facility_id": "term", "name": "终端", "kind": "terminal", "timezone": "UTC", "capacity_barrels": "1000000"})
        self.service.create_route("plan", {"route_id": "pipe", "origin_id": "fac", "destination_id": "term", "product": "crude", "daily_capacity": "100", "loss_basis_points": 0, "transit_hours": 12})
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "fac", "product": "crude", "grade": "BRENT", "quantity_barrels": "200", "unit_cost_usd": "90", "received_at": "2026-09-24T00:00:00Z"})

    def tearDown(self) -> None:
        self.connection.close()

    def _two_nominations_allocated(self) -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-a", "route_id": "pipe", "shipper_id": "ship-a", "service_date": "2026-09-25", "requested_barrels": "60", "priority": 10, "idempotency_key": "key-a"})
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-b", "route_id": "pipe", "shipper_id": "ship-b", "service_date": "2026-09-25", "requested_barrels": "60", "priority": 20, "idempotency_key": "key-b"})
        self.service.allocate("dispatch", "pipe", "2026-09-25")

    def _declare(self, capacity: str = "50") -> dict:
        return self.service.declare_force_majeure("risk", {
            "case_id": "case-1",
            "route_id": "pipe",
            "title": "管道穿孔",
            "impact_starts_at": "2026-09-25T00:00:00Z",
            "impact_ends_at": "2026-09-27T23:59:59Z",
            "capacity_percent": capacity,
            "evidence": {"photo": "sha256:evidence", "scada_tag": "PT-42"},
            "appeal_deadline": "2026-09-24T20:00:00Z",
        })

    def test_only_risk_can_declare(self) -> None:
        self._two_nominations_allocated()
        with self.assertRaises(Forbidden):
            self.service.declare_force_majeure("dispatch", {
                "case_id": "case-x", "route_id": "pipe", "title": "x",
                "impact_starts_at": "2026-09-25T00:00:00Z",
                "impact_ends_at": "2026-09-27T23:59:59Z",
                "capacity_percent": "50", "evidence": {"a": 1},
                "appeal_deadline": "2026-09-24T20:00:00Z",
            })

    def test_curtailment_freezes_in_transit_and_returns_reservation(self) -> None:
        self._two_nominations_allocated()
        self.service.dispatch_transfer("dispatch", "tr-1", "nom-a", "lot-1", 2)
        physical_before = Decimal(self.service.inventory_lot("lot-1")["available_barrels"])
        declared = self._declare()
        by_id = {row["nomination_id"]: row for row in declared["curtailments"]}
        self.assertEqual(by_id["nom-a"]["allocated_after"], "60.000")
        self.assertEqual(by_id["nom-a"]["in_transit_barrels"], "60.000")
        self.assertEqual(by_id["nom-b"]["allocated_after"], "0.000")
        self.assertEqual(by_id["nom-b"]["curtail_barrels"], "40.000")
        self.assertEqual(by_id["nom-a"]["contract_rank"], 1)
        self.assertEqual(by_id["nom-b"]["contract_rank"], 2)
        self.assertTrue(declared["conservation"]["allocation"]["conserved"])
        # 在途实物已经离开储罐，数量与可用库存都不因削减再变化；
        # 未发运提名的预留被释放，净可用库存回升。
        self.assertEqual(self.service.inventory_lot("lot-1")["available_barrels"],
                         format(physical_before, "f"))
        self.assertEqual(self.service.inventory_summary("fac", "crude")["reserved_barrels"], "0.000")
        nom_b = self.connection.execute("SELECT state FROM nominations WHERE nomination_id='nom-b'").fetchone()
        self.assertEqual(nom_b["state"], "curtailed")

    def test_declaration_replay_is_idempotent_and_does_not_release_twice(self) -> None:
        self._two_nominations_allocated()
        first = self._declare()
        second = self._declare()
        self.assertEqual(first["version"], second["version"])
        self.assertTrue(second["replayed"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM force_majeure_case_versions").fetchone()["c"],
            1,
        )

    def test_extension_creates_successor_version_and_replays(self) -> None:
        self._two_nominations_allocated()
        self._declare()
        extended = self.service.extend_force_majeure(
            "risk", "case-1", "2026-09-29T23:59:59Z", "50", {"photo": "new"},
            "抢修延期", "extend-1",
        )
        self.assertEqual(extended["version"], 2)
        self.assertEqual(extended["change_kind"], "extended")
        replay = self.service.extend_force_majeure(
            "risk", "case-1", "2026-09-29T23:59:59Z", "50", {"photo": "new"},
            "抢修延期", "extend-1",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM force_majeure_case_versions").fetchone()["c"],
            2,
        )

    def test_extension_must_lengthen_window(self) -> None:
        self._two_nominations_allocated()
        self._declare()
        with self.assertRaises(ValidationFailed):
            self.service.extend_force_majeure(
                "risk", "case-1", "2026-09-26T00:00:00Z", "50", {"a": 1}, "短", "extend-x",
            )

    def test_appeal_accepted_once_before_deadline_only_for_own_share(self) -> None:
        self._two_nominations_allocated()
        self._declare()
        payload = {"appeal_id": "appeal-1", "case_id": "case-1", "shipper_id": "ship-b", "requested_barrels": "10", "reason": "合同优先", "idempotency_key": "appeal-key-1"}
        first = self.service.submit_appeal("ship-b", payload)
        self.assertEqual(first["state"], "submitted")
        # 同键同内容重放返回原申诉。
        self.assertEqual(self.service.submit_appeal("ship-b", payload)["appeal_id"], "appeal-1")
        # 每家托运方只能申诉一次。
        with self.assertRaises(Conflict):
            self.service.submit_appeal("ship-b", {**payload, "appeal_id": "appeal-2", "idempotency_key": "appeal-key-2"})
        # 不能替别家申诉。
        with self.assertRaises(Forbidden):
            self.service.submit_appeal("ship-a", {**payload, "appeal_id": "appeal-3", "shipper_id": "ship-b", "idempotency_key": "appeal-key-3"})
        # 截止后不接受。
        self.clock.advance(hours=13)
        with self.assertRaises(InvalidState):
            self.service.submit_appeal("ship-a", {"appeal_id": "appeal-4", "case_id": "case-1", "shipper_id": "ship-a", "requested_barrels": "5", "reason": "晚", "idempotency_key": "appeal-key-4"})

    def test_ruling_without_capacity_headroom_is_rejected(self) -> None:
        self._two_nominations_allocated()
        self.service.dispatch_transfer("dispatch", "tr-1", "nom-a", "lot-1", 2)
        self._declare()
        self.service.submit_appeal("ship-b", {"appeal_id": "appeal-1", "case_id": "case-1", "shipper_id": "ship-b", "requested_barrels": "10", "reason": "x", "idempotency_key": "ak"})
        # 上限 50% 已被在途 60 占满，无余量，不能暗改他人。
        with self.assertRaises(InvalidState):
            self.service.rule_appeal("risk", "appeal-1", True, "10", "rule-1")

    def test_ruling_restores_appellant_and_freezes_other_shares(self) -> None:
        self._two_nominations_allocated()
        self.service.dispatch_transfer("dispatch", "tr-1", "nom-a", "lot-1", 2)
        self._declare()
        self.service.submit_appeal("ship-b", {"appeal_id": "appeal-1", "case_id": "case-1", "shipper_id": "ship-b", "requested_barrels": "10", "reason": "x", "idempotency_key": "ak"})
        result = self.service.rule_appeal("risk", "appeal-1", True, "10", "rule-1", capacity_percent="70")
        rows = {row["nomination_id"]: row for row in result["version"]["curtailments"]}
        self.assertEqual(rows["nom-a"]["allocated_before"], rows["nom-a"]["allocated_after"])
        self.assertEqual(rows["nom-b"]["allocated_after"], "10.000")
        self.assertEqual(rows["nom-b"]["restored_barrels"], "10.000")
        self.assertTrue(result["version"]["conservation"]["allocation"]["conserved"])
        self.assertEqual(result["appeal"]["state"], "granted")
        self.assertEqual(result["appeal"]["granted_barrels"], "10.000")

    def test_denied_appeal_changes_nothing_and_is_idempotent(self) -> None:
        self._two_nominations_allocated()
        self._declare()
        self.service.submit_appeal("ship-b", {"appeal_id": "appeal-1", "case_id": "case-1", "shipper_id": "ship-b", "requested_barrels": "10", "reason": "x", "idempotency_key": "ak"})
        denied = self.service.rule_appeal("risk", "appeal-1", False, "0", "rule-1")
        rows = {row["nomination_id"]: row for row in denied["version"]["curtailments"]}
        self.assertEqual(rows["nom-a"]["allocated_before"], rows["nom-a"]["allocated_after"])
        self.assertEqual(rows["nom-b"]["allocated_before"], rows["nom-b"]["allocated_after"])
        replay = self.service.rule_appeal("risk", "appeal-1", False, "0", "rule-1")
        self.assertTrue(replay["version"]["replayed"])

    def test_early_end_restores_baseline_but_keeps_shipped_volume(self) -> None:
        self._two_nominations_allocated()
        self.service.dispatch_transfer("dispatch", "tr-1", "nom-a", "lot-1", 2)
        self._declare()
        ended = self.service.end_force_majeure("risk", "case-1", "2026-09-26T00:00:00Z", "提前复输", "end-1")
        rows = {row["nomination_id"]: row for row in ended["curtailments"]}
        self.assertEqual(ended["state"], "ended")
        self.assertEqual(rows["nom-a"]["allocated_after"], "60.000")
        self.assertEqual(rows["nom-a"]["in_transit_barrels"], "60.000")
        self.assertEqual(rows["nom-b"]["allocated_after"], "40.000")
        self.assertEqual(rows["nom-b"]["restored_barrels"], "40.000")
        self.assertTrue(ended["conservation"]["reservations"]["conserved"])
        self.assertEqual(self.service.inventory_summary("fac", "crude")["reserved_barrels"], "40.000")
        with self.assertRaises(InvalidState):
            self.service.end_force_majeure("risk", "case-1", "2026-09-26T01:00:00Z", "x", "end-2")

    def test_cancel_restores_every_unshipped_quota(self) -> None:
        self._two_nominations_allocated()
        self._declare()
        cancelled = self.service.cancel_force_majeure("risk", "case-1", "误报", "cancel-1")
        self.assertEqual(cancelled["state"], "cancelled")
        restored_total = {row["nomination_id"]: row for row in cancelled["curtailments"]}
        self.assertEqual(restored_total["nom-a"]["allocated_after"], "60.000")
        self.assertEqual(restored_total["nom-b"]["allocated_after"], "40.000")
        self.assertTrue(cancelled["conservation"]["allocation"]["conserved"])

    def test_nomination_trace_links_case_rank_appeal_and_recovery(self) -> None:
        self._two_nominations_allocated()
        self.service.dispatch_transfer("dispatch", "tr-1", "nom-a", "lot-1", 2)
        self._declare()
        self.service.submit_appeal("ship-b", {"appeal_id": "appeal-1", "case_id": "case-1", "shipper_id": "ship-b", "requested_barrels": "10", "reason": "x", "idempotency_key": "ak"})
        self.service.rule_appeal("risk", "appeal-1", True, "10", "rule-1", capacity_percent="70")
        trace = self.service.nomination_force_majeure_trace("ship-b", "nom-b")
        case = trace["force_majeure_cases"][0]
        self.assertEqual(case["contract_rank"], 2)
        self.assertEqual(len(case["versions"]), 2)
        self.assertEqual(case["versions"][-1]["allocated_after"], "10.000")
        self.assertEqual(case["appeal"]["state"], "granted")
        with self.assertRaises(Forbidden):
            self.service.nomination_force_majeure_trace("ship-a", "nom-b")

    def test_reduced_capacity_keeps_new_allocations_at_capped_level(self) -> None:
        self._declare()
        # 案件生效后才提交的新提名按降容后的能力分配，不能再拿名义全额。
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-c", "route_id": "pipe", "shipper_id": "ship-a", "service_date": "2026-09-26", "requested_barrels": "100", "priority": 10, "idempotency_key": "key-c"})
        allocation = self.service.allocate("dispatch", "pipe", "2026-09-26")
        self.assertEqual(allocation["available_capacity"], "50.000")
        self.assertEqual(allocation["allocations"][0]["allocated_barrels"], "50.000")

    def test_audit_chain_stays_valid_through_lifecycle(self) -> None:
        self._two_nominations_allocated()
        self._declare()
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_overlapping_active_cases_on_same_route_are_rejected(self) -> None:
        self._two_nominations_allocated()
        self._declare()
        with self.assertRaises(Conflict):
            self.service.declare_force_majeure("risk", {
                "case_id": "case-2", "route_id": "pipe", "title": "第二次事故",
                "impact_starts_at": "2026-09-26T00:00:00Z",
                "impact_ends_at": "2026-10-01T23:59:59Z",
                "capacity_percent": "40", "evidence": {"a": 1},
                "appeal_deadline": "2026-09-24T20:00:00Z",
            })


class ForceMajeureApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(self.connection, FrozenClock(datetime(2026, 9, 24, 8, tzinfo=timezone.utc)))
        self.app = JsonApplication(self.service)
        self.service.create_user("risk", "risk", "risk")
        self.service.create_user("ship-b", "ship-b", "shipper")
        self.service.create_user("plan", "plan", "planner")
        self.service.create_facility("plan", {"facility_id": "fac", "name": "f", "kind": "storage", "timezone": "UTC", "capacity_barrels": "1"})
        self.service.create_facility("plan", {"facility_id": "term", "name": "t", "kind": "terminal", "timezone": "UTC", "capacity_barrels": "1"})
        self.service.create_route("plan", {"route_id": "pipe", "origin_id": "fac", "destination_id": "term", "product": "crude", "daily_capacity": "100", "loss_basis_points": 0, "transit_hours": 1})

    def tearDown(self) -> None:
        self.connection.close()

    def test_declare_and_trace_endpoints(self) -> None:
        import json
        body = json.dumps({
            "case_id": "case-1", "route_id": "pipe", "title": "穿孔",
            "impact_starts_at": "2026-09-25T00:00:00Z",
            "impact_ends_at": "2026-09-27T23:59:59Z",
            "capacity_percent": "50", "evidence": {"photo": "x"},
            "appeal_deadline": "2026-09-24T20:00:00Z",
        }).encode()
        response = self.app.handle("POST", "/force-majeure", {"X-Actor-Id": "risk"}, body)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["version"], 1)
        case_response = self.app.handle("GET", "/force-majeure/case-1", {"X-Actor-Id": "risk"})
        self.assertEqual(case_response.status, 200)
        self.assertEqual(case_response.body["current_version"], 1)


if __name__ == "__main__":
    unittest.main()
