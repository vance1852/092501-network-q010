"""依赖标准库的 JSON HTTP API。"""
from __future__ import annotations
import argparse,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlparse
from .models import Reading,Segment
from .service import NetworkService
class Handler(BaseHTTPRequestHandler):
    service=NetworkService()
    def _send(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _token(self):return self.headers.get("Authorization","").removeprefix("Bearer ")
    def do_GET(self):
        try:
            parsed=urlparse(self.path); path=parsed.path; query=parse_qs(parsed.query)
            if path=="/health":return self._send(200,{"status":"ok","service":"urban-network"})
            if path.startswith("/segments/") and path.endswith("/risk"):return self._send(200,self.service.risk_report(self._token(),path.split("/")[2]))
            if path.startswith("/segments/"):return self._send(200,self.service.segment(self._token(),path.split("/",2)[2]))
            parts=path.strip("/").split("/")
            if len(parts)==2 and parts[0]=="routes":return self._send(200,self.service.routes.route(self._token(),parts[1]))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="versions":return self._send(200,self.service.routes.route_versions(self._token(),parts[1]))
            if len(parts)==4 and parts[0]=="routes" and parts[2]=="versions":return self._send(200,self.service.routes.route_version_detail(self._token(),parts[1],int(parts[3])))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="conflicts":return self._send(200,self.service.routes.conflicts(self._token(),parts[1],query.get("status",[None])[0]))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="receipts":return self._send(200,self.service.routes.receipts(self._token(),parts[1],query.get("batch_id",[None])[0]))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="batches":return self._send(200,self.service.routes.receipt_batches(self._token(),parts[1]))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
    def do_POST(self):
        try:
            body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            if self.path=="/login":return self._send(200,{"token":self.service.auth.login(body["user_id"],body["password"])})
            token=self._token()
            path=urlparse(self.path).path
            if path=="/segments":return self._send(201,self.service.register_segment(token,Segment(body["segment_id"],body["district"],body["network_type"],body["length_m"],body["criticality"])))
            if path.startswith("/segments/") and path.endswith("/readings"):
                sid=path.split("/")[2]; r=Reading(body["reading_id"],sid,body["sensor_id"],body["pressure_kpa"],body["flow_lps"],body["acoustic_db"],body["observed_at"]); return self._send(201,self.service.ingest_reading(token,r))
            if path.startswith("/segments/") and path.endswith("/work-orders"):
                return self._send(201,self.service.create_work_order(token,path.split("/")[2],body["alert_id"],body["assignee"],body.get("priority",3)))
            parts=path.strip("/").split("/")
            if path=="/routes":return self._send(201,self.service.routes.create_route(token,body["route_id"],body["district"],body.get("device_id"),body["stops"]))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="lock":return self._send(200,self.service.routes.lock_route(token,parts[1],body.get("stops"),body.get("note")))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="receipts":return self._send(202,self.service.routes.upload_receipts(token,parts[1],body["device_id"],body["receipts"],body.get("batch_id")))
            if len(parts)==3 and parts[0]=="conflicts" and parts[2]=="resolutions":return self._send(200,self.service.routes.resolve_conflict(token,parts[1],body))
            if len(parts)==3 and parts[0]=="receipt-batches" and parts[2]=="confirm":return self._send(200,self.service.routes.confirm_batch(token,parts[1]))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
def main():
    p=argparse.ArgumentParser(); p.add_argument("--database",default=":memory:"); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8080); a=p.parse_args(); Handler.service=NetworkService(a.database); Handler.service.bootstrap(); ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
if __name__=="__main__":main()
