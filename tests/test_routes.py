from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from urban_network.api import Handler
from urban_network.models import Reading, Segment
from urban_network.service import NetworkService


def stop(work_order_id, start, end):
    return {"work_order_id": work_order_id, "planned_start": start, "planned_end": end}


def receipt(seq, work_order_id, event_type, **extra):
    base = {"seq": seq, "work_order_id": work_order_id, "crew_member": "crew-a",
            "event_type": event_type, "occurred_at": "2026-09-25T01:00:00+00:00", "route_version": 1}
    base.update(extra)
    return base


class RouteServiceTests(unittest.TestCase):
    def setUp(self):
        self.s = NetworkService(); self.s.bootstrap()
        self.admin = self.s.auth.login("admin", "network-admin")
        self.sup = self.s.auth.login("supervisor", "network-supervisor")
        self.op = self.s.auth.login("operator", "network-operator")
        self.s.register_segment(self.admin, Segment("S1", "east", "gas", 200, 4))
        alert = self.s.ingest_reading(
            self.admin, Reading("R1", "S1", "sensor", 100, 250, 90, "2026-01-01T00:00:00+00:00"))["alert_id"]
        self.wo1 = self.s.create_work_order(self.admin, "S1", alert, "crew", 1)["work_order_id"]
        self.wo2 = self.s.create_work_order(self.admin, "S1", alert, "crew", 2)["work_order_id"]
        self.stops = [
            stop(self.wo1, "2026-09-25T01:00:00+00:00", "2026-09-25T02:00:00+00:00"),
            stop(self.wo2, "2026-09-25T02:00:00+00:00", "2026-09-25T03:00:00+00:00"),
        ]

    def _route_locked(self):
        self.s.routes.create_route(self.sup, "RT-1", "east", "dev-1", self.stops)
        locked = self.s.routes.lock_route(self.sup, "RT-1")
        return locked

    def test_lock_pins_order_windows_and_version(self):
        self._route_locked()
        route = self.s.routes.route(self.sup, "RT-1")
        self.assertEqual(route["status"], "locked")
        self.assertEqual(route["current_version"], 1)
        self.assertEqual([s["position"] for s in route["stops"]], [1, 2])
        self.assertEqual([s["work_order_id"] for s in route["stops"]], [self.wo1, self.wo2])
        self.assertEqual(route["stops"][0]["planned_start"], "2026-09-25T01:00:00+00:00")
        # 锁定即派单
        self.assertEqual(self.s.work_order(self.admin, self.wo1)["status"], "assigned")
        versions = self.s.routes.route_versions(self.sup, "RT-1")
        self.assertEqual([v["version"] for v in versions], [1])
        detail = self.s.routes.route_version_detail(self.sup, "RT-1", 1)
        self.assertEqual(len(detail["stops"]), 2)

    def test_revision_keeps_old_snapshot(self):
        self._route_locked()
        revised = self.s.routes.lock_route(
            self.sup, "RT-1",
            stops=[stop(self.wo2, "2026-09-26T01:00:00+00:00", "2026-09-26T02:00:00+00:00"),
                   stop(self.wo1, "2026-09-26T02:00:00+00:00", "2026-09-26T03:00:00+00:00")],
            note="reschedule")
        self.assertEqual(revised["current_version"], 2)
        v1 = self.s.routes.route_version_detail(self.sup, "RT-1", 1)
        self.assertEqual([s["work_order_id"] for s in v1["stops"]], [self.wo1, self.wo2])
        v2 = self.s.routes.route_version_detail(self.sup, "RT-1", 2)
        self.assertEqual([s["work_order_id"] for s in v2["stops"]], [self.wo2, self.wo1])
        with self.assertRaises(ValueError):
            self.s.routes.lock_route(self.sup, "RT-1", stops=[
                stop(self.wo2, "2026-09-26T01:00:00+00:00", "2026-09-26T02:00:00+00:00"),
                stop(self.wo1, "2026-09-26T02:00:00+00:00", "2026-09-26T03:00:00+00:00")])  # 与当前版本一致

    def test_duplicate_work_order_or_bad_window_rejected(self):
        with self.assertRaises(ValueError):
            self.s.routes.create_route(
                self.sup, "RT-x", "east", "dev",
                [stop(self.wo1, "2026-09-25T01:00:00+00:00", "2026-09-25T02:00:00+00:00"),
                 stop(self.wo1, "2026-09-25T02:00:00+00:00", "2026-09-25T03:00:00+00:00")])
        with self.assertRaises(ValueError):
            self.s.routes.create_route(
                self.sup, "RT-y", "east", "dev",
                [stop(self.wo1, "2026-09-25T03:00:00+00:00", "2026-09-25T02:00:00+00:00")])

    def test_gap_registered_and_auto_filled_by_late_receipt(self):
        self._route_locked()
        result = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(1, self.wo1, "checkin"), receipt(3, self.wo1, "checkin")])
        self.assertEqual(result["status"], "open")
        gaps = [c for c in self.s.routes.conflicts(self.sup, "RT-1") if c["conflict_type"] == "gap"]
        self.assertEqual([(c["seq"], c["status"]) for c in gaps], [(2, "open")])
        result2 = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(2, self.wo1, "checkin")])
        self.assertEqual(result2["status"], "ready")
        gaps = [c for c in self.s.routes.conflicts(self.sup, "RT-1") if c["conflict_type"] == "gap"]
        self.assertEqual(gaps[0]["status"], "resolved")
        self.assertEqual(gaps[0]["resolution"]["decision"], "filled")

    def test_duplicate_seq_idempotent_but_divergent_payload_conflicts(self):
        self._route_locked()
        first = [receipt(1, self.wo1, "finding", defect={"code": "D1", "severity": "high"})]
        self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", first)
        again = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", first)
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["duplicate_receipts"], 1)
        self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(1, self.wo1, "finding", occurred_at="2026-09-25T01:30:00+00:00",
                    defect={"code": "D2", "severity": "low"})])
        dup = [c for c in self.s.routes.conflicts(self.sup, "RT-1") if c["conflict_type"] == "duplicate_seq"]
        self.assertEqual(len(dup), 1)
        # 原始回执未被覆盖
        stored = self.s.routes.receipts(self.sup, "RT-1")
        self.assertEqual(len(stored), 1)
        self.assertEqual(json.loads(self._raw_receipt(stored[0]["receipt_id"]))["defect"]["code"], "D1")

    def _raw_receipt(self, receipt_id):
        return self.s.db.execute("SELECT payload FROM offline_receipts WHERE receipt_id=?",
                                 (receipt_id,)).fetchone()[0]

    def test_conflicting_conclusions_require_arbitration(self):
        self._route_locked()
        result = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(1, self.wo1, "completion", conclusion="completed"),
            receipt(2, self.wo1, "completion", conclusion="blocked")])
        self.assertEqual(result["status"], "open")
        with self.assertRaises(ValueError):
            self.s.routes.confirm_batch(self.sup, result["batch_id"])
        conflict = next(c for c in self.s.routes.conflicts(self.sup, "RT-1", "open")
                        if c["conflict_type"] == "conclusion_conflict")
        # 必须选已存在的序号
        with self.assertRaises(ValueError):
            self.s.routes.resolve_conflict(self.sup, conflict["conflict_id"], {"winning_seq": 99})
        self.s.routes.resolve_conflict(self.sup, conflict["conflict_id"], {"winning_seq": 2})
        self.s.add_resource(self.admin, "P1", "pump", "east", 5)
        summary = self.s.routes.confirm_batch(self.sup, result["batch_id"])
        self.assertEqual(summary["completions"], 1)
        self.assertEqual(summary["superseded"], 1)
        self.assertEqual(self.s.work_order(self.admin, self.wo1)["status"], "blocked")
        actions = [e["action"] for e in self.s.audit_events(self.sup, "work_order", self.wo1)]
        self.assertIn("conclusion_superseded", actions)

    def test_confirm_drives_work_orders_and_consumes_materials(self):
        self._route_locked()
        self.s.add_resource(self.admin, "P1", "sealant", "east", 5)
        result = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(1, self.wo1, "checkin"),
            receipt(2, self.wo1, "finding", defect={"code": "D1", "severity": "medium"},
                    materials=[{"resource_id": "P1", "quantity": 2}]),
            receipt(3, self.wo2, "checkin"),
            receipt(4, self.wo2, "completion", conclusion="completed")])
        summary = self.s.routes.confirm_batch(self.sup, result["batch_id"])
        self.assertEqual(summary["checkins"], 2)
        self.assertEqual(summary["findings"], 1)
        self.assertEqual(summary["completions"], 1)
        self.assertEqual(self.s.work_order(self.admin, self.wo1)["status"], "in_progress")
        self.assertEqual(self.s.work_order(self.admin, self.wo2)["status"], "completed")
        self.assertEqual(self.s.resource(self.admin, "P1")["available"], 3)
        # 确认幂等
        again = self.s.routes.confirm_batch(self.sup, result["batch_id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.s.resource(self.admin, "P1")["available"], 3)

    def test_any_failure_rolls_back_whole_batch(self):
        self._route_locked()
        self.s.add_resource(self.admin, "P1", "sealant", "east", 1)  # 库存不足
        result = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(1, self.wo1, "checkin"),
            receipt(2, self.wo2, "completion", conclusion="completed",
                    materials=[{"resource_id": "P1", "quantity": 2}])])
        with self.assertRaises(ValueError):
            self.s.routes.confirm_batch(self.sup, result["batch_id"])
        # 工单状态全部保持锁定时的 assigned，库存未动，回执仍未应用
        self.assertEqual(self.s.work_order(self.admin, self.wo1)["status"], "assigned")
        self.assertEqual(self.s.work_order(self.admin, self.wo2)["status"], "assigned")
        self.assertEqual(self.s.resource(self.admin, "P1")["available"], 1)
        batches = {b["batch_id"]: b["status"] for b in self.s.routes.receipt_batches(self.sup, "RT-1")}
        self.assertEqual(batches[result["batch_id"]], "failed")
        for row in self.s.routes.receipts(self.sup, "RT-1"):
            self.assertEqual(row["status"], "received")
        # 补货后可重试，原子提交
        self.s.db.execute("UPDATE resources SET available=5 WHERE resource_id='P1'"); self.s.db.commit()
        summary = self.s.routes.confirm_batch(self.sup, result["batch_id"])
        self.assertEqual(summary["status"], "confirmed")
        self.assertEqual(self.s.work_order(self.admin, self.wo2)["status"], "completed")
        self.assertEqual(self.s.resource(self.admin, "P1")["available"], 3)

    def test_version_mismatch_conflict_and_receipt_outside_route(self):
        self._route_locked()
        result = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(1, self.wo1, "checkin", route_version=9)])
        types = {c["conflict_type"] for c in self.s.routes.conflicts(self.sup, "RT-1", "open")}
        self.assertIn("version_mismatch", types)
        with self.assertRaises(ValueError):
            self.s.routes.resolve_conflict(self.sup,
                next(c["conflict_id"] for c in self.s.routes.conflicts(self.sup, "RT-1")
                     if c["conflict_type"] == "version_mismatch"),
                {"decision": "ignore"})
        # 非锁定路线的工单回执直接拒绝
        alert = self.s.ingest_reading(
            self.admin, Reading("R2", "S1", "sensor", 100, 250, 90, "2026-01-02T00:00:00+00:00"))["alert_id"]
        wo3 = self.s.create_work_order(self.admin, "S1", alert, "crew", 3)["work_order_id"]
        with self.assertRaises(ValueError):
            self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [receipt(2, wo3, "checkin")])

    def test_permissions(self):
        self._route_locked()
        with self.assertRaises(PermissionError):
            self.s.routes.create_route(self.op, "RT-x", "east", "dev", [])
        with self.assertRaises(PermissionError):
            self.s.routes.lock_route(self.op, "RT-1")
        result = self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [
            receipt(1, self.wo1, "checkin")])
        # 缺号让批次处于 open
        self.s.routes.upload_receipts(self.op, "RT-1", "dev-1", [receipt(3, self.wo1, "checkin")])
        conflict = self.s.routes.conflicts(self.sup, "RT-1", "open")[0]
        with self.assertRaises(PermissionError):
            self.s.routes.resolve_conflict(self.op, conflict["conflict_id"], {"decision": "waive"})
        with self.assertRaises(PermissionError):
            self.s.routes.confirm_batch(self.op, result["batch_id"])


class RouteApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.service = NetworkService(); Handler.service.bootstrap()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def _call(self, method, path, token=None, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode())
        conn.close()
        return response.status, data

    def test_route_and_receipt_api_flow(self):
        status, body = self._call("POST", "/login", body={"user_id": "admin", "password": "network-admin"})
        self.assertEqual(status, 200); admin = body["token"]
        status, body = self._call("POST", "/login", body={"user_id": "supervisor", "password": "network-supervisor"})
        self.assertEqual(status, 200); sup = body["token"]
        status, body = self._call("POST", "/login", body={"user_id": "operator", "password": "network-operator"})
        self.assertEqual(status, 200); op = body["token"]

        self._call("POST", "/segments", admin,
                   {"segment_id": "SA", "district": "east", "network_type": "gas",
                    "length_m": 100, "criticality": 4})
        status, reading = self._call("POST", "/segments/SA/readings", admin,
                                     {"reading_id": "RA", "sensor_id": "s", "pressure_kpa": 100,
                                      "flow_lps": 250, "acoustic_db": 90,
                                      "observed_at": "2026-01-01T00:00:00+00:00"})
        status, order = self._call("POST", "/segments/SA/work-orders", admin,
                                   {"alert_id": reading["alert_id"], "assignee": "crew", "priority": 1})
        wo = order["work_order_id"]
        status, route = self._call("POST", "/routes", sup, {
            "route_id": "RTA", "district": "east", "device_id": "d1",
            "stops": [{"work_order_id": wo, "planned_start": "2026-09-25T01:00:00+00:00",
                       "planned_end": "2026-09-25T02:00:00+00:00"}]})
        self.assertEqual(status, 201)
        status, locked = self._call("POST", "/routes/RTA/lock", sup)
        self.assertEqual(status, 200); self.assertEqual(locked["current_version"], 1)
        status, uploaded = self._call("POST", "/routes/RTA/receipts", op, {
            "device_id": "d1", "receipts": [
                {"seq": 1, "work_order_id": wo, "crew_member": "a", "event_type": "checkin",
                 "occurred_at": "2026-09-25T01:05:00+00:00", "route_version": 1},
                {"seq": 2, "work_order_id": wo, "crew_member": "a", "event_type": "completion",
                 "occurred_at": "2026-09-25T01:30:00+00:00", "conclusion": "completed", "route_version": 1}]})
        self.assertEqual(status, 202); self.assertEqual(uploaded["status"], "ready")
        status, confirmed = self._call("POST", f"/receipt-batches/{uploaded['batch_id']}/confirm", sup)
        self.assertEqual(status, 200); self.assertEqual(confirmed["status"], "confirmed")
        status, conflicts = self._call("GET", "/routes/RTA/conflicts", sup)
        self.assertEqual(status, 200); self.assertEqual(conflicts, [])
        status, versions = self._call("GET", "/routes/RTA/versions", sup)
        self.assertEqual(status, 200); self.assertEqual(len(versions), 1)


if __name__ == "__main__":
    unittest.main()
