"""班组巡检路线编排、离线回执幂等合并、冲突裁决和批次确认。

路线按 (route_id, version) 版本化，修订把旧版本置为 superseded 并锁定新的工单顺序与预计时窗。
离线回执以设备生成的 sequence 在 (device, route, version) 维度幂等合并：同号同内容视为重放，
同号异内容登记 duplicate_sequence 冲突并保留双方内容，缺号登记 gap 冲突，同一工单出现相反
完工结论登记 work_order_conclusion 冲突。所有冲突由主管裁决，确认批次在单事务内驱动工单
状态并扣减资源，任一失败整批回滚。
"""
from __future__ import annotations
import hashlib,json,uuid
from .errors import Conflict,InvalidState,NotFound,ValidationFailed
from .models import parse_time,utcnow
from .storage import audit,rows,transaction

EVENT_TYPES={"check_in","complete","defect","material_use"}
CONCLUSIONS={"completed","blocked"}
RESOLUTIONS={"gap":{"dismissed"},"duplicate_sequence":{"keep_existing","keep_incoming"},"work_order_conclusion":{"completed","blocked"}}

def _sha256(obj): return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def _new_id(prefix): return prefix+"-"+uuid.uuid4().hex[:16]

class RouteOps:
    """依赖宿主服务的 db 与 auth 成员的路线与回执操作集合。"""
    # ---- 路线编排 ----
    def create_route(self,token,route_id,district,stops,note=None):
        actor=self.auth.require(token,"route_plan")
        if not str(route_id).strip() or not str(district).strip(): raise ValidationFailed("route id and district are required")
        if self.db.execute("SELECT 1 FROM routes WHERE route_id=?",(route_id,)).fetchone(): raise Conflict("route already exists")
        stops=self._validate_stops(stops,exclude_route=None); now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO routes VALUES(?,?,?,?,?,?,?)",(route_id,1,"active",district,actor.user_id,now,note))
            self._insert_stops(route_id,1,stops)
            audit(self.db,"route",route_id,"created",actor.user_id,{"version":1,"district":district,"stops":stops,"note":note})
        return self.route(token,route_id)
    def revise_route(self,token,route_id,stops,note=None):
        actor=self.auth.require(token,"route_plan")
        current=self.db.execute("SELECT * FROM routes WHERE route_id=? ORDER BY version DESC LIMIT 1",(route_id,)).fetchone()
        if not current: raise NotFound(route_id)
        if current["status"]!="active": raise InvalidState("route head is not active")
        if self.db.execute("SELECT 1 FROM receipt_batches WHERE route_id=? AND version=?",(route_id,current["version"])).fetchone(): raise InvalidState("route version already has receipt batches")
        stops=self._validate_stops(stops,exclude_route=route_id); new_version=current["version"]+1
        with transaction(self.db):
            self.db.execute("UPDATE routes SET status='superseded' WHERE route_id=? AND version=?",(route_id,current["version"]))
            self.db.execute("INSERT INTO routes VALUES(?,?,?,?,?,?,?)",(route_id,new_version,"active",current["district"],actor.user_id,utcnow(),note))
            self._insert_stops(route_id,new_version,stops)
            audit(self.db,"route",route_id,"revised",actor.user_id,{"from_version":current["version"],"version":new_version,"stops":stops,"note":note})
        return self.route(token,route_id,new_version)
    def route(self,token,route_id,version=None):
        self.auth.require(token,"read")
        row=self.db.execute("SELECT * FROM routes WHERE route_id=? ORDER BY version DESC LIMIT 1",(route_id,)).fetchone() if version is None else self.db.execute("SELECT * FROM routes WHERE route_id=? AND version=?",(route_id,version)).fetchone()
        if not row: raise NotFound(route_id)
        result=dict(row); result["stops"]=rows(self.db,"SELECT position,work_order_id,planned_start,planned_end FROM route_stops WHERE route_id=? AND version=? ORDER BY position",(route_id,row["version"]))
        return result
    def route_versions(self,token,route_id):
        self.auth.require(token,"read")
        versions=rows(self.db,"SELECT * FROM routes WHERE route_id=? ORDER BY version",(route_id,))
        if not versions: raise NotFound(route_id)
        for v in versions: v["stops"]=rows(self.db,"SELECT position,work_order_id,planned_start,planned_end FROM route_stops WHERE route_id=? AND version=? ORDER BY position",(route_id,v["version"]))
        return {"route_id":route_id,"versions":versions}
    def _validate_stops(self,stops,exclude_route):
        if not isinstance(stops,list) or not stops: raise ValidationFailed("route stops are required")
        normalized=[]; positions=set(); orders=set()
        for raw in stops:
            if not isinstance(raw,dict): raise ValidationFailed("route stop must be an object")
            try: position=int(raw["position"])
            except (KeyError,TypeError,ValueError): raise ValidationFailed("stop position must be an integer") from None
            work_order_id=str(raw.get("work_order_id","")).strip()
            if position<1 or not work_order_id: raise ValidationFailed("stop position and work order are required")
            try:
                start=parse_time(str(raw.get("planned_start",""))); end=parse_time(str(raw.get("planned_end","")))
            except ValueError: raise ValidationFailed("stop time window is invalid") from None
            if not start<end: raise ValidationFailed("stop time window is invalid")
            if position in positions: raise ValidationFailed("stop positions must be unique")
            if work_order_id in orders: raise ValidationFailed("work order appears twice in route")
            positions.add(position); orders.add(work_order_id)
            normalized.append({"position":position,"work_order_id":work_order_id,"planned_start":start.isoformat(),"planned_end":end.isoformat()})
        for stop in normalized:
            if not self.db.execute("SELECT 1 FROM work_orders WHERE work_order_id=?",(stop["work_order_id"],)).fetchone(): raise NotFound(stop["work_order_id"])
            bound=self.db.execute("SELECT s.route_id FROM route_stops s JOIN routes r ON r.route_id=s.route_id AND r.version=s.version WHERE s.work_order_id=? AND r.status='active'",(stop["work_order_id"],)).fetchone()
            if bound and bound[0]!=exclude_route: raise Conflict("work order already bound to an active route")
        return sorted(normalized,key=lambda s:s["position"])
    def _insert_stops(self,route_id,version,stops):
        for s in stops: self.db.execute("INSERT INTO route_stops VALUES(?,?,?,?,?,?)",(route_id,version,s["position"],s["work_order_id"],s["planned_start"],s["planned_end"]))
    # ---- 离线回执合并 ----
    def upload_receipts(self,token,payload):
        actor=self.auth.require(token,"receipt_upload")
        if not isinstance(payload,dict): raise ValidationFailed("receipt batch payload must be an object")
        batch_id=str(payload.get("receipt_batch_id","")).strip(); route_id=str(payload.get("route_id","")).strip(); device_id=str(payload.get("device_id","")).strip()
        if not batch_id or not route_id or not device_id: raise ValidationFailed("receipt batch id, route id and device id are required")
        try: version=int(payload.get("version"))
        except (TypeError,ValueError): raise ValidationFailed("route version must be an integer") from None
        route=self.db.execute("SELECT * FROM routes WHERE route_id=? AND version=?",(route_id,version)).fetchone()
        if not route: raise NotFound(f"{route_id}@{version}")
        if route["status"]!="active": raise InvalidState("route version is not active")
        entries=self._normalize_receipts(payload.get("receipts"))
        stop_orders={r[0] for r in self.db.execute("SELECT work_order_id FROM route_stops WHERE route_id=? AND version=?",(route_id,version)).fetchall()}
        for e in entries:
            if e["work_order_id"] not in stop_orders: raise ValidationFailed(f"work order {e['work_order_id']} is not part of the route")
            if e["event_type"]=="material_use" and not self.db.execute("SELECT 1 FROM resources WHERE resource_id=?",(e["resource_id"],)).fetchone(): raise NotFound(e["resource_id"])
        for e in entries: e["content_sha256"]=_sha256({k:e[k] for k in ("sequence","work_order_id","event_type","conclusion","defect_kind","defect_note","resource_id","quantity","occurred_at")})
        fingerprint=_sha256({"route_id":route_id,"version":version,"device_id":device_id,"receipts":[e["content_sha256"] for e in entries]})
        with transaction(self.db):
            existing=self.db.execute("SELECT * FROM receipt_batches WHERE receipt_batch_id=?",(batch_id,)).fetchone()
            if existing:
                if (existing["content_sha256"],existing["route_id"],existing["version"],existing["device_id"])!=(fingerprint,route_id,version,device_id): raise Conflict("receipt batch id reused with different content")
                return self._batch_view(existing,{"replay":True})
            now=utcnow(); merged=duplicates=0
            self.db.execute("INSERT INTO receipt_batches(receipt_batch_id,route_id,version,device_id,status,first_sequence,last_sequence,content_sha256,received_by,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(batch_id,route_id,version,device_id,"received",entries[0]["sequence"],entries[-1]["sequence"],fingerprint,actor.user_id,now))
            for e in entries:
                live=self.db.execute("SELECT * FROM receipts WHERE device_id=? AND route_id=? AND version=? AND sequence=? AND status IN ('pending','applied')",(device_id,route_id,version,e["sequence"])).fetchone()
                if live is None: self._insert_receipt(batch_id,device_id,route_id,version,e,"pending",now); merged+=1
                elif live["content_sha256"]==e["content_sha256"]: duplicates+=1
                else:
                    self._insert_receipt(batch_id,device_id,route_id,version,e,"duplicate",now); duplicates+=1
                    self._raise_conflict(batch_id,"duplicate_sequence",device_id=device_id,sequence=e["sequence"],work_order_id=e["work_order_id"],existing_sha=live["content_sha256"],incoming_sha=e["content_sha256"],detail={"route_id":route_id,"version":version,"device_id":device_id,"sequence":e["sequence"],"work_order_id":e["work_order_id"]})
            gaps=self._refresh_gaps(batch_id,device_id,route_id,version)
            self._refresh_conclusion_conflicts(batch_id,route_id,version,{e["work_order_id"] for e in entries})
            if self.db.execute("SELECT 1 FROM receipt_conflicts WHERE receipt_batch_id=? AND status='open'",(batch_id,)).fetchone(): self.db.execute("UPDATE receipt_batches SET status='blocked' WHERE receipt_batch_id=?",(batch_id,))
            batch=self.db.execute("SELECT * FROM receipt_batches WHERE receipt_batch_id=?",(batch_id,)).fetchone()
            audit(self.db,"receipt_batch",batch_id,"merged",actor.user_id,{"route_id":route_id,"version":version,"device_id":device_id,"merged":merged,"duplicates":duplicates,"gaps":gaps,"status":batch["status"]})
            return self._batch_view(batch,{"replay":False,"merged":merged,"duplicates":duplicates,"gaps":gaps})
    def confirm_batch(self,token,receipt_batch_id):
        actor=self.auth.require(token,"receipt_confirm")
        batch=self.db.execute("SELECT * FROM receipt_batches WHERE receipt_batch_id=?",(receipt_batch_id,)).fetchone()
        if not batch: raise NotFound(receipt_batch_id)
        if batch["status"]=="confirmed": return self._batch_view(batch,{"replay":True})
        if self.db.execute("SELECT 1 FROM receipt_conflicts WHERE route_id=? AND version=? AND status='open'",(batch["route_id"],batch["version"])).fetchone(): raise InvalidState("receipt batch has open conflicts")
        summary={"check_ins":0,"completions":0,"defects":0,"materials":0,"transitions":[]}
        try:
            with transaction(self.db):
                if self.db.execute("SELECT 1 FROM receipt_conflicts WHERE route_id=? AND version=? AND status='open'",(batch["route_id"],batch["version"])).fetchone(): raise InvalidState("receipt batch has open conflicts")
                pending=rows(self.db,"SELECT * FROM receipts WHERE receipt_batch_id=? AND status='pending' ORDER BY sequence",(receipt_batch_id,))
                if not pending: raise InvalidState("receipt batch has no pending receipts")
                for r in pending: self._apply_receipt(actor,r,summary)
                self.db.execute("UPDATE receipts SET status='applied' WHERE receipt_batch_id=? AND status='pending'",(receipt_batch_id,))
                self.db.execute("UPDATE receipt_batches SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE receipt_batch_id=?",(actor.user_id,utcnow(),receipt_batch_id))
                audit(self.db,"receipt_batch",receipt_batch_id,"confirmed",actor.user_id,summary)
        except (InvalidState,Conflict,NotFound) as e:
            with transaction(self.db):
                self.db.execute("UPDATE receipt_batches SET status='failed',fail_reason=? WHERE receipt_batch_id=?",(str(e),receipt_batch_id))
                audit(self.db,"receipt_batch",receipt_batch_id,"confirm-failed",actor.user_id,{"reason":str(e)})
            raise
        return self._batch_view(self.db.execute("SELECT * FROM receipt_batches WHERE receipt_batch_id=?",(receipt_batch_id,)).fetchone(),{"replay":False,**summary})
    def receipt_batch(self,token,receipt_batch_id):
        self.auth.require(token,"read")
        batch=self.db.execute("SELECT * FROM receipt_batches WHERE receipt_batch_id=?",(receipt_batch_id,)).fetchone()
        if not batch: raise NotFound(receipt_batch_id)
        return self._batch_view(batch)
    def receipt_batches(self,token,route_id,version=None):
        self.auth.require(token,"read")
        q="SELECT * FROM receipt_batches WHERE route_id=?"; args=[route_id]
        if version is not None: q+=" AND version=?"; args.append(version)
        return rows(self.db,q+" ORDER BY received_at,receipt_batch_id",args)
    # ---- 冲突裁决 ----
    def list_conflicts(self,token,route_id=None,status=None):
        self.auth.require(token,"read")
        q="SELECT * FROM receipt_conflicts WHERE 1=1"; args=[]
        if route_id: q+=" AND route_id=?"; args.append(route_id)
        if status: q+=" AND status=?"; args.append(status)
        return [self._conflict_dict(r) for r in self.db.execute(q+" ORDER BY conflict_id",args).fetchall()]
    def conflict(self,token,conflict_id):
        self.auth.require(token,"read")
        row=self.db.execute("SELECT * FROM receipt_conflicts WHERE conflict_id=?",(conflict_id,)).fetchone()
        if not row: raise NotFound(str(conflict_id))
        return self._conflict_dict(row)
    def resolve_conflict(self,token,conflict_id,resolution,note=None):
        actor=self.auth.require(token,"conflict_resolve")
        row=self.db.execute("SELECT * FROM receipt_conflicts WHERE conflict_id=?",(conflict_id,)).fetchone()
        if not row: raise NotFound(str(conflict_id))
        if row["status"]!="open": raise InvalidState("conflict is already resolved")
        kind=row["kind"]
        if resolution not in RESOLUTIONS[kind]: raise ValidationFailed("unsupported resolution for conflict kind")
        with transaction(self.db):
            if kind=="duplicate_sequence": self._resolve_duplicate(row,resolution)
            elif kind=="work_order_conclusion": self._resolve_conclusion(row,resolution)
            self.db.execute("UPDATE receipt_conflicts SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE conflict_id=?",(resolution,actor.user_id,utcnow(),conflict_id))
            audit(self.db,"receipt_conflict",str(conflict_id),"resolved",actor.user_id,{"kind":kind,"resolution":resolution,"note":note})
        return self.conflict(token,conflict_id)
    def _resolve_duplicate(self,row,resolution):
        key=(row["device_id"],row["route_id"],row["version"],row["sequence"])
        live=self.db.execute("SELECT * FROM receipts WHERE device_id=? AND route_id=? AND version=? AND sequence=? AND status IN ('pending','applied')",key).fetchone()
        dups=self.db.execute("SELECT * FROM receipts WHERE device_id=? AND route_id=? AND version=? AND sequence=? AND status='duplicate' ORDER BY receipt_id",key).fetchall()
        if resolution=="keep_existing":
            for d in dups: self.db.execute("UPDATE receipts SET status='void' WHERE receipt_id=?",(d["receipt_id"],))
            return
        if not live or not dups: raise InvalidState("conflict receipts are missing")
        if live["status"]!="pending": raise InvalidState("existing receipt is already applied")
        winner=dups[-1]
        self.db.execute("UPDATE receipts SET status='void' WHERE receipt_id=?",(live["receipt_id"],))
        for d in dups: self.db.execute("UPDATE receipts SET status='void' WHERE receipt_id=?",(d["receipt_id"],))
        self.db.execute("UPDATE receipts SET status='pending' WHERE receipt_id=?",(winner["receipt_id"],))
        self._refresh_conclusion_conflicts(row["receipt_batch_id"],row["route_id"],row["version"],{winner["work_order_id"]})
    def _resolve_conclusion(self,row,resolution):
        others=self.db.execute("SELECT receipt_id,status FROM receipts WHERE route_id=? AND version=? AND work_order_id=? AND event_type='complete' AND status IN ('pending','applied') AND conclusion<>?",(row["route_id"],row["version"],row["work_order_id"],resolution)).fetchall()
        if any(o["status"]=="applied" for o in others): raise InvalidState("conflicting receipt is already applied")
        for o in others: self.db.execute("UPDATE receipts SET status='void' WHERE receipt_id=?",(o["receipt_id"],))
    # ---- 内部辅助 ----
    def _normalize_receipts(self,raw):
        if not isinstance(raw,list) or not raw: raise ValidationFailed("receipts are required")
        entries=[]; seen=set()
        for item in raw:
            if not isinstance(item,dict): raise ValidationFailed("receipt must be an object")
            try: sequence=int(item["sequence"])
            except (KeyError,TypeError,ValueError): raise ValidationFailed("receipt sequence must be an integer") from None
            work_order_id=str(item.get("work_order_id","")).strip(); event_type=str(item.get("event_type","")).strip()
            if sequence<1 or not work_order_id: raise ValidationFailed("receipt sequence and work order are required")
            if event_type not in EVENT_TYPES: raise ValidationFailed("unsupported receipt event type")
            if sequence in seen: raise ValidationFailed("duplicate sequence inside receipt batch")
            seen.add(sequence)
            try: occurred_at=parse_time(str(item.get("occurred_at",""))).isoformat()
            except ValueError: raise ValidationFailed("receipt occurred_at is invalid") from None
            conclusion=item.get("conclusion"); defect_kind=defect_note=resource_id=None; quantity=None
            if event_type=="complete":
                if conclusion not in CONCLUSIONS: raise ValidationFailed("complete receipt requires a conclusion")
            else: conclusion=None
            if event_type=="defect":
                defect_kind=str(item.get("defect_kind","")).strip()
                if not defect_kind: raise ValidationFailed("defect receipt requires a kind")
                note=item.get("defect_note"); defect_note=None if note is None else str(note)
            if event_type=="material_use":
                resource_id=str(item.get("resource_id","")).strip()
                try: quantity=int(item.get("quantity"))
                except (TypeError,ValueError): raise ValidationFailed("material use requires a positive quantity") from None
                if not resource_id or quantity<1: raise ValidationFailed("material use requires resource and positive quantity")
            entries.append({"sequence":sequence,"work_order_id":work_order_id,"event_type":event_type,"conclusion":conclusion,"defect_kind":defect_kind,"defect_note":defect_note,"resource_id":resource_id,"quantity":quantity,"occurred_at":occurred_at})
        return sorted(entries,key=lambda e:e["sequence"])
    def _insert_receipt(self,batch_id,device_id,route_id,version,e,status,now):
        self.db.execute("INSERT INTO receipts(receipt_batch_id,device_id,route_id,version,sequence,work_order_id,event_type,conclusion,defect_kind,defect_note,resource_id,quantity,occurred_at,content_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(batch_id,device_id,route_id,version,e["sequence"],e["work_order_id"],e["event_type"],e["conclusion"],e["defect_kind"],e["defect_note"],e["resource_id"],e["quantity"],e["occurred_at"],e["content_sha256"],status,now))
    def _refresh_gaps(self,batch_id,device_id,route_id,version):
        known={r[0] for r in self.db.execute("SELECT DISTINCT sequence FROM receipts WHERE device_id=? AND route_id=? AND version=?",(device_id,route_id,version)).fetchall()}
        gaps=[]
        if known:
            for seq in range(min(known),max(known)+1):
                if seq not in known:
                    gaps.append(seq); self._raise_conflict(batch_id,"gap",device_id=device_id,sequence=seq,detail={"route_id":route_id,"version":version,"device_id":device_id,"sequence":seq})
        for row in self.db.execute("SELECT conflict_id,sequence FROM receipt_conflicts WHERE route_id=? AND version=? AND device_id=? AND kind='gap' AND status='open'",(route_id,version,device_id)).fetchall():
            if row["sequence"] in known:
                self.db.execute("UPDATE receipt_conflicts SET status='resolved',resolution='filled',resolved_by='system',resolved_at=? WHERE conflict_id=?",(utcnow(),row["conflict_id"]))
                audit(self.db,"receipt_conflict",str(row["conflict_id"]),"resolved","system",{"kind":"gap","resolution":"filled","sequence":row["sequence"]})
        return gaps
    def _refresh_conclusion_conflicts(self,batch_id,route_id,version,work_order_ids):
        for wid in sorted(work_order_ids):
            conclusions={r[0] for r in self.db.execute("SELECT DISTINCT conclusion FROM receipts WHERE route_id=? AND version=? AND work_order_id=? AND event_type='complete' AND status IN ('pending','applied')",(route_id,version,wid)).fetchall()}
            conclusions.discard(None)
            if len(conclusions)>1: self._raise_conflict(batch_id,"work_order_conclusion",work_order_id=wid,detail={"route_id":route_id,"version":version,"work_order_id":wid,"conclusions":sorted(conclusions)})
    def _raise_conflict(self,batch_id,kind,device_id=None,sequence=None,work_order_id=None,existing_sha=None,incoming_sha=None,detail=None):
        row=self.db.execute("SELECT conflict_id FROM receipt_conflicts WHERE route_id=? AND version=? AND kind=? AND status='open' AND COALESCE(device_id,'')=COALESCE(?,'') AND COALESCE(sequence,-1)=COALESCE(?,-1) AND COALESCE(work_order_id,'')=COALESCE(?,'')",(detail["route_id"],detail["version"],kind,device_id,sequence,work_order_id)).fetchone()
        now=utcnow()
        if row:
            self.db.execute("UPDATE receipt_conflicts SET receipt_batch_id=?,detail=?,existing_sha256=COALESCE(?,existing_sha256),incoming_sha256=COALESCE(?,incoming_sha256),raised_at=? WHERE conflict_id=?",(batch_id,json.dumps(detail,ensure_ascii=False,sort_keys=True),existing_sha,incoming_sha,now,row["conflict_id"]))
            return row["conflict_id"]
        cursor=self.db.execute("INSERT INTO receipt_conflicts(receipt_batch_id,route_id,version,device_id,kind,sequence,work_order_id,existing_sha256,incoming_sha256,detail,status,raised_at) VALUES(?,?,?,?,?,?,?,?,?,?,'open',?)",(batch_id,detail["route_id"],detail["version"],device_id,kind,sequence,work_order_id,existing_sha,incoming_sha,json.dumps(detail,ensure_ascii=False,sort_keys=True),now))
        audit(self.db,"receipt_conflict",str(cursor.lastrowid),"raised","system",{"kind":kind,**detail})
        return cursor.lastrowid
    def _apply_receipt(self,actor,r,summary):
        event=r["event_type"]; wid=r["work_order_id"]
        if event=="check_in":
            self._transition_for_receipt(actor,r,{"open","assigned"},"in_progress",summary); summary["check_ins"]+=1
        elif event=="complete":
            if r["conclusion"]=="completed": self._transition_for_receipt(actor,r,{"in_progress"},"completed",summary)
            else: self._transition_for_receipt(actor,r,{"open","assigned","in_progress"},"blocked",summary)
            summary["completions"]+=1
        elif event=="defect":
            defect_id=_new_id("defect")
            self.db.execute("INSERT INTO defects VALUES(?,?,?,?,?,?)",(defect_id,wid,r["receipt_id"],r["defect_kind"],r["defect_note"],utcnow()))
            audit(self.db,"defect",defect_id,"registered",actor.user_id,{"work_order_id":wid,"receipt_id":r["receipt_id"],"kind":r["defect_kind"]}); summary["defects"]+=1
        else:
            resource=self.db.execute("SELECT available FROM resources WHERE resource_id=?",(r["resource_id"],)).fetchone()
            if not resource: raise NotFound(r["resource_id"])
            if resource[0]<r["quantity"]: raise InvalidState("resource capacity exceeded")
            consumption_id=_new_id("consume")
            self.db.execute("UPDATE resources SET available=available-? WHERE resource_id=?",(r["quantity"],r["resource_id"]))
            self.db.execute("INSERT INTO material_consumption VALUES(?,?,?,?,?,?)",(consumption_id,r["resource_id"],wid,r["receipt_id"],r["quantity"],utcnow()))
            audit(self.db,"resource",r["resource_id"],"consumed",actor.user_id,{"work_order_id":wid,"receipt_id":r["receipt_id"],"quantity":r["quantity"]}); summary["materials"]+=1
    def _transition_for_receipt(self,actor,r,allowed_sources,target,summary):
        row=self.db.execute("SELECT status FROM work_orders WHERE work_order_id=?",(r["work_order_id"],)).fetchone()
        if not row: raise NotFound(r["work_order_id"])
        current=row[0]
        if current==target: return
        if current not in allowed_sources: raise InvalidState(f"work order {r['work_order_id']} cannot move from {current} to {target}")
        self.db.execute("UPDATE work_orders SET status=?,updated_at=? WHERE work_order_id=?",(target,utcnow(),r["work_order_id"]))
        audit(self.db,"work_order",r["work_order_id"],"receipt-transition",actor.user_id,{"from":current,"to":target,"receipt_id":r["receipt_id"],"receipt_batch_id":r["receipt_batch_id"]})
        summary["transitions"].append({"work_order_id":r["work_order_id"],"from":current,"to":target})
    def _batch_view(self,batch,extra=None):
        result=dict(batch)
        result["receipts"]=rows(self.db,"SELECT receipt_id,sequence,work_order_id,event_type,conclusion,defect_kind,defect_note,resource_id,quantity,occurred_at,status,content_sha256 FROM receipts WHERE receipt_batch_id=? ORDER BY sequence,receipt_id",(batch["receipt_batch_id"],))
        result["conflicts"]=[self._conflict_dict(r) for r in self.db.execute("SELECT * FROM receipt_conflicts WHERE route_id=? AND version=? AND status='open' ORDER BY conflict_id",(batch["route_id"],batch["version"])).fetchall()]
        if extra: result.update(extra)
        return result
    def _conflict_dict(self,row):
        result=dict(row); result["detail"]=json.loads(result["detail"]); return result
