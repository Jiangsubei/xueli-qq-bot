from __future__ import annotations

import json
import re
from typing import Any


def preview_text_for_log(text: Any, *, max_length: int = 200) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max(1, max_length - 3)].rstrip() + "..."


def preview_json_for_log(data: Any, *, max_length: int = 200) -> str:
    try:
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        serialized = str(data)
    return preview_text_for_log(serialized, max_length=max_length)
