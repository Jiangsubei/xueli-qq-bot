import base64
import shutil
import unittest
import uuid
from pathlib import Path

from tests.test_support import DummyAIClient, build_event, install_dependency_stubs

install_dependency_stubs()

from src.core.config import AppConfig, EmojiConfig
from src.core.models import MessageSegment, MessageType
from src.emoji.models import EmojiEmotionResult
from src.emoji.reply_service import EmojiReplyService
from src.emoji.repository import EmojiRepository
from src.services.ai_client import AIResponse

TMP_ROOT = Path("tests") / "_tmp"


class EmojiReplyServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_storage(self) -> Path:
        temp_dir = TMP_ROOT / f"emoji_reply_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    async def create_service(self, storage: Path, *, responses=None, reply_enabled=True, cooldown=180.0):
        repository = EmojiRepository(str(storage))
        await repository.initialize()
        app_config = AppConfig(
            emoji=EmojiConfig(
                enabled=True,
                storage_path=str(storage),
                reply_enabled=reply_enabled,
                reply_cooldown_seconds=cooldown,
            )
        )
        service = EmojiReplyService(
            repository=repository,
            ai_client=DummyAIClient(responses=responses or []),
            runtime_metrics=None,
            app_config=app_config,
        )
        return service, repository

    async def add_emoji(self, repository: EmojiRepository, *, label: str, tones, intents, disabled=False):
        event = build_event("看看", message_type=MessageType.GROUP.value, image_count=1, message_id=uuid.uuid4().int % 100000)
        image_data = base64.b64encode(label.encode("utf-8")).decode("ascii")
        record = await repository.save_detected_emoji(
            event=event,
            segment=MessageSegment.image(f"{label}.png"),
            image_base64=image_data,
            description=label,
            sticker_confidence=0.95,
            sticker_reason="test",
        )
        updated = await repository.update_emotion(
            record.emoji_id,
            EmojiEmotionResult(
                primary_emotion="开心",
                confidence=0.9,
                reason="test",
                all_emotions=["开心"],
                reply_tones=list(tones),
                reply_intents=list(intents),
            ),
        )
        if disabled:
            updated.disabled = True
            async with repository._lock:
                index = await repository._read_index()
                index["items"][updated.emoji_id] = updated.to_dict()
                await repository._write_index(index)
        return updated

    async def test_group_reply_prefers_intent_match(self):
        storage = self.make_storage()
        service, repository = await self.create_service(
            storage,
            responses=[AIResponse(content='{"should_send": true, "tone": "安慰", "emotion": "开心", "intent": "安慰-开心", "reason": "fit"}')],
        )
        intent_match = await self.add_emoji(repository, label="intent", tones=["安慰"], intents=["安慰-开心"])
        await self.add_emoji(repository, label="tone_only", tones=["安慰"], intents=[])
        event = build_event("别难过", message_type=MessageType.GROUP.value)

        selection = await service.plan_follow_up(
            event=event,
            user_message="别难过",
            assistant_reply="抱抱你",
            reply_context={"window_messages": [{"user_id": "1", "text": "别难过", "is_latest": True}]},
        )

        self.assertEqual(intent_match.emoji_id, selection.emoji.emoji_id)
        self.assertEqual("安慰-开心", selection.decision.target_intent)

    async def test_reply_candidate_falls_back_from_intent_to_tone(self):
        storage = self.make_storage()
        service, repository = await self.create_service(
            storage,
            responses=[AIResponse(content='{"should_send": true, "tone": "吐槽", "emotion": "开心", "intent": "吐槽-开心", "reason": "fit"}')],
        )
        tone_match = await self.add_emoji(repository, label="tone", tones=["吐槽"], intents=[])
        event = build_event("这也太离谱了", message_type=MessageType.GROUP.value)

        selection = await service.plan_follow_up(
            event=event,
            user_message="这也太离谱了",
            assistant_reply="确实离谱",
        )

        self.assertEqual(tone_match.emoji_id, selection.emoji.emoji_id)
        self.assertEqual("吐槽", selection.decision.target_tone)

    async def test_private_message_never_triggers_follow_up(self):
        storage = self.make_storage()
        service, repository = await self.create_service(
            storage,
            responses=[AIResponse(content='{"should_send": true, "tone": "安慰", "emotion": "开心", "intent": "安慰-开心", "reason": "fit"}')],
        )
        await self.add_emoji(repository, label="intent", tones=["安慰"], intents=["安慰-开心"])
        event = build_event("你好", message_type=MessageType.PRIVATE.value)

        selection = await service.plan_follow_up(
            event=event,
            user_message="你好",
            assistant_reply="你好呀",
        )

        self.assertEqual("unsupported_message_type", selection.skip_reason)
        self.assertIsNone(selection.emoji)

    async def test_reply_enabled_false_skips_feature(self):
        storage = self.make_storage()
        service, repository = await self.create_service(
            storage,
            responses=[AIResponse(content='{"should_send": true, "tone": "安慰", "emotion": "开心", "intent": "安慰-开心", "reason": "fit"}')],
            reply_enabled=False,
        )
        await self.add_emoji(repository, label="intent", tones=["安慰"], intents=["安慰-开心"])
        event = build_event("你好", message_type=MessageType.GROUP.value)

        selection = await service.plan_follow_up(
            event=event,
            user_message="你好",
            assistant_reply="你好呀",
        )

        self.assertEqual("feature_disabled", selection.skip_reason)

    async def test_group_and_single_emoji_cooldown_are_respected(self):
        storage = self.make_storage()
        service, repository = await self.create_service(
            storage,
            responses=[
                AIResponse(content='{"should_send": true, "tone": "安慰", "emotion": "开心", "intent": "安慰-开心", "reason": "fit"}'),
                AIResponse(content='{"should_send": true, "tone": "安慰", "emotion": "开心", "intent": "安慰-开心", "reason": "fit"}'),
                AIResponse(content='{"should_send": true, "tone": "安慰", "emotion": "开心", "intent": "安慰-开心", "reason": "fit"}'),
            ],
            cooldown=999.0,
        )
        first = await self.add_emoji(repository, label="first", tones=["安慰"], intents=["安慰-开心"])
        second = await self.add_emoji(repository, label="second", tones=["安慰"], intents=["安慰-开心"])
        event = build_event("难受", message_type=MessageType.GROUP.value)

        first_selection = await service.plan_follow_up(event=event, user_message="难受", assistant_reply="抱抱你")
        self.assertIsNotNone(first_selection.emoji)
        await service.mark_follow_up_sent(event=event, selection=first_selection)

        blocked = await service.plan_follow_up(event=event, user_message="难受", assistant_reply="抱抱你")
        self.assertEqual("group_cooldown", blocked.skip_reason)

        service._group_last_sent_at.clear()
        await repository.mark_auto_reply_sent(first_selection.emoji.emoji_id)
        next_selection = await service.plan_follow_up(event=event, user_message="难受", assistant_reply="抱抱你")

        self.assertIsNotNone(next_selection.emoji)
        expected_next_id = second.emoji_id if first_selection.emoji.emoji_id == first.emoji_id else first.emoji_id
        self.assertEqual(expected_next_id, next_selection.emoji.emoji_id)
        self.assertNotEqual(first_selection.emoji.emoji_id, next_selection.emoji.emoji_id)


if __name__ == "__main__":
    unittest.main()

