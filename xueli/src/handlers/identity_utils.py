from __future__ import annotations

from typing import Any


def format_identity_label(user_id: Any, display_name: str = "") -> str:
    identifier = str(user_id or "").strip() or "unknown"
    name = str(display_name or "").strip()
    if name and name != identifier:
        return f"{identifier}（{name}）"
    return identifier
