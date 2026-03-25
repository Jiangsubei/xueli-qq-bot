import unittest

from src.memory.extraction.memory_extractor import ExtractionConfig, MemoryExtractor


class MemoryExtractorTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_memories_only_uses_user_messages(self):
        captured_prompt = {}

        async def fake_llm_callback(system_prompt, messages):
            captured_prompt["system_prompt"] = system_prompt
            captured_prompt["messages"] = messages
            return "无"

        extractor = MemoryExtractor(
            memory_store=None,
            llm_callback=fake_llm_callback,
            config=ExtractionConfig(extract_every_n_turns=1, max_dialogue_length=5),
        )

        extractor.add_dialogue_turn(
            user_id="42",
            user_message="我住在上海，平时喜欢喝黑咖啡。",
            assistant_message="好的，我记住你住在北京。",
        )
        extractor.add_dialogue_turn(
            user_id="42",
            user_message="我对花生过敏。",
            assistant_message="明白，我之后会推荐含花生的零食。",
        )

        memories = await extractor.extract_memories("42")

        self.assertEqual(memories, [])
        prompt = captured_prompt["messages"][0]["content"]

        self.assertIn("我住在上海，平时喜欢喝黑咖啡。", prompt)
        self.assertIn("我对花生过敏。", prompt)
        self.assertNotIn("好的，我记住你住在北京。", prompt)
        self.assertNotIn("明白，我之后会推荐含花生的零食。", prompt)
        self.assertNotIn("助手回复", prompt)
        self.assertIn("如果用户是在要求你记住某件事", prompt)
        self.assertIn("记住、记得、别忘了", prompt)

    async def test_extract_memories_retries_and_skips_rate_limit(self):
        call_count = 0

        async def fake_llm_callback(system_prompt, messages):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("API 请求失败: 429, provider is temporarily rate-limited upstream")

        extractor = MemoryExtractor(
            memory_store=None,
            llm_callback=fake_llm_callback,
            config=ExtractionConfig(extract_every_n_turns=1, max_dialogue_length=5),
        )

        extractor.add_dialogue_turn(
            user_id="42",
            user_message="请记住我喜欢爵士乐。",
            assistant_message="好的。",
        )

        memories = await extractor.extract_memories("42")

        self.assertEqual(memories, [])
        self.assertEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main()
