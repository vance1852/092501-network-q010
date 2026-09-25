import unittest
from urban_network.errors import Conflict,InvalidState,NotFound,ValidationFailed
from urban_network.models import Reading,Segment
from urban_network.service import NetworkService

OCCURRED="2026-09-25T08:05:00+00:00"

def check_in(seq,work_order_id): return {"sequence":seq,"work_order_id":work_order_id,"event_type":"check_in","occurred_at":OCCURRED}
def complete(seq,work_order_id,conclusion): return {"sequence":seq,"work_order_id":work_order_id,"event_type":"complete","conclusion":conclusion,"occurred_at":OCCURRED}
def defect(seq,work_order_id,kind="crack"): return {"sequence":seq,"work_order_id":work_order_id,"event_type":"defect","defect_kind":kind,"defect_note":"现场记录","occurred_at":OCCURRED}
def material(seq,work_order_id,resource_id,quantity): return {"sequence":seq,"work_order_id":work_order_id,"event_type":"material_use","resource_id":resource_id,"quantity":quantity,"occurred_at":OCCURRED}

class RouteReceiptTests(unittest.TestCase):
    def setUp(self):
        self.s=NetworkService(); self.s.bootstrap()
        self.admin=self.s.auth.login("admin","network-admin"); self.supervisor=self.s.auth.login("supervisor","network-supervisor"); self.operator=self.s.auth.login("operator","network-operator")
        self.s.register_segment(self.admin,Segment("S1","east","drainage",100,4)); self.s.add_resource(self.admin,"MAT-1","sealant","east",5)
        self.orders=[self._make_order(f"R{i}","2026-01-0%dT00:00:00+00:00"%i) for i in (1,2,3)]
    def _make_order(self,reading_id,observed_at):
        r=self.s.ingest_reading(self.admin,Reading(reading_id,"S1","sensor",100,250,90,observed_at))
        return self.s.create_work_order(self.admin,"S1",r["alert_id"],"crew")["work_order_id"]
    def _stops(self,*work_order_ids):
        return [{"position":i+1,"work_order_id":w,"planned_start":"2026-09-25T08:00:00+00:00","planned_end":"2026-09-25T10:00:00+00:00"} for i,w in enumerate(work_order_ids)]
    def _upload(self,batch_id,receipts,device="dev-1",route="RT-1",version=1):
        return self.s.upload_receipts(self.operator,{"receipt_batch_id":batch_id,"route_id":route,"version":version,"device_id":device,"receipts":receipts})
    def _open_conflict(self,kind,route="RT-1"):
        return [c for c in self.s.list_conflicts(self.supervisor,route,"open") if c["kind"]==kind][0]

    def test_route_create_revise_and_binding(self):
        route=self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0],self.orders[1]),note="早班")
        self.assertEqual(route["version"],1); self.assertEqual([s["position"] for s in route["stops"]],[1,2])
        with self.assertRaises(Conflict): self.s.create_route(self.supervisor,"RT-2","east",self._stops(self.orders[0]))
        v2=self.s.revise_route(self.supervisor,"RT-1",self._stops(self.orders[1],self.orders[2]))
        self.assertEqual(v2["version"],2)
        versions=self.s.route_versions(self.supervisor,"RT-1")
        self.assertEqual([v["status"] for v in versions["versions"]],["superseded","active"])
        self.assertEqual(self.s.route(self.supervisor,"RT-1")["version"],2)
        self.assertEqual(self.s.route(self.supervisor,"RT-1",1)["version"],1)
        self.assertEqual([e["action"] for e in self.s.audit_events(self.admin,"route","RT-1")],["created","revised"])
        self._upload("RB-1",[check_in(1,self.orders[1])],version=2)
        with self.assertRaises(InvalidState): self.s.revise_route(self.supervisor,"RT-1",self._stops(self.orders[1]))

    def test_stop_validation(self):
        with self.assertRaises(ValidationFailed): self.s.create_route(self.supervisor,"RT-1","east",[])
        bad_window=[{"position":1,"work_order_id":self.orders[0],"planned_start":"2026-09-25T10:00:00+00:00","planned_end":"2026-09-25T08:00:00+00:00"}]
        with self.assertRaises(ValidationFailed): self.s.create_route(self.supervisor,"RT-1","east",bad_window)
        with self.assertRaises(NotFound): self.s.create_route(self.supervisor,"RT-1","east",self._stops("wo-404"))
        duplicated=self._stops(self.orders[0],self.orders[1]); duplicated[1]["position"]=1
        with self.assertRaises(ValidationFailed): self.s.create_route(self.supervisor,"RT-1","east",duplicated)

    def test_upload_replay_is_idempotent(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        payload=[check_in(1,self.orders[0]),complete(2,self.orders[0],"completed")]
        first=self._upload("RB-1",payload); replay=self._upload("RB-1",payload)
        self.assertFalse(first["replay"]); self.assertTrue(replay["replay"])
        self.assertEqual(first["merged"],2)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM receipts").fetchone()[0],2)
        with self.assertRaises(Conflict): self._upload("RB-1",[check_in(9,self.orders[0])])

    def test_gap_detection_fill_and_confirm(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0],self.orders[1]))
        w1,w2=self.orders[0],self.orders[1]
        first=self._upload("RB-1",[check_in(1,w1),complete(3,w1,"completed")])
        self.assertEqual(first["gaps"],[2]); self.assertEqual(first["status"],"blocked")
        with self.assertRaises(InvalidState): self.s.confirm_batch(self.supervisor,"RB-1")
        second=self._upload("RB-2",[check_in(2,w2),complete(4,w2,"completed")])
        self.assertEqual(second["gaps"],[])
        conflicts=self.s.list_conflicts(self.supervisor,"RT-1")
        self.assertEqual([(c["kind"],c["status"],c["resolution"]) for c in conflicts],[("gap","resolved","filled")])
        self.assertEqual(self.s.confirm_batch(self.supervisor,"RB-1")["status"],"confirmed")
        self.assertEqual(self.s.confirm_batch(self.supervisor,"RB-2")["status"],"confirmed")
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"completed")
        self.assertEqual(self.s.work_order(self.admin,w2)["status"],"completed")
        self.assertTrue(self.s.confirm_batch(self.supervisor,"RB-1")["replay"])

    def test_gap_dismissed_by_supervisor(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        self._upload("RB-1",[check_in(1,self.orders[0]),complete(3,self.orders[0],"completed")])
        conflict=self._open_conflict("gap")
        with self.assertRaises(ValidationFailed): self.s.resolve_conflict(self.supervisor,conflict["conflict_id"],"keep_existing")
        resolved=self.s.resolve_conflict(self.supervisor,conflict["conflict_id"],"dismissed",note="设备损坏，序号 2 确认丢失")
        self.assertEqual(resolved["status"],"resolved"); self.assertEqual(resolved["resolved_by"],"supervisor")
        self.assertEqual(self.s.confirm_batch(self.supervisor,"RB-1")["status"],"confirmed")
        with self.assertRaises(InvalidState): self.s.resolve_conflict(self.supervisor,conflict["conflict_id"],"dismissed")

    def test_duplicate_sequence_keep_existing(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        self._upload("RB-1",[check_in(1,w1)])
        second=self._upload("RB-2",[complete(1,w1,"completed")])
        self.assertEqual(second["status"],"blocked"); self.assertEqual(second["duplicates"],1)
        conflict=self._open_conflict("duplicate_sequence")
        self.assertEqual(conflict["sequence"],1)
        self.assertNotEqual(conflict["existing_sha256"],conflict["incoming_sha256"])
        live=[r for r in self.s.receipt_batch(self.supervisor,"RB-1")["receipts"] if r["status"]=="pending"]
        self.assertEqual([r["event_type"] for r in live],["check_in"])
        self.s.resolve_conflict(self.supervisor,conflict["conflict_id"],"keep_existing")
        confirmed=self.s.confirm_batch(self.supervisor,"RB-1")
        self.assertEqual(confirmed["check_ins"],1)
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"in_progress")

    def test_duplicate_sequence_keep_incoming(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        self._upload("RB-1",[check_in(1,w1)])
        self._upload("RB-2",[defect(1,w1)])
        conflict=self._open_conflict("duplicate_sequence")
        self.s.resolve_conflict(self.supervisor,conflict["conflict_id"],"keep_incoming")
        with self.assertRaises(InvalidState): self.s.confirm_batch(self.supervisor,"RB-1")
        confirmed=self.s.confirm_batch(self.supervisor,"RB-2")
        self.assertEqual(confirmed["defects"],1)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM defects").fetchone()[0],1)
        statuses={r["event_type"]:r["status"] for r in self.s.db.execute("SELECT event_type,status FROM receipts ORDER BY receipt_id").fetchall()}
        self.assertEqual(statuses,{"check_in":"void","defect":"applied"})

    def test_conclusion_conflict_keep_completed(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        self._upload("RB-1",[check_in(1,w1),complete(2,w1,"completed")])
        second=self._upload("RB-2",[complete(3,w1,"blocked")])
        self.assertEqual(second["status"],"blocked")
        conflict=self._open_conflict("work_order_conclusion")
        self.assertEqual(conflict["work_order_id"],w1)
        self.assertEqual(sorted(conflict["detail"]["conclusions"]),["blocked","completed"])
        with self.assertRaises(InvalidState): self.s.confirm_batch(self.supervisor,"RB-1")
        self.s.resolve_conflict(self.supervisor,conflict["conflict_id"],"completed")
        self.assertEqual(self.s.confirm_batch(self.supervisor,"RB-1")["status"],"confirmed")
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"completed")
        with self.assertRaises(InvalidState): self.s.confirm_batch(self.supervisor,"RB-2")

    def test_conclusion_conflict_keep_blocked(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        self._upload("RB-1",[check_in(1,w1),complete(2,w1,"completed")])
        self._upload("RB-2",[complete(3,w1,"blocked")])
        conflict=self._open_conflict("work_order_conclusion")
        self.s.resolve_conflict(self.supervisor,conflict["conflict_id"],"blocked")
        self.assertEqual(self.s.confirm_batch(self.supervisor,"RB-1")["status"],"confirmed")
        self.assertEqual(self.s.confirm_batch(self.supervisor,"RB-2")["status"],"confirmed")
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"blocked")

    def test_confirm_rolls_back_atomically_on_material_shortage(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        self._upload("RB-1",[check_in(1,w1),material(2,w1,"MAT-1",10),complete(3,w1,"completed")])
        with self.assertRaises(InvalidState): self.s.confirm_batch(self.supervisor,"RB-1")
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"open")
        self.assertEqual(self.s.resource(self.admin,"MAT-1")["available"],5)
        batch=self.s.receipt_batch(self.supervisor,"RB-1")
        self.assertEqual(batch["status"],"failed")
        self.assertTrue(all(r["status"]=="pending" for r in batch["receipts"]))
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM material_consumption").fetchone()[0],0)
        self.s.db.execute("UPDATE resources SET available=20 WHERE resource_id='MAT-1'"); self.s.db.commit()
        confirmed=self.s.confirm_batch(self.supervisor,"RB-1")
        self.assertEqual(confirmed["status"],"confirmed")
        self.assertEqual(self.s.resource(self.admin,"MAT-1")["available"],10)
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"completed")
        self.assertEqual(self.s.db.execute("SELECT quantity FROM material_consumption").fetchone()[0],10)

    def test_full_happy_path_with_defect_and_material(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        batch=self._upload("RB-1",[check_in(1,w1),defect(2,w1),material(3,w1,"MAT-1",2),complete(4,w1,"completed")])
        self.assertEqual(batch["status"],"received")
        confirmed=self.s.confirm_batch(self.supervisor,"RB-1")
        self.assertEqual((confirmed["check_ins"],confirmed["defects"],confirmed["materials"],confirmed["completions"]),(1,1,1,1))
        self.assertEqual(self.s.db.execute("SELECT kind FROM defects").fetchone()[0],"crack")
        self.assertEqual(self.s.resource(self.admin,"MAT-1")["available"],3)
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"completed")
        self.assertIn("receipt-transition",[e["action"] for e in self.s.audit_events(self.admin,"work_order",w1)])
        self.assertIn("consumed",[e["action"] for e in self.s.audit_events(self.admin,"resource","MAT-1")])

    def test_blocked_conclusion_marks_work_order_blocked(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        self._upload("RB-1",[check_in(1,w1),complete(2,w1,"blocked")])
        self.s.confirm_batch(self.supervisor,"RB-1")
        self.assertEqual(self.s.work_order(self.admin,w1)["status"],"blocked")

    def test_upload_validation_and_permissions(self):
        self.s.create_route(self.supervisor,"RT-1","east",self._stops(self.orders[0]))
        w1=self.orders[0]
        with self.assertRaises(ValidationFailed): self._upload("RB-X",[check_in(1,self.orders[1])])
        with self.assertRaises(NotFound): self._upload("RB-X",[check_in(1,w1)],route="RT-404")
        with self.assertRaises(ValidationFailed): self._upload("RB-X",[{"sequence":1,"work_order_id":w1,"event_type":"ping","occurred_at":OCCURRED}])
        with self.assertRaises(ValidationFailed): self._upload("RB-X",[check_in(1,w1),check_in(1,w1)])
        with self.assertRaises(ValidationFailed): self._upload("RB-X",[{"sequence":1,"work_order_id":w1,"event_type":"complete","occurred_at":OCCURRED}])
        with self.assertRaises(ValidationFailed): self._upload("RB-X",[material(1,w1,"MAT-1",0)])
        with self.assertRaises(NotFound): self._upload("RB-X",[material(1,w1,"MAT-404",1)])
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM receipts").fetchone()[0],0)
        with self.assertRaises(PermissionError): self.s.create_route(self.operator,"RT-9","east",self._stops(w1))
        self._upload("RB-1",[check_in(1,w1)])
        with self.assertRaises(PermissionError): self.s.confirm_batch(self.operator,"RB-1")
        with self.assertRaises(PermissionError): self.s.resolve_conflict(self.operator,1,"dismissed")
        self.s.auth.create_user("viewer1","viewer-pass-1","viewer"); viewer=self.s.auth.login("viewer1","viewer-pass-1")
        with self.assertRaises(PermissionError): self.s.upload_receipts(viewer,{"receipt_batch_id":"RB-V","route_id":"RT-1","version":1,"device_id":"dev-9","receipts":[check_in(2,w1)]})

if __name__=="__main__":
    unittest.main()
