from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProactiveShareStore:
    """Persist pending proactive share items with expiry and cooldown."""

    def __init__(self, base_path: str) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def pending_shares(self, *, max_count: int = 3, cooldown_hours: float = 6.0, time_range_start: str = "09:00", time_range_end: str = "22:00") -> List[Dict[str, Any]]:
        payload = self._load_payload()
        now_dt = datetime.now(timezone.utc)
        if not self._within_time_range(now_dt, time_range_start, time_range_end):
            return []
        items = list(payload.get("items", []) if isinstance(payload.get("items"), list) else [])
        valid: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            expires_at = str(item.get("expires_at", ""))
            if expires_at and _now_iso() > expires_at:
                continue
            if cooldown_hours > 0:
                last_sent = str(item.get("last_sent_at", ""))
                if last_sent and not self._is_cooldown_passed(last_sent, cooldown_hours):
                    continue
            valid.append(item)
        self._save_payload({"items": valid, "cooldown_until": payload.get("cooldown_until", "")})
        return valid[:max_count]

    def add_share(self, *, content: str, source: str = "insight", expires_in_hours: float = 168.0) -> Dict[str, Any]:
        payload = self._load_payload()
        items = list(payload.get("items", []) if isinstance(payload.get("items"), list) else [])
        item = {
            "id": uuid4().hex,
            "content": content,
            "source": source,
            "created_at": _now_iso(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=max(1.0, expires_in_hours))).isoformat(),
            "last_sent_at": "",
        }
        items.append(item)
        self._save_payload({"items": items, "cooldown_until": payload.get("cooldown_until", "")})
        return item

    def mark_sent(self, item_id: str) -> None:
        payload = self._load_payload()
        items = list(payload.get("items", []) if isinstance(payload.get("items"), list) else [])
        for item in items:
            if isinstance(item, dict) and str(item.get("id", "")) == item_id:
                item["last_sent_at"] = _now_iso()
                break
        self._save_payload(payload)

    def count_sent_today(self) -> int:
        payload = self._load_payload()
        items = list(payload.get("items", []) if isinstance(payload.get("items"), list) else [])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(
            1
            for item in items
            if isinstance(item, dict)
            and str(item.get("last_sent_at", "") or "").startswith(today)
        )

    def set_global_cooldown(self, hours: float = 6.0) -> None:
        payload = self._load_payload()
        cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=max(0.0, hours))).isoformat()
        self._save_payload({"items": payload.get("items", []), "cooldown_until": cooldown_until})

    def is_global_cooldown_active(self) -> bool:
        payload = self._load_payload()
        cooldown = str(payload.get("cooldown_until", ""))
        if not cooldown:
            return False
        return _now_iso() < cooldown

    def _file_path(self) -> Path:
        return self.base_path / "proactive_shares.json"

    def _load_payload(self) -> Dict[str, Any]:
        file_path = self._file_path()
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"items": [], "cooldown_until": ""}

    def _save_payload(self, payload: Dict[str, Any]) -> None:
        file_path = self._file_path()
        tmp_path = file_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, file_path)

    def _within_time_range(self, now_dt: datetime, time_range_start: str, time_range_end: str) -> bool:
        try:
            start_h, start_m = map(int, time_range_start.split(":"))
            end_h, end_m = map(int, time_range_end.split(":"))
        except (ValueError, TypeError):
            return True
        now_time = now_dt.hour * 60 + now_dt.minute
        range_start = start_h * 60 + start_m
        range_end = end_h * 60 + end_m
        if range_end < range_start:
            return now_time >= range_start or now_time <= range_end
        return range_start <= now_time <= range_end

    def _is_cooldown_passed(self, last_sent_at: str, cooldown_hours: float) -> bool:
        try:
            dt = datetime.fromisoformat(last_sent_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        return (datetime.now(timezone.utc) - dt).total_seconds() >= cooldown_hours * 3600
