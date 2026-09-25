"""SQLite 结构、事务和审计事件辅助函数。"""
from __future__ import annotations
import json, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS segments(segment_id TEXT PRIMARY KEY,district TEXT NOT NULL,network_type TEXT NOT NULL,length_m REAL NOT NULL,criticality INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS readings(reading_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL REFERENCES segments(segment_id),sensor_id TEXT NOT NULL,pressure_kpa REAL NOT NULL,flow_lps REAL NOT NULL,acoustic_db REAL NOT NULL,observed_at TEXT NOT NULL,UNIQUE(segment_id,sensor_id,observed_at));
CREATE TABLE IF NOT EXISTS alerts(alert_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL REFERENCES segments(segment_id),fingerprint TEXT NOT NULL UNIQUE,severity TEXT NOT NULL,score REAL NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,resolved_at TEXT);
CREATE TABLE IF NOT EXISTS work_orders(work_order_id TEXT PRIMARY KEY,segment_id TEXT NOT NULL,alert_id TEXT NOT NULL,assignee TEXT NOT NULL,status TEXT NOT NULL,priority INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resources(resource_id TEXT PRIMARY KEY,kind TEXT NOT NULL,district TEXT NOT NULL,capacity INTEGER NOT NULL,available INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(allocation_id TEXT PRIMARY KEY,resource_id TEXT NOT NULL,work_order_id TEXT NOT NULL,quantity INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(resource_id,work_order_id));
CREATE TABLE IF NOT EXISTS routes(route_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','superseded')),district TEXT NOT NULL,planned_by TEXT NOT NULL,planned_at TEXT NOT NULL,note TEXT,PRIMARY KEY(route_id,version));
CREATE TABLE IF NOT EXISTS route_stops(route_id TEXT NOT NULL,version INTEGER NOT NULL,position INTEGER NOT NULL,work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),planned_start TEXT NOT NULL,planned_end TEXT NOT NULL,PRIMARY KEY(route_id,version,position),UNIQUE(route_id,version,work_order_id));
CREATE TABLE IF NOT EXISTS receipt_batches(receipt_batch_id TEXT PRIMARY KEY,route_id TEXT NOT NULL,version INTEGER NOT NULL,device_id TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('received','blocked','confirmed','failed')),first_sequence INTEGER NOT NULL,last_sequence INTEGER NOT NULL,content_sha256 TEXT NOT NULL,fail_reason TEXT,received_by TEXT NOT NULL,received_at TEXT NOT NULL,confirmed_by TEXT,confirmed_at TEXT,FOREIGN KEY(route_id,version) REFERENCES routes(route_id,version));
CREATE TABLE IF NOT EXISTS receipts(receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,receipt_batch_id TEXT NOT NULL REFERENCES receipt_batches(receipt_batch_id),device_id TEXT NOT NULL,route_id TEXT NOT NULL,version INTEGER NOT NULL,sequence INTEGER NOT NULL,work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),event_type TEXT NOT NULL CHECK(event_type IN ('check_in','complete','defect','material_use')),conclusion TEXT CHECK(conclusion IS NULL OR conclusion IN ('completed','blocked')),defect_kind TEXT,defect_note TEXT,resource_id TEXT REFERENCES resources(resource_id),quantity INTEGER,occurred_at TEXT NOT NULL,content_sha256 TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','void','duplicate')),created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_receipts_lookup ON receipts(device_id,route_id,version,sequence);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_receipt_per_sequence ON receipts(device_id,route_id,version,sequence) WHERE status IN ('pending','applied');
CREATE TABLE IF NOT EXISTS defects(defect_id TEXT PRIMARY KEY,work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),receipt_id INTEGER NOT NULL REFERENCES receipts(receipt_id),kind TEXT NOT NULL,note TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_consumption(consumption_id TEXT PRIMARY KEY,resource_id TEXT NOT NULL REFERENCES resources(resource_id),work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),receipt_id INTEGER NOT NULL REFERENCES receipts(receipt_id),quantity INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipt_conflicts(conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,receipt_batch_id TEXT NOT NULL REFERENCES receipt_batches(receipt_batch_id),route_id TEXT NOT NULL,version INTEGER NOT NULL,device_id TEXT,kind TEXT NOT NULL CHECK(kind IN ('gap','duplicate_sequence','work_order_conclusion')),sequence INTEGER,work_order_id TEXT,existing_sha256 TEXT,incoming_sha256 TEXT,detail TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),resolution TEXT,resolved_by TEXT,resolved_at TEXT,raised_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
"""
def utcnow() -> str: return datetime.now(timezone.utc).isoformat()
def connect(path: str = ":memory:") -> sqlite3.Connection:
    db=sqlite3.connect(path,timeout=10); db.row_factory=sqlite3.Row; db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA journal_mode=WAL"); db.executescript(SCHEMA); db.commit(); return db
@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try: db.execute("BEGIN IMMEDIATE"); yield db; db.commit()
    except Exception: db.rollback(); raise
def audit(db, entity_type, entity_id, action, actor, payload):
    db.execute("INSERT INTO audit_events(entity_type,entity_id,action,actor,payload,created_at) VALUES(?,?,?,?,?,?)",(entity_type,entity_id,action,actor,json.dumps(payload,ensure_ascii=False,sort_keys=True),utcnow()))
def rows(db, query, args=()): return [dict(r) for r in db.execute(query,args).fetchall()]
