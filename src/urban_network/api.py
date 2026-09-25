"""依赖标准库的 JSON HTTP API。"""
from __future__ import annotations
import argparse,json
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import parse_qs,urlsplit
from .errors import ServiceError
from .models import Reading,Segment
from .service import NetworkService
class Handler(BaseHTTPRequestHandler):
    service=NetworkService()
    def _send(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _token(self):return self.headers.get("Authorization","").removeprefix("Bearer ")
    def _parts(self):return [p for p in urlsplit(self.path).path.split("/") if p]
    def _query(self):return parse_qs(urlsplit(self.path).query)
    def do_GET(self):
        try:
            parts=self._parts(); query=self._query()
            if parts==["health"]:return self._send(200,{"status":"ok","service":"urban-network"})
            if len(parts)==3 and parts[0]=="segments" and parts[2]=="risk":return self._send(200,self.service.risk_report(self._token(),parts[1]))
            if len(parts)==2 and parts[0]=="segments":return self._send(200,self.service.segment(self._token(),parts[1]))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="versions":return self._send(200,self.service.route_versions(self._token(),parts[1]))
            if len(parts)==2 and parts[0]=="routes":
                version=query.get("version",[None])[0]; return self._send(200,self.service.route(self._token(),parts[1],int(version) if version else None))
            if len(parts)==2 and parts[0]=="receipt-batches":return self._send(200,self.service.receipt_batch(self._token(),parts[1]))
            if len(parts)==2 and parts[0]=="receipt-conflicts":return self._send(200,self.service.conflict(self._token(),int(parts[1])))
            if parts==["receipt-conflicts"]:return self._send(200,{"conflicts":self.service.list_conflicts(self._token(),query.get("route_id",[None])[0],query.get("status",[None])[0])})
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except ServiceError as e:return self._send(e.status,{"error":str(e),"code":e.code})
        except Exception as e:return self._send(400,{"error":str(e)})
    def do_POST(self):
        try:
            body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            if urlsplit(self.path).path=="/login":return self._send(200,{"token":self.service.auth.login(body["user_id"],body["password"])})
            token=self._token(); parts=self._parts()
            if parts==["segments"]:return self._send(201,self.service.register_segment(token,Segment(body["segment_id"],body["district"],body["network_type"],body["length_m"],body["criticality"])))
            if len(parts)==3 and parts[0]=="segments" and parts[2]=="readings":
                r=Reading(body["reading_id"],parts[1],body["sensor_id"],body["pressure_kpa"],body["flow_lps"],body["acoustic_db"],body["observed_at"]); return self._send(201,self.service.ingest_reading(token,r))
            if len(parts)==3 and parts[0]=="segments" and parts[2]=="work-orders":
                return self._send(201,self.service.create_work_order(token,parts[1],body["alert_id"],body["assignee"],body.get("priority",3)))
            if parts==["routes"]:return self._send(201,self.service.create_route(token,body["route_id"],body["district"],body["stops"],body.get("note")))
            if len(parts)==3 and parts[0]=="routes" and parts[2]=="revisions":return self._send(201,self.service.revise_route(token,parts[1],body["stops"],body.get("note")))
            if parts==["receipt-batches"]:return self._send(201,self.service.upload_receipts(token,body))
            if len(parts)==3 and parts[0]=="receipt-batches" and parts[2]=="confirm":return self._send(200,self.service.confirm_batch(token,parts[1]))
            if len(parts)==3 and parts[0]=="receipt-conflicts" and parts[2]=="resolve":return self._send(200,self.service.resolve_conflict(token,int(parts[1]),body["resolution"],body.get("note")))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except ServiceError as e:return self._send(e.status,{"error":str(e),"code":e.code})
        except Exception as e:return self._send(400,{"error":str(e)})
def main():
    p=argparse.ArgumentParser(); p.add_argument("--database",default=":memory:"); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8080); a=p.parse_args(); Handler.service=NetworkService(a.database); Handler.service.bootstrap(); HTTPServer((a.host,a.port),Handler).serve_forever()
if __name__=="__main__":main()
