from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from src.core.config import AppConfig, config, get_vision_service_status
from src.core.models import MessageEvent
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.command_registry import CommandContext, CommandRegistry, CommandSpec
from src.handlers.conversation_session_manager import ConversationSessionManager


StatusProvider = Callable[[], Dict[str, Any]]


class CommandHandler:
    """Handle built-in commands through a registry-backed dispatcher."""

    def __init__(
        self,
        session_manager: ConversationSessionManager,
        *,
        status_provider: Optional[StatusProvider] = None,
        runtime_metrics: Optional[RuntimeMetrics] = None,
        app_config: Optional[AppConfig] = None,
    ) -> None:
        self.session_manager = session_manager
        self.status_provider = status_provider
        self.runtime_metrics = runtime_metrics
        self.app_config = app_config or config.app
        self.registry = CommandRegistry()
        self._register_builtin_commands()

    def set_status_provider(self, status_provider: Optional[StatusProvider]) -> None:
        self.status_provider = status_provider

    def handle(self, text: str, event: MessageEvent) -> Optional[str]:
        spec = self.registry.match(text)
        if spec is None:
            return None

        if self.runtime_metrics:
            self.runtime_metrics.inc_command(spec.name)
        return spec.execute(CommandContext(event=event, raw_text=text))

    def get_help_text(self) -> str:
        assistant_name = self._assistant_name()
        intro_lines = [
            "私聊直接发消息即可。",
            "群聊请 @ 机器人，或在配置中开启更主动的群聊回复。",
            "",
            f"当前助手：{assistant_name}",
        ]
        return self.registry.build_help_text(
            title=f"{assistant_name} 使用帮助",
            intro_lines=intro_lines,
        )

    def get_status_text(self) -> str:
        status = self._snapshot_status()
        ai_service = self.app_config.ai_service
        vision_service = self.app_config.vision_service
        bot_behavior = self.app_config.bot_behavior
        vision_status = get_vision_service_status(self.app_config)

        lines = [
            f"{self._assistant_name()} 状态",
            f"运行状态：{'就绪' if status.get('ready') else '未就绪'}",
            f"连接状态：{'已连接' if status.get('connected') else '未连接'}",
            f"运行时长：{status.get('uptime_seconds', 0)} 秒",
            f"活跃消息任务：{status.get('active_message_tasks', 0)}",
            f"活跃会话数：{status.get('active_conversations', 0)}",
            f"消息接收/回复：{status.get('messages_received', 0)} / {status.get('messages_replied', 0)}",
            f"回复分片数：{status.get('reply_parts_sent', 0)}",
            f"命令命中：{status.get('command_hits', 0)}",
            f"群规划 reply/wait/ignore：{status.get('planner_reply', 0)} / {status.get('planner_wait', 0)} / {status.get('planner_ignore', 0)}",
            f"群规划 burst merge：{status.get('planner_burst_merge', 0)}",
            f"视觉请求/图片处理/失败：{status.get('vision_requests', 0)} / {status.get('vision_images_processed', 0)} / {status.get('vision_failures', 0)}",
            f"视觉结果复用：{status.get('vision_reused_from_plan', 0)}",
            f"Memory 读取/共享读取：{status.get('memory_reads', 0)} / {status.get('memory_shared_reads', 0)}",
            f"Memory 场景命中/拒绝：{status.get('memory_scene_rule_hits', 0)} / {status.get('memory_access_denied', 0)}",
            f"Memory 写入/迁移/压缩：{status.get('memory_writes', 0)} / {status.get('memory_migrations', 0)} / {status.get('memory_compactions', 0)}",
            f"后台任务数：{status.get('background_tasks', 0)}",
            f"AI 服务：{ai_service.api_base}",
            f"模型：{ai_service.model}",
            f"视觉状态：{vision_status}",
            f"视觉模型：{vision_service.model or '-'}",
            f"响应超时：{bot_behavior.response_timeout} 秒",
            f"消息长度限制：{bot_behavior.max_message_length} 字符",
        ]

        last_error_at = status.get("last_error_at")
        if last_error_at:
            lines.append(f"最近错误时间：{last_error_at}")
        return "\n".join(lines)

    def _register_builtin_commands(self) -> None:
        self.registry.register(
            CommandSpec(
                name="/help",
                aliases=("/帮助", "帮助"),
                description="查看帮助信息",
                execute=lambda ctx: self.get_help_text(),
            )
        )
        self.registry.register(
            CommandSpec(
                name="/status",
                aliases=("/状态",),
                description="查看运行状态与指标",
                execute=lambda ctx: self.get_status_text(),
            )
        )
        self.registry.register(
            CommandSpec(
                name="/reset",
                aliases=("/清除", "/清空"),
                description="清空当前会话上下文",
                execute=self._execute_reset,
            )
        )

    def _execute_reset(self, ctx: CommandContext) -> str:
        self.session_manager.clear_for_event(ctx.event)
        return "对话历史已清空。"

    def _snapshot_status(self) -> Dict[str, Any]:
        if callable(self.status_provider):
            return dict(self.status_provider() or {})

        active_conversations = self.session_manager.count_active()
        return {
            "ready": False,
            "connected": False,
            "uptime_seconds": 0,
            "active_message_tasks": 0,
            "active_conversations": active_conversations,
            "messages_received": 0,
            "messages_replied": 0,
            "reply_parts_sent": 0,
            "command_hits": 0,
            "planner_reply": 0,
            "planner_wait": 0,
            "planner_ignore": 0,
            "planner_burst_merge": 0,
            "vision_requests": 0,
            "vision_images_processed": 0,
            "vision_failures": 0,
            "vision_reused_from_plan": 0,
            "memory_reads": 0,
            "memory_shared_reads": 0,
            "memory_scene_rule_hits": 0,
            "memory_access_denied": 0,
            "memory_writes": 0,
            "memory_migrations": 0,
            "memory_compactions": 0,
            "background_tasks": 0,
            "last_error_at": None,
        }

    def _assistant_name(self) -> str:
        name = self.app_config.assistant_profile.name.strip()
        return name or config.get_assistant_name()
