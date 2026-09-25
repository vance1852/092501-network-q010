"""管段、读数、告警、工单和资源的领域模型。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

@dataclass(frozen=True)
class Segment:
    segment_id: str; district: str; network_type: str; length_m: float; criticality: int; status: str = "normal"
    def validate(self) -> None:
        if not self.segment_id.strip() or not self.district.strip(): raise ValueError("segment id and district are required")
        if self.network_type not in {"water", "drainage", "gas"}: raise ValueError("unsupported network type")
        if self.length_m <= 0 or not 1 <= self.criticality <= 5: raise ValueError("segment dimensions are invalid")

@dataclass(frozen=True)
class Reading:
    reading_id: str; segment_id: str; sensor_id: str; pressure_kpa: float; flow_lps: float; acoustic_db: float; observed_at: str
    def validate(self) -> None:
        if not self.reading_id.strip() or not self.segment_id.strip() or not self.sensor_id.strip(): raise ValueError("reading identifiers are required")
        if min(self.pressure_kpa, self.flow_lps, self.acoustic_db) < 0: raise ValueError("reading values cannot be negative")
        parse_time(self.observed_at)

@dataclass(frozen=True)
class RouteStop:
    work_order_id: str; planned_start: str; planned_end: str
    def validate(self) -> None:
        if not self.work_order_id.strip(): raise ValueError("work order id is required")
        start=parse_time(self.planned_start); end=parse_time(self.planned_end)
        if start>=end: raise ValueError("planned time window is invalid")

@dataclass(frozen=True)
class Receipt:
    """设备在离线期间生成的一条班组回执。"""
    seq: int; work_order_id: str; crew_member: str; event_type: str; occurred_at: str
    conclusion: str | None = None; defect: dict[str,Any] | None = None
    materials: tuple[tuple[str,int],...] = (); route_version: int | None = None
    def validate(self) -> None:
        if not isinstance(self.seq,int) or isinstance(self.seq,bool) or self.seq<1: raise ValueError("receipt seq must be a positive integer")
        if not self.work_order_id.strip() or not self.crew_member.strip(): raise ValueError("work order id and crew member are required")
        if self.event_type not in {"checkin","finding","completion"}: raise ValueError("unsupported receipt event type")
        parse_time(self.occurred_at)
        if self.event_type=="checkin" and (self.conclusion is not None or self.defect is not None): raise ValueError("check-in receipt carries no conclusion or defect")
        if self.event_type=="finding":
            defect=self.defect or {}
            if self.conclusion is not None: raise ValueError("finding receipt carries no conclusion")
            if not str(defect.get("code","")).strip() or defect.get("severity") not in {"low","medium","high","critical"}: raise ValueError("finding receipt requires code and severity")
        if self.event_type=="completion" and self.conclusion not in {"completed","blocked"}: raise ValueError("completion receipt requires completed or blocked")
        for resource_id,quantity in self.materials:
            if not str(resource_id).strip() or not isinstance(quantity,int) or isinstance(quantity,bool) or quantity<=0: raise ValueError("material entries are invalid")
    @classmethod
    def from_dict(cls,data: dict[str,Any]) -> "Receipt":
        materials=tuple((str(m["resource_id"]),int(m["quantity"])) for m in data.get("materials",()))
        return cls(int(data["seq"]),str(data["work_order_id"]),str(data["crew_member"]),str(data["event_type"]),str(data["occurred_at"]),data.get("conclusion"),data.get("defect"),materials,data.get("route_version"))
    def payload(self) -> dict[str,Any]:
        return {"work_order_id":self.work_order_id,"crew_member":self.crew_member,"event_type":self.event_type,"occurred_at":self.occurred_at,"conclusion":self.conclusion,"defect":self.defect,"materials":[{"resource_id":r,"quantity":q} for r,q in self.materials]}

def as_dict(value: Any) -> dict[str, Any]:
    return {name: getattr(value, name) for name in value.__dataclass_fields__} if hasattr(value, "__dataclass_fields__") else dict(value)
