from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any


class PromptTemplateLoader:
    """Lightweight file-based prompt template loader."""

    def __init__(self, *, locale: str = "zh-CN") -> None:
        self.locale = locale
        self._formatter = Formatter()

    @property
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "prompts" / self.locale

    @lru_cache(maxsize=32)
    def load(self, name: str) -> str:
        template_path = self.templates_dir / name
        if not template_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_path}")
        return template_path.read_text(encoding="utf-8")

    def render(self, name: str, **fields: Any) -> str:
        template = self.load(name)
        missing = sorted(
            {
                field_name
                for _, field_name, _, _ in self._formatter.parse(template)
                if field_name and field_name not in fields
            }
        )
        if missing:
            raise KeyError(f"Prompt template '{name}' missing fields: {', '.join(missing)}")
        rendered = template.format(**fields)
        return self._normalize_spacing(rendered)

    @staticmethod
    def _normalize_spacing(text: str) -> str:
        lines = [line.rstrip() for line in str(text or "").splitlines()]
        normalized: list[str] = []
        previous_blank = False
        for line in lines:
            is_blank = not line.strip()
            if is_blank and previous_blank:
                continue
            normalized.append(line)
            previous_blank = is_blank
        return "\n".join(normalized).strip()

