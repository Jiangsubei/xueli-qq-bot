from __future__ import annotations

import asyncio
import json
import re
import sys
import types
import unittest
from collections import defaultdict, deque
from types import SimpleNamespace


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

if "aiofiles" not in sys.modules:
    aiofiles = types.ModuleType("aiofiles")
    aiofiles.open = lambda *args, **kwargs: None
    sys.modules["aiofiles"] = aiofiles

if "jieba" not in sys.modules:
    jieba = types.ModuleType("jieba")
    jieba.cut = lambda text: str(text or "").split()
    jieba.lcut = lambda text: str(text or "").split()
    sys.modules["jieba"] = jieba

if "rank_bm25" not in sys.modules:
    rank_bm25 = types.ModuleType("rank_bm25")

    class _BM25Okapi:
        def __init__(self, corpus):
            self.corpus = corpus

        def get_scores(self, query_tokens):
            del query_tokens
            return [0.0 for _ in self.corpus]

    rank_bm25.BM25Okapi = _BM25Okapi
    sys.modules["rank_bm25"] = rank_bm25

if "django.conf" not in sys.modules:
    django = types.ModuleType("django")
    django_conf = types.ModuleType("django.conf")
    django_conf.settings = SimpleNamespace(
        WEBUI_CONFIG_PATH="F:/AI/Workspace/xueli/config/settings.toml",
        WEBUI_RUNTIME_SNAPSHOT_PATH="F:/AI/Workspace/xueli/data/webui/runtime_snapshot.json",
        WEBUI_AVATAR_ROOT="F:/AI/Workspace/xueli/data/webui/avatar",
    )
    django_urls = types.ModuleType("django.urls")
    django_urls.reverse = lambda name: f"/{name}/"
    sys.modules["django"] = django
    sys.modules["django.conf"] = django_conf
    sys.modules["django.urls"] = django_urls

if "tomlkit" not in sys.modules:
    tomlkit = types.ModuleType("tomlkit")
    tomlkit.aot = lambda: []
    tomlkit.document = lambda: {}
    tomlkit.dumps = lambda value: str(value)
    tomlkit.inline_table = lambda: {}
    tomlkit.item = lambda value: value
    tomlkit.parse = lambda text: {}
    tomlkit.table = lambda: {}
    sys.modules["tomlkit"] = tomlkit
    tomlkit_items = types.ModuleType("tomlkit.items")
    tomlkit_items.AoT = list
    tomlkit_items.InlineTable = dict
    tomlkit_items.Table = dict
    sys.modules["tomlkit.items"] = tomlkit_items
    tomlkit_doc = types.ModuleType("tomlkit.toml_document")
    tomlkit_doc.TOMLDocument = dict
    sys.modules["tomlkit.toml_document"] = tomlkit_doc

from src.core.models import MessageEvent
from src.core.runtime import BotRuntime
from src.handlers.command_handler import CommandHandler
from src.handlers.conversation_context_builder import ConversationContextBuilder
from src.handlers.conversation_plan_coordinator import ConversationPlanCoordinator
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.conversation_session_manager import ConversationSessionManager
from src.handlers.message_handler import MessageHandler
from src.handlers.timing_gate_service import TimingGateService
from src.handlers.reply_pipeline import ReplyPipeline
from src.handlers.repeat_echo_service import RepeatEchoService
from src.memory.memory_flow_service import MemoryFlowService
from src.memory.internal.access_policy import MemoryAccessPolicy
from src.memory.storage.markdown_store import MemoryItem
from src.webui.console.services import _serialize_memory_item


class _PlannerAIClient:
    def build_text_message(self, role: str, content: str):
        return {"role": role, "content": content}

    async def chat_completion(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            content=json.dumps(
                {
                    "action": "reply",
                    "reason": "当前话题适合自然接住",
                    "prompt_plan": {
                        "reply_goal": "continue",
                        "continuity_mode": "resume_recent_topic",
                        "timeline_detail": "summary",
                        "context_profile": "standard",
                        "memory_profile": "relevant",
                        "tone_profile": "balanced",
                        "initiative": "gentle_follow",
                        "expression_profile": "colloquial",
                        "policy": {
                            "include_recent_history": True,
                            "include_person_facts": True,
                            "include_session_restore": True,
                            "include_precise_recall": False,
                            "include_dynamic_memory": True,
                            "include_vision_context": True,
                            "include_reply_scope": True,
                            "include_style_guide": True,
                        },
                    },
                },
                ensure_ascii=False,
            )
        )


class _TimingGateAIClient:
    def build_text_message(self, role: str, content: str):
        return {"role": role, "content": content}

    async def chat_completion(self, **kwargs):
        del kwargs
        return SimpleNamespace(content='{"decision":"continue","reason":"现在适合继续"}')


class _ReplyAIClient:
    def __init__(self) -> None:
        self.last_messages = []

    def build_text_message(self, role: str, content: str):
        return {"role": role, "content": content}

    async def chat_completion(self, **kwargs):
        self.last_messages = list(kwargs.get("messages") or [])
        return SimpleNamespace(content="那我们就顺着刚才的周末计划继续定一下吧。", tool_calls=[])


class _SegmentedReplyAIClient(_ReplyAIClient):
    async def chat_completion(self, **kwargs):
        self.last_messages = list(kwargs.get("messages") or [])
        return SimpleNamespace(content='["第一句", "第二句"]', tool_calls=[])


class _MemoryManagerStub:
    def __init__(self) -> None:
        self.turns = []
        self.extractions = []
        self.webui_items = []

    async def format_person_facts_for_prompt(self, **kwargs):
        del kwargs
        return "1. 用户最近在准备周末出行"

    async def get_important_memories(self, **kwargs):
        del kwargs
        return []

    async def search_memories_with_context(self, **kwargs):
        del kwargs
        return {
            "memories": [{"content": "用户想继续聊周末计划", "owner_user_id": "42"}],
            "history_messages": [{"role": "assistant", "content": "上次我们聊到出行安排。"}],
            "session_restore": [{"content": "上一轮聊到想周末出去走走。"}],
            "precise_recall": [],
        }

    def build_access_context(self, **kwargs):
        return SimpleNamespace(
            read_scope=kwargs.get("read_scope"),
            message_type=kwargs.get("message_type"),
            group_id=kwargs.get("group_id"),
        )

    def register_dialogue_turn(self, **kwargs):
        self.turns.append(dict(kwargs))
        self.webui_items.append(
            MemoryItem(
                id=f"mem-{len(self.turns)}",
                content=str(kwargs.get("user_message") or ""),
                source="integration",
                owner_user_id=str(kwargs.get("user_id") or ""),
                metadata={
                    "summary": "用户在顺着周末计划继续往下聊。",
                    "source_message_type": str(kwargs.get("message_type") or ""),
                    "source_session_id": str(kwargs.get("dialogue_key") or ""),
                    "source_dialogue_key": str(kwargs.get("dialogue_key") or ""),
                    "source_turn_start": len(self.turns),
                    "source_turn_end": len(self.turns),
                    "source_group_id": str(kwargs.get("group_id") or ""),
                    "source_message_ids": [str(kwargs.get("message_id") or "")],
                },
            )
        )

    def schedule_memory_extraction(self, user_id: str, **kwargs):
        self.extractions.append({"user_id": user_id, **kwargs})


class _RuntimeMetricsStub:
    def __init__(self) -> None:
        self.received = 0
        self.replied = 0
        self.errors = 0

    def inc_messages_received(self):
        self.received += 1

    def inc_messages_replied(self, count: int):
        self.replied += int(count)

    def record_error(self, **kwargs):
        del kwargs
        self.errors += 1


class _AdapterStub:
    def __init__(self) -> None:
        self.platform = "qq"
        self.adapter_name = "napcat"
        self.actions = []

    async def send_action(self, action):
        self.actions.append(action)
        return True


class RuntimeConversationFlowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _build_app_config(self):
        return SimpleNamespace(
            assistant_profile=SimpleNamespace(name="测试助手", alias="小助"),
            personality=SimpleNamespace(content="温和耐心"),
            dialogue_style=SimpleNamespace(content="自然一点"),
            behavior=SimpleNamespace(content="优先自然陪伴，不要突然说教"),
            ai_service=SimpleNamespace(model="reply-model"),
            bot_behavior=SimpleNamespace(
                max_context_length=6,
                max_message_length=4000,
                response_timeout=30,
                rate_limit_interval=0.0,
                log_full_prompt=False,
                private_quote_reply_enabled=False,
                private_batch_window_seconds=0.0,
                segmented_reply_enabled=True,
                max_segments=3,
                first_segment_delay_min_ms=0,
                first_segment_delay_max_ms=0,
                followup_delay_min_seconds=0.0,
                followup_delay_max_seconds=0.0,
            ),
            memory=SimpleNamespace(read_scope="global"),
            group_reply=SimpleNamespace(
                only_reply_when_at=False,
                interest_reply_enabled=True,
                plan_request_interval=0.0,
                plan_request_max_parallel=1,
                at_user_when_proactive_reply=False,
                repeat_echo_enabled=False,
                repeat_echo_window_seconds=20.0,
                repeat_echo_min_count=2,
                repeat_echo_cooldown_seconds=90.0,
            ),
            group_reply_decision=SimpleNamespace(api_base="https://planner.example", api_key="k", model="planner"),
            emoji=SimpleNamespace(enabled=False),
        )

    def _build_handler(self, *, memory_manager: _MemoryManagerStub, reply_ai: _ReplyAIClient) -> MessageHandler:
        app_config = self._build_app_config()
        handler = MessageHandler.__new__(MessageHandler)
        handler.app_config = app_config
        handler.runtime_metrics = None
        handler.memory_manager = memory_manager
        handler.model_invocation_router = None
        handler.ai_client = reply_ai
        handler.image_client = SimpleNamespace()
        handler.vision_client = None
        handler.emoji_manager = None
        handler.emoji_reply_service = None
        handler.conversation_planner = ConversationPlanner(ai_client=_PlannerAIClient(), app_config=app_config)
        handler.session_manager = ConversationSessionManager()
        handler.conversation_plan_coordinator = ConversationPlanCoordinator(
            planner=handler.conversation_planner,
            session_manager=handler.session_manager,
            group_reply_config=app_config.group_reply,
            context_window_size=app_config.bot_behavior.max_context_length,
            event_text_getter=handler._get_event_text,
            sender_display_name_getter=handler._get_sender_display_name,
            has_image_getter=handler._has_image_input,
            image_count_getter=handler._get_image_count,
            image_file_ids_getter=handler._get_image_file_ids,
            image_analyzer=None,
        )
        handler.command_handler = CommandHandler.__new__(CommandHandler)
        handler.command_handler.handle = lambda text, event: None
        handler.command_handler.get_help_text = lambda: ""
        handler.command_handler.get_status_text = lambda: ""
        handler.command_handler.set_status_provider = lambda provider: None
        handler.memory_flow_service = MemoryFlowService(memory_manager)
        handler.context_builder = ConversationContextBuilder(handler)
        handler.timing_gate_service = TimingGateService(ai_client=_TimingGateAIClient(), app_config=app_config)
        handler.reply_pipeline = ReplyPipeline(handler)
        handler.last_send_time = {}
        handler.rate_limit_lock = asyncio.Lock()
        handler.private_batch_lock = asyncio.Lock()
        handler.private_batch_versions = defaultdict(int)
        handler.private_pending_inputs = defaultdict(list)
        handler.private_batch_window_seconds = 0.0
        handler.group_repeat_lock = asyncio.Lock()
        handler._group_repeat_history = defaultdict(deque)
        handler._group_repeat_cooldowns = {}
        handler.repeat_echo_service = RepeatEchoService(app_config, handler.runtime_metrics)
        handler._sync_active_conversations_metric = lambda count=None: None
        return handler

    def _build_runtime(self, handler: MessageHandler) -> tuple[BotRuntime, _AdapterStub, _RuntimeMetricsStub]:
        adapter = _AdapterStub()
        metrics = _RuntimeMetricsStub()
        runtime = BotRuntime.__new__(BotRuntime)
        runtime.adapter = adapter
        runtime.connection = adapter
        runtime.config = SimpleNamespace(app=SimpleNamespace(adapter_connection=SimpleNamespace(platform="qq", adapter="napcat")))
        runtime._adapters_by_name = {}
        runtime._adapters_by_platform = {}
        runtime.message_handler = handler
        runtime.runtime_metrics = metrics
        runtime._sync_status_cache = lambda: None
        runtime._should_log_message_summary = lambda: False
        runtime.webui_snapshot = SimpleNamespace(publish=lambda: None)
        runtime.register_runtime_adapter(adapter)

        async def _noop_follow_up(event, reply_result, plan, trace_id=""):
            del event, reply_result, plan, trace_id

        runtime._send_emoji_follow_up_if_needed = _noop_follow_up
        return runtime, adapter, metrics

    def _event(self) -> MessageEvent:
        return MessageEvent.from_dict(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 88,
                "user_id": 42,
                "group_id": 100,
                "self_id": 999,
                "time": 1000,
                "raw_message": "然后呢",
                "message": [{"type": "text", "data": {"text": "然后呢"}}],
                "sender": {"card": "Group User"},
            }
        )

    async def test_runtime_planner_memory_reply_and_webui_form_a_closed_loop(self) -> None:
        memory_manager = _MemoryManagerStub()
        reply_ai = _ReplyAIClient()
        handler = self._build_handler(memory_manager=memory_manager, reply_ai=reply_ai)
        runtime, adapter, metrics = self._build_runtime(handler)

        await runtime._handle_message_event(self._event(), trace_id="trace-integration")

        await handler.memory_flow_service._drain_queue_for_tests()

        self.assertEqual(metrics.received, 1)
        self.assertEqual(metrics.replied, 1)
        self.assertEqual(len(adapter.actions), 1)
        self.assertEqual(adapter.actions[0].text, "那我们就顺着刚才的周末计划继续定一下吧。")

        self.assertTrue(reply_ai.last_messages)
        system_prompt = str(reply_ai.last_messages[0].get("content") or "")
        self.assertIn("[风格约束]", system_prompt)
        self.assertIn("回复目标：continue", system_prompt)
        self.assertIn("用户最近在准备周末出行", system_prompt)

        self.assertEqual(len(memory_manager.turns), 1)
        self.assertEqual(memory_manager.turns[0]["user_message"], "然后呢")
        self.assertEqual(memory_manager.turns[0]["assistant_message"], "那我们就顺着刚才的周末计划继续定一下吧。")
        self.assertEqual(len(memory_manager.extractions), 1)

        payload = _serialize_memory_item(
            memory_manager.webui_items[0],
            kind="ordinary",
            access_policy=MemoryAccessPolicy(),
        )
        self.assertEqual(payload["content"], "然后呢")
        self.assertEqual(payload["summary"], "用户在顺着周末计划继续往下聊。")
        self.assertEqual(payload["source_message_type"], "group")
        self.assertEqual(payload["source_group_id"], "100")

    async def test_runtime_sends_segmented_reply_as_multiple_messages(self) -> None:
        memory_manager = _MemoryManagerStub()
        reply_ai = _SegmentedReplyAIClient()
        handler = self._build_handler(memory_manager=memory_manager, reply_ai=reply_ai)
        runtime, adapter, metrics = self._build_runtime(handler)

        await runtime._handle_message_event(self._event(), trace_id="trace-segmented")

        self.assertEqual(metrics.received, 1)
        self.assertEqual(metrics.replied, 2)
        self.assertEqual(len(adapter.actions), 2)
        self.assertEqual(adapter.actions[0].text, "第一句")
        self.assertEqual(adapter.actions[1].text, "第二句")


if __name__ == "__main__":
    unittest.main()
