from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from src.core.models import MessageEvent


@dataclass(frozen=True)
class CommandContext:
    event: MessageEvent
    raw_text: str


CommandExecutor = Callable[[CommandContext], str]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    description: str
    execute: CommandExecutor
    usage: str = ""

    def all_aliases(self) -> Iterable[str]:
        yield self.name
        for alias in self.aliases:
            yield alias


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: Dict[str, CommandSpec] = {}
        self._alias_map: Dict[str, CommandSpec] = {}

    def register(self, spec: CommandSpec) -> None:
        self._commands[spec.name] = spec
        for alias in spec.all_aliases():
            normalized = self.normalize(alias)
            if normalized:
                self._alias_map[normalized] = spec

    def match(self, text: str) -> Optional[CommandSpec]:
        normalized = self.normalize(text)
        if not normalized:
            return None
        command_token = normalized.split(maxsplit=1)[0]
        return self._alias_map.get(command_token)

    def list_commands(self) -> List[CommandSpec]:
        return [self._commands[name] for name in sorted(self._commands)]

    def build_help_text(self, title: str, intro_lines: List[str]) -> str:
        lines = [title, ""]
        lines.extend(line for line in intro_lines if line)
        lines.append("可用命令：")
        for spec in self.list_commands():
            alias_text = " / ".join(spec.all_aliases())
            usage_suffix = f" 用法：{spec.usage}" if spec.usage else ""
            lines.append(f"- {alias_text}：{spec.description}{usage_suffix}")
        return "\n".join(lines).strip()

    @staticmethod
    def normalize(text: str) -> str:
        return str(text or "").strip().lower()
