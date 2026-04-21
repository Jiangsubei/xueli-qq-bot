from __future__ import annotations

import unittest

from src.core.prompt_templates import PromptTemplateLoader


class PromptTemplateLoaderTests(unittest.TestCase):
    def test_render_reply_template_includes_named_blocks(self) -> None:
        loader = PromptTemplateLoader()

        rendered = loader.render(
            "reply.prompt",
            identity_block="你是雪梨。",
            session_block="当前场景：私聊。",
            reply_target_block="当前消息：早上好",
            continuity_block="连续性标签：unknown",
            planner_reference_block="规划参考：轻一点。",
            timeline_block="时间线摘要：无",
            recent_history_block="最近上下文：无",
            memory_sections_block="",
            reply_scope_block="回复范围：围绕当前消息。",
            final_style_block="最终回复风格：自然。",
            output_format_block="输出格式要求：数组。",
        )

        self.assertIn("你是雪梨。", rendered)
        self.assertIn("规划参考：轻一点。", rendered)
        self.assertIn("输出格式要求：数组。", rendered)

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
