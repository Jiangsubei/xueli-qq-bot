from __future__ import annotations

import asyncio
import unittest

from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType


class ModelInvocationRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_exposes_per_purpose_default_timeouts(self) -> None:
        router = ModelInvocationRouter(base_timeout_seconds=60)
        try:
            self.assertEqual(router.timeout_seconds_for(ModelInvocationType.GROUP_PLAN), 20.0)
            self.assertEqual(router.timeout_seconds_for(ModelInvocationType.REPLY_GENERATION), 60.0)
            self.assertEqual(router.timeout_seconds_for(ModelInvocationType.EMOJI_REPLY_DECISION), 12.0)
            self.assertEqual(router.timeout_seconds_for(ModelInvocationType.MEMORY_RERANK), 8.0)
        finally:
            await router.close()

    async def test_router_times_out_slow_task_and_keeps_worker_usable(self) -> None:
        router = ModelInvocationRouter(
            purpose_timeouts={ModelInvocationType.GROUP_PLAN: 0.01},
        )

        try:
            with self.assertRaises(asyncio.TimeoutError):
                await router.submit(
                    purpose=ModelInvocationType.GROUP_PLAN,
                    label="slow-plan",
                    runner=self._slow_runner,
                )

            result = await router.submit(
                purpose=ModelInvocationType.GROUP_PLAN,
                label="fast-plan",
                runner=self._fast_runner,
            )

            self.assertEqual(result, "ok")
        finally:
            await router.close()

    async def _slow_runner(self) -> str:
        await asyncio.sleep(0.05)
        return "slow"

    async def _fast_runner(self) -> str:
        await asyncio.sleep(0)
        return "ok"


if __name__ == "__main__":
    unittest.main()
