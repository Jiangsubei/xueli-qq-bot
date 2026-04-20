from __future__ import annotations

import sys
import types
import unittest


if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = object
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

from src.core.models import MessageHandlingPlan, PromptPlan, TemporalContext
from src.handlers.message_context import MessageContext
from src.handlers.timing_gate_service import TimingGateService


class TimingGateServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_waits_when_message_looks_incomplete(self) -> None:
        service = TimingGateService()
        service.ai_client = None
        context = MessageContext(
            user_message="然后",
            temporal_context=TemporalContext(summary_text="刚刚还在连续对话"),
            planning_signals={"looks_fragmented": True},
        )
        plan = MessageHandlingPlan(action="reply", reason="适合接话", prompt_plan=PromptPlan())

        decision = await service.decide(event=types.SimpleNamespace(message_id=1), plan=plan, context=context)

        self.assertEqual(decision.decision, "wait")

    async def test_fallback_continues_by_default(self) -> None:
        service = TimingGateService()
        service.ai_client = None
        context = MessageContext(
            user_message="继续说吧",
            temporal_context=TemporalContext(summary_text="上下文连续"),
            planning_signals={},
        )
        plan = MessageHandlingPlan(action="reply", reason="适合继续", prompt_plan=PromptPlan())

        decision = await service.decide(event=types.SimpleNamespace(message_id=1), plan=plan, context=context)

        self.assertEqual(decision.decision, "continue")


if __name__ == "__main__":
    unittest.main()
