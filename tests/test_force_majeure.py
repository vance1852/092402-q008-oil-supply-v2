from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from oil_supply.service import SupplyService


class ForceMajeureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
        self.service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91", "received_at": "2026-09-24T06:00:00Z"})

    def tearDown(self) -> None:
        self.connection.close()

    def nominate(self, nomination_id: str, requested: str, priority: int, service_date: str = "2026-09-25") -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": nomination_id, "route_id": "pipe-a-b", "shipper_id": f"shipper-{nomination_id}", "service_date": service_date, "requested_barrels": requested, "priority": priority, "idempotency_key": f"key-{nomination_id}"})

    def declare(self, capacity: str = "25", case_id: str = "fm-1", starts: str = "2026-09-25T00:00:00Z", ends: str = "2026-09-26T00:00:00Z") -> dict[str, object]:
        return self.service.declare_force_majeure("risk", {"case_id": case_id, "route_id": "pipe-a-b", "evidence": {"summary": "管线泄漏", "report_ref": "INC-42"}, "starts_at": starts, "ends_at": ends, "capacity_percent": capacity, "appeal_deadline": "2026-09-25T12:00:00Z"})

    def prepare_curtailed(self) -> None:
        """三个提名分配后，一个发运、一个预留，再按 25% 上限削减。"""
        self.nominate("nom-a", "50000", 10)
        self.nominate("nom-b", "40000", 20)
        self.nominate("nom-c", "30000", 30)
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        self.service.dispatch_transfer("dispatch", "transfer-a", "nom-a", "lot-1", 2)
        self.service.reserve_inventory("dispatch", {"reservation_id": "res-1", "lot_id": "lot-1", "nomination_id": "nom-b", "quantity_barrels": "40000"})
        self.declare()
        self.service.apply_force_majeure("risk", "fm-1", 1)

    def nomination(self, nomination_id: str) -> dict[str, object]:
        row = self.connection.execute("SELECT * FROM nominations WHERE nomination_id=?", (nomination_id,)).fetchone()
        return dict(row)

    def reservation(self, reservation_id: str) -> dict[str, object]:
        row = self.connection.execute("SELECT * FROM inventory_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        return dict(row)

    def test_apply_curtails_deterministically_and_releases_reservation(self) -> None:
        self.prepare_curtailed()
        nom_b = self.nomination("nom-b")
        nom_c = self.nomination("nom-c")
        nom_a = self.nomination("nom-a")
        self.assertEqual(nom_b["allocated_barrels"], "25000.000")
        self.assertEqual(nom_b["state"], "allocated")
        self.assertEqual(nom_c["allocated_barrels"], "0.000")
        self.assertEqual(nom_c["state"], "cancelled")
        self.assertEqual(nom_a["allocated_barrels"], "50000.000")
        self.assertEqual(nom_a["state"], "in_transit")
        self.assertEqual(self.reservation("res-1")["held_barrels"], "25000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_barrels"], "75000.000")
        case = self.service.get_force_majeure("risk", "fm-1")
        self.assertEqual(len(case["runs"]), 1)
        totals = case["runs"][0]["totals"]
        self.assertEqual(totals["allocated_before"], "50000.000")
        self.assertEqual(totals["allocated_after"], "25000.000")
        self.assertEqual(totals["curtailed"], "25000.000")
        self.assertEqual(totals["released_inventory"], "15000.000")
        self.assertEqual(totals["in_transit"], "50000.000")
        day = case["runs"][0]["dates"][0]
        self.assertEqual(day["ceiling"], "25000.000")
        self.assertTrue(day["in_window"])
        self.assertEqual(Decimal(day["allocated_before"]) - Decimal(day["allocated_after"]), Decimal(day["curtailed"]) - Decimal(day["restored"]))

    def test_reapply_same_version_does_not_release_again(self) -> None:
        self.prepare_curtailed()
        before_lot = self.service.inventory_lot("lot-1")["available_barrels"]
        replay = self.service.apply_force_majeure("risk", "fm-1", 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run_id"], 1)
        self.assertEqual(self.service.inventory_lot("lot-1")["available_barrels"], before_lot)
        self.assertEqual(self.reservation("res-1")["held_barrels"], "25000.000")
        self.assertEqual(self.nomination("nom-b")["allocated_barrels"], "25000.000")
        case = self.service.get_force_majeure("risk", "fm-1")
        self.assertEqual(len(case["runs"]), 1)

    def test_appeal_once_before_deadline_and_decision_isolated(self) -> None:
        self.prepare_curtailed()
        filed = self.service.file_appeal("dispatch", "fm-1", {"appeal_id": "ap-1", "nomination_id": "nom-c", "reason": "长期合同应优先恢复", "requested_barrels": "8000"})
        self.assertEqual(filed["state"], "filed")
        with self.assertRaises(Conflict):
            self.service.file_appeal("dispatch", "fm-1", {"appeal_id": "ap-2", "nomination_id": "nom-c", "reason": "重复申诉", "requested_barrels": "1000"})
        with self.assertRaises(InvalidState):
            self.service.file_appeal("dispatch", "fm-1", {"appeal_id": "ap-3", "nomination_id": "nom-a", "reason": "未被削减", "requested_barrels": "1000"})
        self.service.dispatch_transfer("dispatch", "transfer-b", "nom-b", "lot-1", 3)
        self.assertEqual(self.reservation("res-1")["state"], "consumed")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_barrels"], "75000.000")
        decided = self.service.decide_appeal("risk", "ap-1", {"decision": "upheld", "note": "核准恢复"})
        self.assertEqual(decided["granted_barrels"], "8000.000")
        self.assertEqual(self.nomination("nom-c")["allocated_barrels"], "8000.000")
        self.assertEqual(self.nomination("nom-c")["state"], "allocated")
        self.assertEqual(self.nomination("nom-b")["allocated_barrels"], "25000.000")
        with self.assertRaises(InvalidState):
            self.service.decide_appeal("risk", "ap-1", {"decision": "rejected"})

    def test_appeal_grant_is_capped_by_ceiling_headroom(self) -> None:
        self.prepare_curtailed()
        self.service.file_appeal("dispatch", "fm-1", {"appeal_id": "ap-1", "nomination_id": "nom-c", "reason": "争取恢复", "requested_barrels": "10000"})
        decided = self.service.decide_appeal("risk", "ap-1", {"decision": "upheld"})
        self.assertEqual(decided["granted_barrels"], "0.000")
        self.assertEqual(self.nomination("nom-c")["allocated_barrels"], "0.000")

    def test_appeal_deadline_is_enforced(self) -> None:
        self.prepare_curtailed()
        self.clock.advance(hours=30)
        with self.assertRaises(InvalidState):
            self.service.file_appeal("dispatch", "fm-1", {"appeal_id": "ap-1", "nomination_id": "nom-c", "reason": "超时申诉", "requested_barrels": "1000"})

    def test_extend_end_early_and_revoke_form_successor_versions(self) -> None:
        self.nominate("nom-a", "40000", 10, "2026-09-25")
        self.nominate("nom-b", "30000", 20, "2026-09-26")
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-26")
        self.declare(case_id="fm-2", ends="2026-09-27T00:00:00Z")
        first = self.service.apply_force_majeure("risk", "fm-2", 1)
        self.assertEqual(first["totals"]["curtailed"], "20000.000")
        self.assertEqual(self.nomination("nom-a")["allocated_barrels"], "25000.000")
        self.assertEqual(self.nomination("nom-b")["allocated_barrels"], "25000.000")
        self.service.revise_force_majeure("risk", "fm-2", {"kind": "end_early", "ends_at": "2026-09-25T23:59:59Z", "evidence": {"summary": "提前恢复"}})
        second = self.service.apply_force_majeure("risk", "fm-2", 2)
        by_date = {row["service_date"]: row for row in second["dates"]}
        self.assertFalse(by_date["2026-09-26"]["in_window"])
        self.assertIsNone(by_date["2026-09-26"]["ceiling"])
        self.assertEqual(by_date["2026-09-26"]["restored"], "5000.000")
        self.assertEqual(self.nomination("nom-b")["allocated_barrels"], "30000.000")
        self.assertEqual(self.nomination("nom-a")["allocated_barrels"], "25000.000")
        self.service.revise_force_majeure("risk", "fm-2", {"kind": "revoke", "evidence": {"summary": "事故撤销"}})
        third = self.service.apply_force_majeure("risk", "fm-2", 3)
        self.assertEqual(third["totals"]["restored"], "15000.000")
        self.assertEqual(self.nomination("nom-a")["allocated_barrels"], "40000.000")
        case = self.service.get_force_majeure("dispatch", "fm-2")
        self.assertEqual(case["state"], "revoked")
        self.assertEqual([version["kind"] for version in case["versions"]], ["declare", "end_early", "revoke"])
        self.assertTrue(all(version["applied"] for version in case["versions"]))
        with self.assertRaises(InvalidState):
            self.service.revise_force_majeure("risk", "fm-2", {"kind": "extend", "ends_at": "2026-09-28T00:00:00Z"})

    def test_extend_restores_when_ceiling_raised(self) -> None:
        self.prepare_curtailed()
        self.service.revise_force_majeure("risk", "fm-1", {"kind": "extend", "ends_at": "2026-09-27T00:00:00Z", "capacity_percent": "60"})
        applied = self.service.apply_force_majeure("risk", "fm-1", 2)
        self.assertEqual(applied["totals"]["restored"], "25000.000")
        self.assertEqual(self.nomination("nom-b")["allocated_barrels"], "40000.000")
        self.assertEqual(self.nomination("nom-c")["allocated_barrels"], "10000.000")
        self.assertEqual(self.reservation("res-1")["held_barrels"], "25000.000")

    def test_trace_shows_case_order_appeal_and_restoration(self) -> None:
        self.prepare_curtailed()
        self.service.file_appeal("dispatch", "fm-1", {"appeal_id": "ap-1", "nomination_id": "nom-c", "reason": "合同优先级", "requested_barrels": "8000"})
        self.service.dispatch_transfer("dispatch", "transfer-b", "nom-b", "lot-1", 3)
        self.service.decide_appeal("risk", "ap-1", {"decision": "upheld"})
        self.service.revise_force_majeure("risk", "fm-1", {"kind": "extend", "ends_at": "2026-09-27T00:00:00Z", "capacity_percent": "60"})
        self.service.apply_force_majeure("risk", "fm-1", 2)
        trace = self.service.nomination_trace("dispatch", "nom-c")
        self.assertEqual(trace["nomination"]["allocated_barrels"], "10000.000")
        self.assertEqual(len(trace["force_majeure"]), 1)
        view = trace["force_majeure"][0]
        self.assertEqual(view["case_id"], "fm-1")
        self.assertEqual(view["latest_version"]["kind"], "extend")
        self.assertEqual(view["latest_version"]["capacity_percent"], "60")
        positions = {row["nomination_id"]: row["position"] for row in view["contract_order"]}
        self.assertEqual(positions, {"nom-a": 1, "nom-b": 2, "nom-c": 3})
        self.assertEqual(len(view["curtailments"]), 1)
        self.assertEqual(view["curtailments"][0]["allocated_before"], "10000.000")
        self.assertEqual(view["curtailments"][0]["allocated_after"], "0.000")
        self.assertEqual(len(view["restorations"]), 1)
        self.assertEqual(view["restorations"][0]["allocated_after"], "10000.000")
        self.assertEqual(view["appeal"]["state"], "upheld")
        self.assertEqual(view["appeal"]["granted_barrels"], "8000.000")
        untouched = self.service.nomination_trace("dispatch", "nom-a")
        self.assertEqual(untouched["force_majeure"], [])

    def test_open_case_limits_new_allocation(self) -> None:
        self.nominate("nom-a", "80000", 10)
        self.declare(capacity="50")
        allocation = self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "50000.000")

    def test_permissions_are_enforced(self) -> None:
        self.nominate("nom-a", "80000", 10)
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        with self.assertRaises(Forbidden):
            self.service.declare_force_majeure("dispatch", {"case_id": "fm-1", "route_id": "pipe-a-b", "evidence": {"summary": "x"}, "starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-26T00:00:00Z", "capacity_percent": "50", "appeal_deadline": "2026-09-25T12:00:00Z"})
        self.declare(capacity="50")
        with self.assertRaises(Forbidden):
            self.service.apply_force_majeure("plan", "fm-1", 1)
        self.service.apply_force_majeure("risk", "fm-1", 1)
        with self.assertRaises(Forbidden):
            self.service.file_appeal("risk", "fm-1", {"appeal_id": "ap-1", "nomination_id": "nom-a", "reason": "x", "requested_barrels": "100"})
        self.service.file_appeal("dispatch", "fm-1", {"appeal_id": "ap-1", "nomination_id": "nom-a", "reason": "x", "requested_barrels": "100"})
        with self.assertRaises(Forbidden):
            self.service.decide_appeal("dispatch", "ap-1", {"decision": "rejected"})
        with self.assertRaises(Forbidden):
            self.service.get_force_majeure("plan", "fm-1")
        self.assertEqual(self.service.get_force_majeure("audit", "fm-1")["case_id"], "fm-1")

    def test_version_and_declaration_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.declare(ends="2026-09-24T00:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.service.declare_force_majeure("risk", {"case_id": "fm-9", "route_id": "pipe-a-b", "evidence": {"summary": "x"}, "starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-26T00:00:00Z", "capacity_percent": "50", "appeal_deadline": "2026-09-23T00:00:00Z"})
        self.declare()
        with self.assertRaises(Conflict):
            self.declare()
        with self.assertRaises(ValidationFailed):
            self.service.revise_force_majeure("risk", "fm-1", {"kind": "extend", "ends_at": "2026-09-25T12:00:00Z"})
        with self.assertRaises(ValidationFailed):
            self.service.revise_force_majeure("risk", "fm-1", {"kind": "amend"})
        with self.assertRaises(ValidationFailed):
            self.service.revise_force_majeure("risk", "fm-1", {"kind": "revoke", "capacity_percent": "80"})
        self.service.revise_force_majeure("risk", "fm-1", {"kind": "extend", "ends_at": "2026-09-27T00:00:00Z"})
        with self.assertRaises(InvalidState):
            self.service.apply_force_majeure("risk", "fm-1", 1)
        with self.assertRaises(NotFound):
            self.service.apply_force_majeure("risk", "fm-1", 9)
        with self.assertRaises(NotFound):
            self.service.get_force_majeure("risk", "fm-none")

    def test_reservation_rules(self) -> None:
        self.nominate("nom-a", "40000", 10)
        with self.assertRaises(InvalidState):
            self.service.reserve_inventory("dispatch", {"reservation_id": "res-1", "lot_id": "lot-1", "nomination_id": "nom-a", "quantity_barrels": "1000"})
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        with self.assertRaises(Conflict):
            self.service.reserve_inventory("dispatch", {"reservation_id": "res-1", "lot_id": "lot-1", "nomination_id": "nom-a", "quantity_barrels": "40001"})
        self.service.reserve_inventory("dispatch", {"reservation_id": "res-1", "lot_id": "lot-1", "nomination_id": "nom-a", "quantity_barrels": "30000"})
        self.assertEqual(self.service.inventory_lot("lot-1")["available_barrels"], "120000.000")
        with self.assertRaises(Conflict):
            self.service.reserve_inventory("dispatch", {"reservation_id": "res-2", "lot_id": "lot-1", "nomination_id": "nom-a", "quantity_barrels": "10001"})
        transfer = self.service.dispatch_transfer("dispatch", "transfer-a", "nom-a", "lot-1", 2)
        self.assertEqual(transfer["loaded_barrels"], "40000.000")
        self.assertEqual(self.reservation("res-1")["state"], "consumed")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_barrels"], "110000.000")

    def test_api_exposes_force_majeure_flow(self) -> None:
        app = JsonApplication(self.service)
        self.nominate("nom-a", "40000", 10)
        self.nominate("nom-b", "30000", 20)
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        headers = {"X-Actor-Id": "risk"}
        body = json.dumps({"case_id": "fm-api", "route_id": "pipe-a-b", "evidence": {"summary": "泄漏"}, "starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-26T00:00:00Z", "capacity_percent": "50", "appeal_deadline": "2026-09-25T12:00:00Z"}).encode()
        response = app.handle("POST", "/force-majeure", headers, body)
        self.assertEqual(response.status, 201)
        response = app.handle("POST", "/force-majeure/fm-api/apply", headers, json.dumps({"version_no": 1}).encode())
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["replayed"])
        self.assertEqual(self.nomination("nom-b")["allocated_barrels"], "10000.000")
        self.service.dispatch_transfer("dispatch", "transfer-a", "nom-a", "lot-1", 2)
        response = app.handle("POST", "/force-majeure/fm-api/appeals", {"X-Actor-Id": "dispatch"}, json.dumps({"appeal_id": "ap-api", "nomination_id": "nom-b", "reason": "恢复", "requested_barrels": "5000"}).encode())
        self.assertEqual(response.status, 201)
        response = app.handle("POST", "/appeals/ap-api/decide", headers, json.dumps({"decision": "partially_upheld", "granted_barrels": "3000"}).encode())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["granted_barrels"], "3000.000")
        response = app.handle("GET", "/force-majeure/fm-api", headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["runs"]), 1)
        response = app.handle("GET", "/nominations/nom-b/trace", {"X-Actor-Id": "dispatch"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["force_majeure"][0]["case_id"], "fm-api")
        self.assertEqual(response.body["nomination"]["allocated_barrels"], "13000.000")


if __name__ == "__main__":
    unittest.main()
