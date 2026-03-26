import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.test_support import ClosableResource, DummyManagedConnection, DummyManagedHandler

from src.core import bot as bot_module
from src.core.bootstrap import BotRuntimeComponents
from src.core.models import MessageHandlingPlan, MessageType
from src.handlers.reply_pipeline import ReplyResult


class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_managed_resources_and_reports_real_sessions(self):
        bot = bot_module.QQBot()
        bot.connection = DummyManagedConnection()
        bot.message_handler = DummyManagedHandler(active_count=3)
        bot.memory_manager = ClosableResource()
        pending_task = asyncio.create_task(asyncio.sleep(60))
        bot._message_tasks.add(pending_task)
        bot._connection_task = asyncio.create_task(asyncio.sleep(60))

        status_before_close = bot.get_status()
        self.assertEqual(3, status_before_close["active_conversations"])

        await bot.close()

        self.assertTrue(bot.connection.disconnected)
        self.assertTrue(bot.message_handler.closed)
        self.assertTrue(bot.memory_manager.closed)
        self.assertFalse(bot.status["connected"])
        self.assertFalse(bot.status["ready"])
        self.assertEqual(set(), bot._message_tasks)
        self.assertIsNone(bot._connection_task)

    async def test_initialize_failure_triggers_cleanup_path(self):
        bot = bot_module.QQBot()
        bot.close = AsyncMock(wraps=bot.close)

        async def failing_build(**kwargs):
            raise RuntimeError("bootstrap failed")

        bot.bootstrapper.build = failing_build

        with self.assertRaises(RuntimeError):
            await bot.initialize()

        bot.close.assert_awaited_once()
        self.assertTrue(bot._closed)
        self.assertFalse(bot._initialized)

    async def test_close_is_idempotent(self):
        bot = bot_module.QQBot()
        bot.connection = DummyManagedConnection()
        bot.message_handler = DummyManagedHandler(active_count=1)
        bot.memory_manager = ClosableResource()

        await bot.close()
        await bot.close()

        self.assertEqual(1, bot.memory_manager.close_calls)
        self.assertTrue(bot.connection.disconnected)

    async def test_run_uses_bootstrapped_components_and_shuts_down_cleanly(self):
        bot = bot_module.QQBot()
        runtime = BotRuntimeComponents(
            connection=DummyManagedConnection(),
            message_handler=DummyManagedHandler(active_count=2),
            memory_manager=ClosableResource(),
        )

        async def build_runtime(**kwargs):
            return runtime

        async def stop_soon():
            await asyncio.sleep(0.01)
            bot._shutdown_event.set()

        bot.bootstrapper.build = build_runtime
        stopper = asyncio.create_task(stop_soon())
        self.addAsyncCleanup(stopper.cancel)

        await bot.run()

        self.assertTrue(runtime.connection.run_started)
        self.assertTrue(runtime.connection.disconnected)
        self.assertTrue(runtime.message_handler.closed)
        self.assertTrue(runtime.memory_manager.closed)
        self.assertFalse(bot.status["ready"])

    async def test_group_reply_sends_follow_up_image_without_second_at(self):
        bot = bot_module.QQBot()
        bot.connection = DummyManagedConnection()
        selection = SimpleNamespace(emoji=SimpleNamespace(emoji_id="emoji-1"))
        plan = MessageHandlingPlan(action="reply", reason="test")
        handler = SimpleNamespace(
            plan_message=AsyncMock(return_value=plan),
            check_rate_limit=AsyncMock(),
            get_ai_response=AsyncMock(return_value=ReplyResult(text="收到", source="ai")),
            split_long_message=lambda text: [text],
            plan_emoji_follow_up=AsyncMock(return_value=selection),
            get_emoji_follow_up_image_path=AsyncMock(return_value="d:/Works/code/Claude/tests/_tmp/follow.png"),
            mark_emoji_follow_up_sent=AsyncMock(),
            get_active_conversation_count=lambda: 0,
        )
        bot.message_handler = handler
        event = SimpleNamespace(
            user_id=123,
            group_id=456,
            message_type=MessageType.GROUP.value,
        )

        await bot._handle_message_event(event)

        self.assertEqual(2, len(bot.connection.sent))
        text_payload = bot.connection.sent[0]
        image_payload = bot.connection.sent[1]
        self.assertEqual("send_group_msg", text_payload["action"])
        self.assertIn("[CQ:at,qq=123] 收到", text_payload["params"]["message"])
        self.assertEqual("send_group_msg", image_payload["action"])
        self.assertIsInstance(image_payload["params"]["message"], list)
        self.assertEqual("image", image_payload["params"]["message"][0]["type"])
        self.assertEqual(["image"], [segment["type"] for segment in image_payload["params"]["message"]])
        handler.mark_emoji_follow_up_sent.assert_awaited_once_with(event, selection)


if __name__ == "__main__":
    unittest.main()

