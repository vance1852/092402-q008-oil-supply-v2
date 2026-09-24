"""贯通报价、线路、库存、提名、不可抗力和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "50000", "priority": 10, "idempotency_key": "nom-key-001"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-002", "route_id": "pipe-a-b", "shipper_id": "refinery-west", "service_date": "2026-09-25", "requested_barrels": "40000", "priority": 20, "idempotency_key": "nom-key-002"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-003", "route_id": "pipe-a-b", "shipper_id": "refinery-north", "service_date": "2026-09-25", "requested_barrels": "30000", "priority": 30, "idempotency_key": "nom-key-003"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    service.reserve_inventory("dispatch", {"reservation_id": "res-001", "lot_id": "lot-001", "nomination_id": "nom-002", "quantity_barrels": "40000"})
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.declare_force_majeure("risk", {"case_id": "fm-001", "route_id": "pipe-a-b", "evidence": {"summary": "线路泄漏停运", "report_ref": "INC-2026-42"}, "starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-26T00:00:00Z", "capacity_percent": "25", "appeal_deadline": "2026-09-25T12:00:00Z"})
    applied = service.apply_force_majeure("risk", "fm-001", 1)
    replay = service.apply_force_majeure("risk", "fm-001", 1)
    service.dispatch_transfer("dispatch", "transfer-002", "nom-002", "lot-001", 3)
    service.file_appeal("dispatch", "fm-001", {"appeal_id": "ap-001", "nomination_id": "nom-003", "reason": "长期合同应优先恢复", "requested_barrels": "8000"})
    appeal = service.decide_appeal("risk", "ap-001", {"decision": "upheld", "note": "在释放的能力余量内核准"})
    service.revise_force_majeure("risk", "fm-001", {"kind": "extend", "ends_at": "2026-09-27T00:00:00Z", "capacity_percent": "60", "evidence": {"summary": "部分恢复通油", "report_ref": "INC-2026-42"}})
    service.apply_force_majeure("risk", "fm-001", 2)
    service.revise_force_majeure("risk", "fm-001", {"kind": "revoke", "evidence": {"summary": "事故撤销，恢复全量", "report_ref": "INC-2026-42"}})
    service.apply_force_majeure("risk", "fm-001", 3)
    case = service.get_force_majeure("dispatch", "fm-001")
    trace = service.nomination_trace("dispatch", "nom-003")
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    result = {
        "status": "ok",
        "price": service.price_summary("BRENT"),
        "allocation_id": allocation["allocation_id"],
        "transfer": transfer,
        "force_majeure": {
            "case_id": case["case_id"],
            "state": case["state"],
            "versions": len(case["versions"]),
            "runs": len(case["runs"]),
            "curtailed_total": applied["totals"]["curtailed"],
            "released_inventory": applied["totals"]["released_inventory"],
            "in_transit_untouched": applied["totals"]["in_transit"],
            "replay_run_id": replay["run_id"],
            "appeal_granted": appeal["granted_barrels"],
            "trace_restorations": len(trace["force_majeure"][0]["restorations"]),
        },
        "scenario_run_id": scenario["run_id"],
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行油气供应服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
