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
CREATE TABLE IF NOT EXISTS audit_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS route_batches(route_id TEXT PRIMARY KEY,district TEXT NOT NULL,device_id TEXT,status TEXT NOT NULL,current_version INTEGER NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,confirmed_at TEXT);
CREATE TABLE IF NOT EXISTS route_stops(route_id TEXT NOT NULL,version INTEGER NOT NULL,position INTEGER NOT NULL,work_order_id TEXT NOT NULL,planned_start TEXT NOT NULL,planned_end TEXT NOT NULL,PRIMARY KEY(route_id,version,position));
CREATE TABLE IF NOT EXISTS route_versions(route_id TEXT NOT NULL,version INTEGER NOT NULL,note TEXT,created_by TEXT NOT NULL,created_at TEXT NOT NULL,stops_json TEXT NOT NULL,PRIMARY KEY(route_id,version));
CREATE TABLE IF NOT EXISTS receipt_batches(batch_id TEXT PRIMARY KEY,route_id TEXT NOT NULL,device_id TEXT NOT NULL,first_seq INTEGER NOT NULL,last_seq INTEGER NOT NULL,status TEXT NOT NULL,detail TEXT,created_by TEXT NOT NULL,created_at TEXT NOT NULL,confirmed_at TEXT);
CREATE TABLE IF NOT EXISTS offline_receipts(receipt_id TEXT PRIMARY KEY,route_id TEXT NOT NULL,batch_id TEXT NOT NULL,device_id TEXT NOT NULL,seq INTEGER NOT NULL,work_order_id TEXT NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,applied_at TEXT,created_at TEXT NOT NULL,UNIQUE(device_id,seq));
CREATE TABLE IF NOT EXISTS receipt_conflicts(conflict_id TEXT PRIMARY KEY,route_id TEXT NOT NULL,batch_id TEXT,conflict_type TEXT NOT NULL,status TEXT NOT NULL,seq INTEGER,work_order_id TEXT,detail TEXT NOT NULL,resolution TEXT,resolved_by TEXT,resolved_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS defects(defect_id TEXT PRIMARY KEY,work_order_id TEXT NOT NULL,code TEXT NOT NULL,severity TEXT NOT NULL,description TEXT,source_receipt_id TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_usages(usage_id TEXT PRIMARY KEY,work_order_id TEXT NOT NULL,resource_id TEXT NOT NULL,quantity INTEGER NOT NULL,source_receipt_id TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(source_receipt_id,resource_id));
"""
def utcnow() -> str: return datetime.now(timezone.utc).isoformat()
def connect(path: str = ":memory:") -> sqlite3.Connection:
    db=sqlite3.connect(path,timeout=10,check_same_thread=False); db.row_factory=sqlite3.Row; db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA journal_mode=WAL"); db.executescript(SCHEMA); db.commit(); return db
@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try: db.execute("BEGIN IMMEDIATE"); yield db; db.commit()
    except Exception: db.rollback(); raise
def audit(db, entity_type, entity_id, action, actor, payload):
    db.execute("INSERT INTO audit_events(entity_type,entity_id,action,actor,payload,created_at) VALUES(?,?,?,?,?,?)",(entity_type,entity_id,action,actor,json.dumps(payload,ensure_ascii=False,sort_keys=True),utcnow()))
def rows(db, query, args=()): return [dict(r) for r in db.execute(query,args).fetchall()]
