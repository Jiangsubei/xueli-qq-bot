from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AIResponse:
    """Normalized response payload returned by the AI client facade."""

    content: str
    segments: Optional[List[str]] = None
    usage: Optional[Dict[str, int]] = None
    model: str = ""
    finish_reason: str = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    raw_response: Optional[Dict[str, Any]] = None


class AIAPIError(Exception):
    """Raised when an OpenAI-compatible upstream request fails."""
