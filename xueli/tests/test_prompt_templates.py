from __future__ import annotations

import unittest

from src.core.prompt_templates import PromptTemplateLoader


class PromptTemplateLoaderTests(unittest.TestCase):
    def test_render_reply_template_includes_named_blocks(self) -> None:
        loader = PromptTemplateLoader()

        rendered = loader.render(
            "reply.prompt",
            identity_block="你是雪梨。",
            constraint_block="平台：私聊。格式：数组。",
            scene_block="这是和用户的私聊对话。用户说：早上好",
            continuity_block="回复目标：answer\n连续性策略：direct_continue",
            planner_reference_block="参考方向（非强制）：轻一点。不要照抄。",
            vision_block="",
            person_facts_block="[人格事实]\n用户喜欢聊动漫。",
            precise_recall_block="[精确召回]\n上一轮聊的是天气。",
            dynamic_memory_block="[动态记忆]\n最近在追一部新番。",
            final_style_block="[风格约束]\n- 长度：短。",
        )

        self.assertIn("你是雪梨。", rendered)
        self.assertIn("参考方向（非强制）：轻一点。不要照抄。", rendered)
        self.assertIn("[人格事实]", rendered)
        self.assertIn("[风格约束]", rendered)

    def test_render_raises_clear_error_when_field_missing(self) -> None:
        loader = PromptTemplateLoader()

        with self.assertRaises(KeyError) as exc:
            loader.render(
                "planner.prompt",
                chat_mode_label="私聊",
                scene_guidance="只看当前消息。",
            )

        self.assertIn("decision_output_schema", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
