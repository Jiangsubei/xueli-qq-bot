from __future__ import annotations

import asyncio
import re
from pathlib import Path
from string import Formatter
from typing import Any


class PromptTemplateLoader:
    """Lightweight file-based prompt template loader.

    Supports #-prefixed comment lines in .prompt files — they are stripped
    during load() and never appear in rendered output.
    """

    _COMMENT_PATTERN = re.compile(r"^\s*#")

    def __init__(self, *, locale: str = "zh-CN") -> None:
        self.locale = locale
        self._formatter = Formatter()
        self._cache: dict[str, str] = {}
        self._preloaded = False

    @property
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "prompts" / self.locale

    def _preload_templates(self) -> None:
        if self._preloaded:
            return
        self._preloaded = True
        try:
            for path in self.templates_dir.iterdir():
                if path.suffix == ".prompt":
                    name = path.name
                    if name not in self._cache:
                        raw = path.read_text(encoding="utf-8")
                        self._cache[name] = self._normalize_spacing(self._strip_comments(raw))
        except Exception:
            pass

    def load(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        self._preload_templates()
        if name in self._cache:
            return self._cache[name]
        template_path = self.templates_dir / name
        if not template_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_path}")
        raw = template_path.read_text(encoding="utf-8")
        result = self._normalize_spacing(self._strip_comments(raw))
        self._cache[name] = result
        return result

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
    def _strip_comments(text: str) -> str:
        """Remove #-prefixed comment lines from template text."""
        lines = [
            line for line in str(text or "").splitlines()
            if not PromptTemplateLoader._COMMENT_PATTERN.match(line)
        ]
        return "\n".join(lines)

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

