"""班组路线批次、离线回执合并、冲突裁决与批次确认。

规则要点：
- 路线锁定后产生不可变的版本快照，记录工单顺序与预计时窗；
- 离线回执以 (设备号, 设备内序号) 幂等合并，同序号同内容视为重放；
- 缺号、同序号异文、同工单冲突结论、回执版本不匹配一律登记冲突，不静默覆盖；
- 冲突未全部裁决前不能确认批次；确认在单事务内驱动工单状态与材料扣减，任一失败整批回滚。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .models import Receipt, RouteStop, parse_time, utcnow
from .storage import audit, rows, transaction

WO_TRANSITIONS = {
    "open": {"assigned", "cancelled"},
    "assigned": {"in_progress", "cancelled"},
    "in_progress": {"completed", "blocked"},
    "blocked": {"in_progress", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _new(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


def _canonical(receipt: Receipt) -> str:
    body = {"seq": receipt.seq, **receipt.payload()}
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _receipt_hash(receipt: Receipt) -> str:
    return hashlib.sha256(_canonical(receipt).encode("utf-8")).hexdigest()


class RouteService:
    def __init__(self, db, auth):
        self.db = db
        self.auth = auth

    # ---------------- 路线批次 ----------------

    def create_route(self, token, route_id: str, district: str, device_id: str | None, stops: list[dict[str, Any]]):
        actor = self.auth.require(token, "dispatch")
        parsed = self._parse_stops(stops, nonempty=True)
        if not route_id.strip() or not district.strip():
            raise ValueError("route id and district are required")
        if self.db.execute("SELECT 1 FROM route_batches WHERE route_id=?", (route_id,)).fetchone():
            raise ValueError("route already exists")
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO route_batches VALUES(?,?,?,?,?,?,?,?,?)",
                (route_id, district, device_id, "draft", 0, actor.user_id, now, now, None),
            )
            self._write_stops(route_id, 0, parsed)
            audit(self.db, "route", route_id, "created", actor.user_id,
                  {"district": district, "device_id": device_id, "stops": len(parsed)})
        return self.route(token, route_id)

    @staticmethod
    def _parse_stops(stops, nonempty: bool) -> list[RouteStop]:
        if not isinstance(stops, list) or (nonempty and not stops):
            raise ValueError("route requires at least one stop")
        parsed: list[RouteStop] = []
        seen: set[str] = set()
        for item in stops:
            stop = RouteStop(str(item["work_order_id"]), str(item["planned_start"]), str(item["planned_end"]))
            stop.validate()
            if stop.work_order_id in seen:
                raise ValueError("work order appears twice in route")
            seen.add(stop.work_order_id)
            parsed.append(stop)
        return parsed

    def _write_stops(self, route_id: str, version: int, stops: list[RouteStop]) -> None:
        for position, stop in enumerate(stops, start=1):
            self.db.execute(
                "INSERT INTO route_stops VALUES(?,?,?,?,?,?)",
                (route_id, version, position, stop.work_order_id, stop.planned_start, stop.planned_end),
            )

    def route(self, token, route_id: str):
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM route_batches WHERE route_id=?", (route_id,)).fetchone()
        if not row:
            raise KeyError(route_id)
        result = dict(row)
        result["stops"] = rows(
            self.db,
            "SELECT position,work_order_id,planned_start,planned_end FROM route_stops "
            "WHERE route_id=? AND version=? ORDER BY position",
            (route_id, row["current_version"]),
        )
        return result

    def lock_route(self, token, route_id: str, stops=None, note: str | None = None):
        actor = self.auth.require(token, "dispatch")
        row = self.db.execute("SELECT * FROM route_batches WHERE route_id=?", (route_id,)).fetchone()
        if not row:
            raise KeyError(route_id)
        if row["status"] not in {"draft", "locked"}:
            raise ValueError("only draft or locked routes can be re-issued")
        if stops is None:
            if row["status"] == "locked":
                raise ValueError("revising a locked route requires a new stop list")
            existing = rows(
                self.db,
                "SELECT work_order_id,planned_start,planned_end FROM route_stops WHERE route_id=? AND version=0 ORDER BY position",
                (route_id,),
            )
            if not existing:
                raise ValueError("route has no stops")
            stops = existing
        parsed = self._parse_stops(stops, nonempty=True)
        version = row["current_version"] + 1
        if row["status"] == "locked":
            current = [
                (r["work_order_id"], r["planned_start"], r["planned_end"])
                for r in rows(
                    self.db,
                    "SELECT work_order_id,planned_start,planned_end FROM route_stops WHERE route_id=? AND version=? ORDER BY position",
                    (route_id, row["current_version"]),
                )
            ]
            candidate = [(s.work_order_id, s.planned_start, s.planned_end) for s in parsed]
            if current == candidate:
                raise ValueError("new route version is identical to the current one")
        with transaction(self.db):
            for stop in parsed:
                wo = self.db.execute("SELECT status FROM work_orders WHERE work_order_id=?", (stop.work_order_id,)).fetchone()
                if not wo:
                    raise KeyError(stop.work_order_id)
            self._write_stops(route_id, version, parsed)
            self.db.execute(
                "INSERT INTO route_versions VALUES(?,?,?,?,?,?)",
                (route_id, version, note, actor.user_id, utcnow(),
                 json.dumps([{"position": i, **s.__dict__} for i, s in enumerate(parsed, 1)], ensure_ascii=False, sort_keys=True)),
            )
            # 路线锁定即把工单派给班组：open -> assigned。
            for stop in parsed:
                wo = self.db.execute("SELECT status FROM work_orders WHERE work_order_id=?", (stop.work_order_id,)).fetchone()
                if wo["status"] == "open":
                    self.db.execute("UPDATE work_orders SET status='assigned',updated_at=? WHERE work_order_id=?",
                                    (utcnow(), stop.work_order_id))
                    audit(self.db, "work_order", stop.work_order_id, "transition", actor.user_id,
                          {"from": "open", "to": "assigned", "reason": f"route {route_id} v{version} locked"})
            self.db.execute("UPDATE route_batches SET status='locked',current_version=?,updated_at=? WHERE route_id=?",
                            (version, utcnow(), route_id))
            audit(self.db, "route", route_id, "locked" if version == 1 else "revised", actor.user_id,
                  {"version": version, "note": note, "stops": len(parsed)})
        return self.route(token, route_id)

    def route_versions(self, token, route_id: str):
        self.auth.require(token, "read")
        if not self.db.execute("SELECT 1 FROM route_batches WHERE route_id=?", (route_id,)).fetchone():
            raise KeyError(route_id)
        return rows(
            self.db,
            "SELECT route_id,version,note,created_by,created_at FROM route_versions WHERE route_id=? ORDER BY version",
            (route_id,),
        )

    def route_version_detail(self, token, route_id: str, version: int):
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT * FROM route_versions WHERE route_id=? AND version=?", (route_id, version)
        ).fetchone()
        if not row:
            raise KeyError(f"{route_id}@v{version}")
        result = {k: row[k] for k in row.keys() if k != "stops_json"}
        result["stops"] = json.loads(row["stops_json"])
        return result

    # ---------------- 离线回执 ----------------

    def upload_receipts(self, token, route_id: str, device_id: str, receipts: list[dict[str, Any]],
                        batch_id: str | None = None):
        actor = self.auth.require(token, "receipt")
        route = self.db.execute("SELECT * FROM route_batches WHERE route_id=?", (route_id,)).fetchone()
        if not route:
            raise KeyError(route_id)
        if route["status"] != "locked":
            raise ValueError("route must be locked before receipts are uploaded")
        if not device_id.strip():
            raise ValueError("device id is required")
        if not receipts:
            raise ValueError("at least one receipt is required")
        parsed = [Receipt.from_dict(item) for item in receipts]
        for receipt in parsed:
            receipt.validate()
        stop_orders = {
            r["work_order_id"]: r["position"]
            for r in rows(self.db, "SELECT work_order_id,position FROM route_stops WHERE route_id=? AND version=?",
                          (route_id, route["current_version"]))
        }
        for receipt in parsed:
            if receipt.work_order_id not in stop_orders:
                raise ValueError(f"receipt seq {receipt.seq} references work order outside the locked route")

        with transaction(self.db):
            batch = self._get_or_create_batch(batch_id, route, device_id, actor.user_id, [r.seq for r in parsed])
            bid = batch["batch_id"]
            if batch["status"] == "confirmed":
                # 幂等重放：已确认批次只接受完全一致的回执重传，不改动任何状态。
                for receipt in parsed:
                    existing = self.db.execute(
                        "SELECT payload_hash,batch_id FROM offline_receipts WHERE device_id=? AND seq=?",
                        (device_id, receipt.seq),
                    ).fetchone()
                    if not existing or existing["payload_hash"] != _receipt_hash(receipt) or existing["batch_id"] != bid:
                        raise ValueError("batch already confirmed")
                audit(self.db, "receipt_batch", bid, "replayed", actor.user_id, {"receipts": len(parsed)})
                return {"batch_id": bid, "duplicate": True, "inserted": 0, "duplicate_receipts": len(parsed),
                        "conflicts": [], "status": "confirmed"}

            inserted = duplicate_receipts = 0
            new_conflicts: list[str] = []
            for receipt, digest in ((r, _receipt_hash(r)) for r in parsed):
                existing = self.db.execute(
                    "SELECT receipt_id,payload_hash,batch_id FROM offline_receipts WHERE device_id=? AND seq=?",
                    (device_id, receipt.seq),
                ).fetchone()
                if existing:
                    if existing["payload_hash"] == digest:
                        duplicate_receipts += 1
                        if existing["batch_id"] != bid:
                            raise ValueError(f"seq {receipt.seq} already belongs to another batch")
                        continue
                    cid = self._register_conflict(
                        route_id, bid, "duplicate_seq", receipt.seq, receipt.work_order_id,
                        {"incoming_hash": digest, "stored_hash": existing["payload_hash"],
                         "stored_receipt_id": existing["receipt_id"]}, actor.user_id)
                    if cid:
                        new_conflicts.append(cid)
                    continue
                receipt_id = _new("rcpt-")
                self.db.execute(
                    "INSERT INTO offline_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, route_id, bid, device_id, receipt.seq, receipt.work_order_id,
                     digest, _canonical(receipt), "received", None, utcnow()),
                )
                inserted += 1
                if receipt.route_version is not None and receipt.route_version != route["current_version"]:
                    cid = self._register_conflict(
                        route_id, bid, "version_mismatch", receipt.seq, receipt.work_order_id,
                        {"receipt_version": receipt.route_version, "current_version": route["current_version"]},
                        actor.user_id)
                    if cid:
                        new_conflicts.append(cid)

            self._detect_gaps(route_id, bid, device_id, actor.user_id, new_conflicts)
            self._detect_conclusion_conflicts(route_id, bid, actor.user_id, new_conflicts)
            status = "open" if self._open_conflict_count(bid) else "ready"
            span = self.db.execute(
                "SELECT min(seq),max(seq) FROM offline_receipts WHERE batch_id=?", (bid,)
            ).fetchone()
            self.db.execute("UPDATE receipt_batches SET status=?,first_seq=?,last_seq=?,detail=NULL WHERE batch_id=?",
                            (status, span[0], span[1], bid))
            audit(self.db, "receipt_batch", bid, "merged", actor.user_id,
                  {"inserted": inserted, "duplicate": duplicate_receipts, "conflicts": new_conflicts, "status": status})
        return {"batch_id": bid, "duplicate": False, "inserted": inserted, "duplicate_receipts": duplicate_receipts,
                "conflicts": new_conflicts, "status": status}

    def _get_or_create_batch(self, batch_id, route, device_id: str, actor: str, seqs: list[int] | None = None):
        if batch_id:
            row = self.db.execute("SELECT * FROM receipt_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row:
                if row["route_id"] != route["route_id"] or row["device_id"] != device_id:
                    raise ValueError("batch id is bound to another route or device")
                return row
        elif seqs:
            # 未带批次号的重传：优先按设备序号找回原批次；
            # 否则并入同一路线+设备最近一个仍开放的批次，保证多次回传合并到同一批。
            found = rows(
                self.db,
                f"SELECT DISTINCT batch_id FROM offline_receipts WHERE device_id=? AND seq IN ({','.join('?' * len(seqs))})",
                (device_id, *seqs),
            )
            if len(found) > 1:
                raise ValueError("receipt seqs span multiple batches; specify batch_id")
            if found:
                row = self.db.execute("SELECT * FROM receipt_batches WHERE batch_id=?", (found[0]["batch_id"],)).fetchone()
                if row["route_id"] != route["route_id"]:
                    raise ValueError("receipts already merged under another route")
                return row
            open_batch = self.db.execute(
                "SELECT * FROM receipt_batches WHERE route_id=? AND device_id=? AND status!='confirmed' "
                "ORDER BY created_at DESC LIMIT 1", (route["route_id"], device_id)).fetchone()
            if open_batch:
                return open_batch
        if not batch_id:
            batch_id = _new("rb-")
        self.db.execute(
            "INSERT INTO receipt_batches VALUES(?,?,?,?,?,?,?,?,?,?)",
            (batch_id, route["route_id"], device_id, 0, 0, "ready", None, actor, utcnow(), None),
        )
        return self.db.execute("SELECT * FROM receipt_batches WHERE batch_id=?", (batch_id,)).fetchone()

    def _register_conflict(self, route_id, batch_id, conflict_type, seq, work_order_id, detail: dict[str, Any], actor: str):
        """按自然键登记冲突；已存在（含已裁决）则返回 None。"""
        existing = self.db.execute(
            "SELECT conflict_id,status FROM receipt_conflicts WHERE route_id=? AND conflict_type=? "
            "AND COALESCE(seq,-1)=? AND COALESCE(work_order_id,'')=? AND COALESCE(batch_id,'')=?",
            (route_id, conflict_type, seq if seq is not None else -1, work_order_id or "", batch_id or ""),
        ).fetchone()
        if existing:
            return None
        cid = _new("conf-")
        self.db.execute(
            "INSERT INTO receipt_conflicts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, route_id, batch_id, conflict_type, "open", seq, work_order_id,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), None, None, None, utcnow()),
        )
        audit(self.db, "conflict", cid, "registered", actor,
              {"type": conflict_type, "batch_id": batch_id, "seq": seq, "work_order_id": work_order_id})
        return cid

    def _detect_gaps(self, route_id, batch_id, device_id: str, actor: str, new_conflicts: list[str]):
        seqs = [r["seq"] for r in rows(
            self.db, "SELECT DISTINCT seq FROM offline_receipts WHERE route_id=? AND device_id=?",
            (route_id, device_id))]
        batch_seqs = [r["seq"] for r in rows(
            self.db, "SELECT DISTINCT seq FROM offline_receipts WHERE batch_id=?", (batch_id,))]
        if not seqs:
            return
        lo, hi = min(batch_seqs), max(batch_seqs)
        present = set(seqs)
        for missing in range(lo, hi + 1):
            if missing in present:
                # 迟到的缺号补齐：自动关闭仍开放的缺号冲突。
                filled = self.db.execute(
                    "SELECT conflict_id FROM receipt_conflicts WHERE route_id=? AND conflict_type='gap' AND seq=? AND status='open'",
                    (route_id, missing),
                ).fetchone()
                if filled:
                    self.db.execute(
                        "UPDATE receipt_conflicts SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE conflict_id=?",
                        (json.dumps({"decision": "filled"}), "system", utcnow(), filled["conflict_id"]),
                    )
                    audit(self.db, "conflict", filled["conflict_id"], "auto_resolved", "system",
                          {"reason": "late receipt arrived"})
                continue
            cid = self._register_conflict(route_id, batch_id, "gap", missing, None,
                                          {"device_id": device_id}, actor)
            if cid:
                new_conflicts.append(cid)

    def _detect_conclusion_conflicts(self, route_id, batch_id, actor: str, new_conflicts: list[str]):
        # 路线范围内检测：不同批次（设备分多次回传）对同一工单给出互斥结论同样要留给主管。
        groups = rows(
            self.db,
            "SELECT r.work_order_id AS work_order_id,r.seq AS seq,r.payload AS payload,b.batch_id AS batch_id,b.status AS batch_status "
            "FROM offline_receipts r JOIN receipt_batches b ON b.batch_id=r.batch_id "
            "WHERE r.route_id=? AND json_extract(r.payload,'$.event_type')='completion'",
            (route_id,),
        )
        by_wo: dict[str, dict[str, dict[int, str]]] = {}
        for row in groups:
            conclusion = json.loads(row["payload"])["conclusion"]
            by_wo.setdefault(row["work_order_id"], {}).setdefault(row["batch_id"], {})[row["seq"]] = conclusion
        for work_order_id, per_batch in by_wo.items():
            choices: dict[int, str] = {}
            for batch_choices in per_batch.values():
                choices.update(batch_choices)
            if len(set(choices.values())) <= 1:
                continue
            for bid, batch_choices in per_batch.items():
                batch_status = self.db.execute("SELECT status FROM receipt_batches WHERE batch_id=?", (bid,)).fetchone()["status"]
                if batch_status == "confirmed":
                    continue  # 已确认批次不可改写；冲突挂到仍开放的批次上由主管裁决
                cid = self._register_conflict(route_id, bid, "conclusion_conflict", None, work_order_id,
                                              {"choices": batch_choices, "route_choices": choices}, actor)
                if cid:
                    new_conflicts.append(cid)

    def _open_conflict_count(self, batch_id: str) -> int:
        return self.db.execute(
            "SELECT count(*) FROM receipt_conflicts WHERE batch_id=? AND status='open'", (batch_id,)
        ).fetchone()[0]

    # ---------------- 冲突与裁决 ----------------

    def conflicts(self, token, route_id: str, status: str | None = None):
        self.auth.require(token, "read")
        query = "SELECT * FROM receipt_conflicts WHERE route_id=?"
        args: list[Any] = [route_id]
        if status:
            query += " AND status=?"
            args.append(status)
        query += " ORDER BY created_at,conflict_id"
        result = rows(self.db, query, tuple(args))
        for row in result:
            row["detail"] = json.loads(row["detail"])
            row["resolution"] = json.loads(row["resolution"]) if row["resolution"] else None
        return result

    def resolve_conflict(self, token, conflict_id: str, resolution: dict[str, Any]):
        actor = self.auth.require(token, "arbitrate")
        row = self.db.execute("SELECT * FROM receipt_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
        if not row:
            raise KeyError(conflict_id)
        if row["status"] != "open":
            raise ValueError("conflict is already resolved")
        detail = json.loads(row["detail"])
        ctype = row["conflict_type"]
        if ctype == "conclusion_conflict":
            winner = resolution.get("winning_seq")
            choices = detail["choices"]
            if not isinstance(winner, int) or winner not in {int(s) for s in choices}:
                raise ValueError("resolution must select one of the conflicting receipt seqs as winning_seq")
            stored = json.dumps({"decision": "winning_seq", "winning_seq": winner}, sort_keys=True)
        elif ctype in {"gap", "version_mismatch"}:
            if resolution.get("decision") != "waive":
                raise ValueError(f"{ctype} resolution must waive the discrepancy to proceed")
            stored = json.dumps({"decision": "waive", "note": resolution.get("note")}, ensure_ascii=False, sort_keys=True)
        elif ctype == "duplicate_seq":
            if resolution.get("decision") != "keep_stored":
                raise ValueError("duplicate_seq resolution must confirm keep_stored")
            stored = json.dumps({"decision": "keep_stored", "note": resolution.get("note")}, ensure_ascii=False, sort_keys=True)
        else:  # pragma: no cover - 防御未知类型
            raise ValueError(f"unknown conflict type {ctype}")
        with transaction(self.db):
            self.db.execute(
                "UPDATE receipt_conflicts SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE conflict_id=?",
                (stored, actor.user_id, utcnow(), conflict_id),
            )
            if row["batch_id"]:
                status = "open" if self._open_conflict_count(row["batch_id"]) else "ready"
                self.db.execute("UPDATE receipt_batches SET status=? WHERE batch_id=?", (status, row["batch_id"]))
            audit(self.db, "conflict", conflict_id, "resolved", actor.user_id, json.loads(stored))
        return self.conflicts(token, row["route_id"])

    # ---------------- 批次确认（原子应用） ----------------

    def receipt_batches(self, token, route_id: str):
        self.auth.require(token, "read")
        return rows(self.db,
                    "SELECT batch_id,route_id,device_id,status,detail,created_by,created_at,confirmed_at "
                    "FROM receipt_batches WHERE route_id=? ORDER BY created_at", (route_id,))

    def receipts(self, token, route_id: str, batch_id: str | None = None):
        self.auth.require(token, "read")
        query = "SELECT receipt_id,batch_id,device_id,seq,work_order_id,status,applied_at,created_at,payload " \
                "FROM offline_receipts WHERE route_id=?"
        args: list[Any] = [route_id]
        if batch_id:
            query += " AND batch_id=?"
            args.append(batch_id)
        query += " ORDER BY device_id,seq"
        result = rows(self.db, query, tuple(args))
        for row in result:
            row["payload"] = json.loads(row["payload"])
        return result

    def confirm_batch(self, token, batch_id: str):
        actor = self.auth.require(token, "dispatch")
        batch = self.db.execute("SELECT * FROM receipt_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not batch:
            raise KeyError(batch_id)
        route_id = batch["route_id"]
        if batch["status"] == "confirmed":
            return {"batch_id": batch_id, "duplicate": True, "status": "confirmed"}
        open_conflicts = self.db.execute(
            "SELECT count(*) FROM receipt_conflicts WHERE batch_id=? AND status='open'", (batch_id,)
        ).fetchone()[0]
        if open_conflicts:
            raise ValueError("batch has unresolved conflicts")

        # 主管对冲突结论的裁决：路线范围内每个工单选定的获胜序号。
        winners: dict[str, int] = {}
        for row in rows(self.db,
                        "SELECT work_order_id,resolution FROM receipt_conflicts WHERE route_id=? AND conflict_type='conclusion_conflict' AND status='resolved'",
                        (route_id,)):
            winners[row["work_order_id"]] = json.loads(row["resolution"])["winning_seq"]

        receipt_rows = rows(self.db,
                            "SELECT * FROM offline_receipts WHERE batch_id=? AND status='received' ORDER BY seq",
                            (batch_id,))
        terminal = {"completed", "blocked", "cancelled"}
        summary = {"checkins": 0, "findings": 0, "completions": 0, "superseded": 0, "redundant": 0,
                   "defects": [], "materials": []}
        try:
            with transaction(self.db):
                for row in receipt_rows:
                    payload = json.loads(row["payload"])
                    wo = self.db.execute("SELECT status FROM work_orders WHERE work_order_id=?",
                                         (row["work_order_id"],)).fetchone()
                    if not wo:
                        raise ValueError(f"work order {row['work_order_id']} no longer exists")
                    event = payload["event_type"]
                    winning_seq = winners.get(row["work_order_id"])
                    if winning_seq is not None and winning_seq != row["seq"] and event == "completion":
                        # 主管裁决中落选的结论：不应用其状态与材料，留审计痕迹。
                        summary["superseded"] += 1
                        audit(self.db, "work_order", row["work_order_id"], "conclusion_superseded", actor.user_id,
                              {"seq": row["seq"], "winning_seq": winning_seq, "batch_id": batch_id})
                        self.db.execute(
                            "UPDATE offline_receipts SET status='applied',applied_at=? WHERE receipt_id=?",
                            (utcnow(), row["receipt_id"]))
                        continue
                    stale = wo["status"] in terminal
                    if event == "checkin":
                        if stale:
                            # 工单已由先前批次确认：迟到签到留痕但不回退状态，材料同样不再扣减。
                            summary["redundant"] += 1
                            audit(self.db, "work_order", row["work_order_id"], "redundant_checkin", actor.user_id,
                                  {"seq": row["seq"], "status": wo["status"], "batch_id": batch_id})
                        else:
                            if wo["status"] == "assigned":
                                self._transition(route_id, row["work_order_id"], "in_progress",
                                                 f"check-in {row['device_id']}#{row['seq']} by {payload['crew_member']}", actor.user_id)
                            summary["checkins"] += 1
                    elif event == "finding":
                        if stale:
                            summary["redundant"] += 1
                            audit(self.db, "work_order", row["work_order_id"], "late_finding_skipped", actor.user_id,
                                  {"seq": row["seq"], "status": wo["status"], "defect": payload["defect"], "batch_id": batch_id})
                        else:
                            defect = payload["defect"]
                            defect_id = _new("def-")
                            self.db.execute(
                                "INSERT INTO defects VALUES(?,?,?,?,?,?,?)",
                                (defect_id, row["work_order_id"], defect["code"], defect["severity"],
                                 defect.get("description"), row["receipt_id"], utcnow()),
                            )
                            summary["defects"].append(defect_id)
                            summary["findings"] += 1
                            audit(self.db, "defect", defect_id, "registered", actor.user_id,
                                  {"work_order_id": row["work_order_id"], "receipt_seq": row["seq"], **defect})
                    else:  # completion
                        target = payload["conclusion"]
                        if stale and wo["status"] != target:
                            if winners.get(row["work_order_id"]) == row["seq"]:
                                # 主管人工裁决推翻既有终态：直接改判并完整留痕。
                                self.db.execute("UPDATE work_orders SET status=?,updated_at=? WHERE work_order_id=?",
                                                (target, utcnow(), row["work_order_id"]))
                                audit(self.db, "work_order", row["work_order_id"], "arbitration_override", actor.user_id,
                                      {"from": wo["status"], "to": target, "winning_seq": row["seq"], "batch_id": batch_id})
                                summary["completions"] += 1
                            else:
                                # 未经主管裁决的互斥终态：不静默覆盖，整批失败留给主管处理。
                                raise ValueError(f"seq {row['seq']}: work order already {wo['status']}, conflicts with {target}")
                        elif stale:
                            summary["redundant"] += 1
                            audit(self.db, "work_order", row["work_order_id"], "redundant_completion", actor.user_id,
                                  {"seq": row["seq"], "status": target, "batch_id": batch_id})
                        else:
                            # 离线期间可能漏发签到：允许从 assigned 直接收敛，隐式补齐 in_progress。
                            if wo["status"] == "assigned":
                                self._transition(route_id, row["work_order_id"], "in_progress",
                                                 f"implicit check-in {row['device_id']}#{row['seq']}", actor.user_id)
                            self._transition(route_id, row["work_order_id"], target,
                                             f"offline completion {row['device_id']}#{row['seq']}", actor.user_id)
                            summary["completions"] += 1
                    # 过时的签到/发现不扣材料；其余独立回执（含同结论的重复完工）各自记账。
                    if not (stale and event in {"checkin", "finding"}):
                        for item in payload["materials"]:
                            rid, qty = item["resource_id"], int(item["quantity"])
                            available = self.db.execute("SELECT available FROM resources WHERE resource_id=?", (rid,)).fetchone()
                            if not available:
                                raise ValueError(f"seq {row['seq']}: unknown resource {rid}")
                            if available["available"] < qty:
                                raise ValueError(f"seq {row['seq']}: resource {rid} capacity exceeded")
                            usage_id = _new("use-")
                            self.db.execute(
                                "INSERT INTO material_usages VALUES(?,?,?,?,?,?)",
                                (usage_id, row["work_order_id"], rid, qty, row["receipt_id"], utcnow()),
                            )
                            self.db.execute("UPDATE resources SET available=available-? WHERE resource_id=?", (qty, rid))
                            summary["materials"].append({"resource_id": rid, "quantity": qty})
                            audit(self.db, "resource", rid, "consumed", actor.user_id,
                                  {"work_order_id": row["work_order_id"], "quantity": qty, "receipt_seq": row["seq"]})
                    self.db.execute(
                        "UPDATE offline_receipts SET status='applied',applied_at=? WHERE receipt_id=?",
                        (utcnow(), row["receipt_id"]),
                    )
                self.db.execute(
                    "UPDATE receipt_batches SET status='confirmed',detail=NULL,confirmed_at=? WHERE batch_id=?",
                    (utcnow(), batch_id),
                )
                audit(self.db, "receipt_batch", batch_id, "confirmed", actor.user_id, summary)
        except Exception as exc:
            # 任何一条失败：事务已整体回滚，批次保持开放并记录失败原因，等待主管处理。
            with transaction(self.db):
                self.db.execute("UPDATE receipt_batches SET status='failed',detail=? WHERE batch_id=?",
                                (str(exc), batch_id))
                audit(self.db, "receipt_batch", batch_id, "confirm_failed", actor.user_id, {"reason": str(exc)})
            raise
        summary.update({"batch_id": batch_id, "duplicate": False, "status": "confirmed"})
        return summary

    def _transition(self, route_id: str, work_order_id: str, target: str, reason: str, actor: str) -> None:
        current = self.db.execute("SELECT status FROM work_orders WHERE work_order_id=?", (work_order_id,)).fetchone()
        if target not in WO_TRANSITIONS.get(current["status"], set()):
            raise ValueError(f"invalid work order transition {current['status']} -> {target}")
        self.db.execute("UPDATE work_orders SET status=?,updated_at=? WHERE work_order_id=?",
                        (target, utcnow(), work_order_id))
        audit(self.db, "work_order", work_order_id, "transition", actor,
              {"from": current["status"], "to": target, "reason": reason, "route_id": route_id})
