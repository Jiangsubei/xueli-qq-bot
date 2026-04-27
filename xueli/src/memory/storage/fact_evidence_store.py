from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from src.core.models import FactEvidenceRecord, SoftUncertaintySignal


def _now_iso() -> str:
    return datetime.now().isoformat()


class FactEvidenceStore:
    """Persist memory dispute evidence and active soft uncertainty signals."""

    def __init__(self, base_path: str) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def add_record(self, record: FactEvidenceRecord) -> FactEvidenceRecord:
        payload = self._load_payload(record.user_id)
        data = asdict(record)
        if not data.get("record_id"):
            data["record_id"] = uuid4().hex
        if not data.get("created_at"):
            data["created_at"] = _now_iso()
        data["updated_at"] = _now_iso()
        payload["records"].append(data)
        self._save_payload(record.user_id, payload)
        return FactEvidenceRecord(**data)

    def build_signal(
        self,
        *,
        user_id: str,
        record: FactEvidenceRecord,
        ttl_hours: float,
    ) -> SoftUncertaintySignal:
        now = datetime.now()
        signal = SoftUncertaintySignal(
            signal_id=uuid4().hex,
            user_id=user_id,
            summary=record.summary,
            confidence=float(record.confidence or 0.0),
            conflict_type=record.conflict_type,
            action=record.action,
            active=True,
            source_memory_id=record.source_memory_id,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=max(1.0, float(ttl_hours or 1.0)))).isoformat(),
            metadata={
                "record_id": record.record_id,
                "reason": record.reason,
            },
        )
        payload = self._load_payload(user_id)
        payload["signals"].append(asdict(signal))
        self._save_payload(user_id, payload)
        return signal

    def list_records(self, user_id: str, *, memory_id: str = "") -> List[FactEvidenceRecord]:
        payload = self._load_payload(user_id)
        records = []
        for item in payload.get("records", []):
            if memory_id and str(item.get("source_memory_id") or "") != memory_id:
                continue
            records.append(FactEvidenceRecord(**item))
        return records

    def get_active_signals(self, user_id: str, *, limit: int = 3) -> List[SoftUncertaintySignal]:
        payload = self._load_payload(user_id)
        now = datetime.now()
        active: List[dict] = []
        result: List[SoftUncertaintySignal] = []
        for item in payload.get("signals", []):
            expires_at = self._parse_datetime(str(item.get("expires_at") or ""))
            is_active = bool(item.get("active", True))
            if not is_active or expires_at is None or expires_at < now:
                continue
            active.append(item)
            result.append(SoftUncertaintySignal(**item))
            if len(result) >= max(1, limit):
                break
        payload["signals"] = active
        self._save_payload(user_id, payload)
        return result

    def _file_path(self, user_id: str) -> Path:
        return self.base_path / f"{str(user_id or 'unknown').strip() or 'unknown'}.json"

    def _load_payload(self, user_id: str) -> dict:
        file_path = self._file_path(user_id)
        if not file_path.exists():
            return {"records": [], "signals": []}
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"records": [], "signals": []}

    async def _load_payload_async(self, user_id: str) -> dict:
        return await asyncio.to_thread(self._load_payload, user_id)

    def _save_payload(self, user_id: str, payload: dict) -> None:
        self._file_path(user_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _save_payload_async(self, user_id: str, payload: dict) -> None:
        await asyncio.to_thread(self._save_payload, user_id, payload)

    async def add_record_async(self, record: FactEvidenceRecord) -> FactEvidenceRecord:
        return await asyncio.to_thread(self.add_record, record)

    async def build_signal_async(
        self,
        *,
        user_id: str,
        record: FactEvidenceRecord,
        ttl_hours: float,
    ) -> SoftUncertaintySignal:
        return await asyncio.to_thread(self.build_signal, user_id=user_id, record=record, ttl_hours=ttl_hours)

    async def get_active_signals_async(self, user_id: str, *, limit: int = 3) -> List[SoftUncertaintySignal]:
        return await asyncio.to_thread(self.get_active_signals, user_id, limit=limit)

    def _parse_datetime(self, value: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
