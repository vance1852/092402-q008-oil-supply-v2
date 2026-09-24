"""贯通报价、线路、库存、提名和情景分析的离线验收。"""

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
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor"), ("refinery-west", "shipper")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    # 同日低优先级提名只能使用在途之后的剩余能力。
    service.submit_nomination("dispatch", {"nomination_id": "nom-002", "route_id": "pipe-a-b", "shipper_id": "refinery-west", "service_date": "2026-09-25", "requested_barrels": "60000", "priority": 20, "idempotency_key": "nom-key-002"})
    service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    # 事故后冻结证据、窗口与容量上限：在途 80000 保持不变，未发运配额被确定性削减并退回预留。
    declared = service.declare_force_majeure("risk", {
        "case_id": "fm-pipe-001",
        "route_id": "pipe-a-b",
        "title": "管道穿孔降容",
        "impact_starts_at": "2026-09-25T00:00:00Z",
        "impact_ends_at": "2026-09-27T23:59:59Z",
        "capacity_percent": "60",
        "evidence": {"photo_sha256": "evidence-photo", "scada_tag": "PT-42"},
        "appeal_deadline": "2026-09-24T20:00:00Z",
    })
    service.submit_appeal("refinery-west", {"appeal_id": "appeal-001", "case_id": "fm-pipe-001", "shipper_id": "refinery-west", "requested_barrels": "20000", "reason": "合同排序应保留通道", "idempotency_key": "appeal-key-001"})
    ruling = service.rule_appeal("risk", "appeal-001", True, "20000", "rule-001", capacity_percent="100")
    trace = service.nomination_force_majeure_trace("refinery-west", "nom-002")
    force_majeure = {
        "declared_version": declared["version"],
        "declared_conserved": declared["conservation"]["allocation"]["conserved"],
        "curtailed": declared["curtailments"],
        "ruling_version": ruling["version"]["version"],
        "ruling_conserved": ruling["version"]["conservation"]["allocation"]["conserved"],
        "trace_versions": [
            {"version": item["version"], "after": item["allocated_after"], "restored": item["restored_barrels"]}
            for item in trace["force_majeure_cases"][0]["versions"]
        ],
        "contract_rank": trace["force_majeure_cases"][0]["contract_rank"],
        "reserved_barrels": service.inventory_summary("field-a", "crude")["reserved_barrels"],
    }
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    result = {"status": "ok", "price": service.price_summary("BRENT"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "force_majeure": force_majeure, "scenario_run_id": scenario["run_id"], "audit": service.audit_chain("audit"), "workspace": workspace.name}
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
