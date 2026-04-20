from __future__ import annotations

import asyncio
import unittest

from src.handlers.conversation_window_scheduler import ConversationWindowScheduler


class ConversationWindowSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_windows_dispatch_in_order(self) -> None:
        scheduler = ConversationWindowScheduler()

        def build_message(event: str):
            return {"text_content": event, "text": event, "event_time": 1.0}

        def merge_messages(items):
            return "\n".join(str(item.get("text_content") or "") for item in items)

        first_task = asyncio.create_task(
            scheduler.submit_event(
                conversation_key="c1",
                chat_mode="private",
                event="窗口1-消息1",
                window_seconds=0.01,
                queue_expire_seconds=1.0,
                message_builder=build_message,
                merge_builder=merge_messages,
            )
        )
        await asyncio.sleep(0.02)
        first_result = await first_task
        self.assertEqual(first_result.status, "dispatch_window")
        self.assertEqual(first_result.window.seq, 1)

        accepted_second = await scheduler.submit_event(
            conversation_key="c1",
            chat_mode="private",
            event="窗口2-消息1",
            window_seconds=0.01,
            queue_expire_seconds=1.0,
            message_builder=build_message,
            merge_builder=merge_messages,
        )
        self.assertEqual(accepted_second.status, "accepted_only")
        await asyncio.sleep(0.02)
        accepted_third = await scheduler.submit_event(
            conversation_key="c1",
            chat_mode="private",
            event="窗口3-消息1",
            window_seconds=0.01,
            queue_expire_seconds=1.0,
            message_builder=build_message,
            merge_builder=merge_messages,
        )
        self.assertEqual(accepted_third.status, "accepted_only")
        await asyncio.sleep(0.02)

        second_dispatch = await scheduler.mark_window_complete("c1", 1)
        self.assertEqual(second_dispatch.status, "dispatch_window")
        self.assertEqual(second_dispatch.window.seq, 2)
        third_dispatch = await scheduler.mark_window_complete("c1", 2)
        self.assertEqual(third_dispatch.status, "dispatch_window")
        self.assertEqual(third_dispatch.window.seq, 3)

    async def test_expired_queued_window_is_dropped_before_dispatch(self) -> None:
        scheduler = ConversationWindowScheduler()

        def build_message(event: str):
            return {"text_content": event, "text": event, "event_time": 1.0}

        def merge_messages(items):
            return "\n".join(str(item.get("text_content") or "") for item in items)

        first_task = asyncio.create_task(
            scheduler.submit_event(
                conversation_key="c2",
                chat_mode="private",
                event="窗口1",
                window_seconds=0.01,
                queue_expire_seconds=0.02,
                message_builder=build_message,
                merge_builder=merge_messages,
            )
        )
        await asyncio.sleep(0.02)
        first_result = await first_task
        self.assertEqual(first_result.window.seq, 1)

        accepted = await scheduler.submit_event(
            conversation_key="c2",
            chat_mode="private",
            event="窗口2",
            window_seconds=0.01,
            queue_expire_seconds=0.02,
            message_builder=build_message,
            merge_builder=merge_messages,
        )
        self.assertEqual(accepted.status, "accepted_only")
        await asyncio.sleep(0.05)

        next_result = await scheduler.mark_window_complete("c2", 1)
        self.assertEqual(next_result.status, "accepted_only")
        self.assertEqual(next_result.dropped_count, 1)


if __name__ == "__main__":
    unittest.main()
