from __future__ import annotations

from typing import Any, Dict


def window_display_text(item: Dict[str, Any]) -> str:
    """Format a window message item for display in prompt text.

    Uses merged_description for image context when available.
    Returns the standard placeholder '用户发送了空文本' when no text or image
    content is present, per AGENTS.md empty-text convention.
    """
    text = str(item.get("display_text") or item.get("text") or item.get("raw_text") or "").strip()
    raw_image_count = int(item.get("raw_image_count", item.get("image_count", 0)) or 0)
    has_image_indicator = bool(item.get("raw_has_image")) or raw_image_count > 0
    merged_desc = str(item.get("merged_description") or "").strip()
    if has_image_indicator and merged_desc:
        return f"{text} [图片] {merged_desc}" if text and text != "用户发送了空文本" else f"[图片] {merged_desc}"
    if text and text != "用户发送了空文本":
        return text
    if has_image_indicator:
        return "[图片]" if raw_image_count <= 1 else f"[图片 x{raw_image_count}]"
    return "用户发送了空文本"


__all__ = ["window_display_text"]
