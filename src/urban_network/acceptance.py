"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json
from .models import Reading,Segment
from .service import NetworkService
def run():
    s=NetworkService(); s.bootstrap(); admin=s.auth.login("admin","network-admin"); supervisor=s.auth.login("supervisor","network-supervisor"); operator=s.auth.login("operator","network-operator")
    s.register_segment(admin,Segment("SEG-DEMO","north","water",680,5))
    r1=s.ingest_reading(admin,Reading("RD-DEMO-1","SEG-DEMO","sensor-01",160,230,88,"2026-09-24T10:00:00+00:00")); r2=s.ingest_reading(admin,Reading("RD-DEMO-2","SEG-DEMO","sensor-01",150,235,90,"2026-09-24T11:00:00+00:00"))
    report=s.risk_report(admin,"SEG-DEMO")
    order1=s.create_work_order(admin,"SEG-DEMO",r1["alert_id"],"crew-north",1); order2=s.create_work_order(admin,"SEG-DEMO",r2["alert_id"],"crew-north",2)
    s.add_resource(admin,"PUMP-01","mobile-pump","north",2); allocation=s.allocate(admin,"PUMP-01",order1["work_order_id"],1); s.add_resource(admin,"SEALANT-01","sealant","north",10)
    route=s.create_route(supervisor,"ROUTE-DEMO","north",[
        {"position":1,"work_order_id":order1["work_order_id"],"planned_start":"2026-09-25T08:00:00+00:00","planned_end":"2026-09-25T10:00:00+00:00"},
        {"position":2,"work_order_id":order2["work_order_id"],"planned_start":"2026-09-25T10:30:00+00:00","planned_end":"2026-09-25T12:00:00+00:00"}],note="早班巡检")
    first=s.upload_receipts(operator,{"receipt_batch_id":"RB-DEMO-1","route_id":"ROUTE-DEMO","version":1,"device_id":"device-07","receipts":[
        {"sequence":1,"work_order_id":order1["work_order_id"],"event_type":"check_in","occurred_at":"2026-09-25T08:05:00+00:00"},
        {"sequence":2,"work_order_id":order1["work_order_id"],"event_type":"material_use","resource_id":"SEALANT-01","quantity":2,"occurred_at":"2026-09-25T08:40:00+00:00"},
        {"sequence":4,"work_order_id":order1["work_order_id"],"event_type":"complete","conclusion":"completed","occurred_at":"2026-09-25T09:30:00+00:00"}]})
    second=s.upload_receipts(operator,{"receipt_batch_id":"RB-DEMO-2","route_id":"ROUTE-DEMO","version":1,"device_id":"device-07","receipts":[
        {"sequence":3,"work_order_id":order2["work_order_id"],"event_type":"check_in","occurred_at":"2026-09-25T08:20:00+00:00"},
        {"sequence":5,"work_order_id":order2["work_order_id"],"event_type":"defect","defect_kind":"crack","defect_note":"管壁裂缝 2cm","occurred_at":"2026-09-25T11:00:00+00:00"},
        {"sequence":6,"work_order_id":order2["work_order_id"],"event_type":"complete","conclusion":"completed","occurred_at":"2026-09-25T11:30:00+00:00"}]})
    confirmed=[s.confirm_batch(supervisor,"RB-DEMO-1"),s.confirm_batch(supervisor,"RB-DEMO-2")]
    return {"status":"ok","segment":"SEG-DEMO","severity":r1["risk"]["severity"],"probability":report["leak_probability"],"allocation":allocation["allocation_id"],"route":route["route_id"],"route_version":route["version"],"first_upload_gaps":first["gaps"],"second_upload_gaps":second["gaps"],"batches":[b["status"] for b in confirmed],"work_orders":[s.work_order(admin,o["work_order_id"])["status"] for o in (order1,order2)],"sealant_available":s.resource(admin,"SEALANT-01")["available"]}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
