import asyncio
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path


def install_dependency_stubs():
    if "aiohttp" not in sys.modules:
        aiohttp_module = types.ModuleType("aiohttp")

        class ClientError(Exception):
            pass

        class ClientTimeout:
            def __init__(self, total=None):
                self.total = total

        class _DummyResponse:
            def __init__(self, status=200, text="{}"):
                self.status = status
                self._text = text

            async def text(self):
                return self._text

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class ClientSession:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.closed = False

            def post(self, *args, **kwargs):
                return _DummyResponse()

            async def close(self):
                self.closed = True

        aiohttp_module.ClientError = ClientError
        aiohttp_module.ClientTimeout = ClientTimeout
        aiohttp_module.ClientSession = ClientSession
        sys.modules["aiohttp"] = aiohttp_module

    if "aiofiles" not in sys.modules:
        aiofiles_module = types.ModuleType("aiofiles")

        class AsyncFile:
            def __init__(self, path, mode="r", encoding=None):
                self._path = path
                self._mode = mode
                self._encoding = encoding
                self._handle = None

            async def __aenter__(self):
                self._handle = open(self._path, self._mode, encoding=self._encoding)
                return self

            async def __aexit__(self, exc_type, exc, tb):
                if self._handle:
                    self._handle.close()
                return False

            async def read(self):
                return self._handle.read()

            async def write(self, data):
                self._handle.write(data)
                self._handle.flush()
                return len(data)

        def open_file(path, mode="r", encoding=None):
            return AsyncFile(path, mode=mode, encoding=encoding)

        aiofiles_module.open = open_file
        sys.modules["aiofiles"] = aiofiles_module

    if "websockets" not in sys.modules:
        websockets_module = types.ModuleType("websockets")
        server_module = types.ModuleType("websockets.server")
        exceptions_module = types.ModuleType("websockets.exceptions")

        class DummyServer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        class WebSocketServerProtocol:
            remote_address = ("127.0.0.1", 12345)

            async def recv(self):
                raise asyncio.CancelledError()

            async def send(self, _payload):
                return None

            async def close(self):
                return None

        class ConnectionClosed(Exception):
            pass

        async def serve(*args, **kwargs):
            return DummyServer()

        websockets_module.serve = serve
        server_module.WebSocketServerProtocol = WebSocketServerProtocol
        exceptions_module.ConnectionClosed = ConnectionClosed

        sys.modules["websockets"] = websockets_module
        sys.modules["websockets.server"] = server_module
        sys.modules["websockets.exceptions"] = exceptions_module

    if "jieba" not in sys.modules:
        jieba_module = types.ModuleType("jieba")

        def cut(text):
            return str(text or "").split()

        jieba_module.cut = cut
        sys.modules["jieba"] = jieba_module

    if "rank_bm25" not in sys.modules:
        rank_bm25_module = types.ModuleType("rank_bm25")

        class BM25Okapi:
            def __init__(self, tokenized_docs):
                self._docs = tokenized_docs

            def get_scores(self, query_tokens):
                query = set(query_tokens)
                scores = []
                for doc in self._docs:
                    scores.append(float(len(query.intersection(doc))))
                return scores

        rank_bm25_module.BM25Okapi = BM25Okapi
        sys.modules["rank_bm25"] = rank_bm25_module


install_dependency_stubs()

from src.core.models import MessageEvent, MessageHandlingPlan, MessageSegment, MessageType
from src.services.ai_client import AIResponse


def build_event(
    text="hello",
    *,
    message_type=MessageType.PRIVATE.value,
    message_id=1,
    user_id=123,
    image_count=0,
):
    message = []
    if text:
        message.append(MessageSegment.text(text))
    for index in range(image_count):
        message.append(MessageSegment.image(f"image-{message_id}-{index + 1}.jpg"))
    return MessageEvent(
        post_type="message",
        self_id=999,
        user_id=user_id,
        message_type=message_type,
        message_id=message_id,
        message=message,
        group_id=456 if message_type == MessageType.GROUP.value else None,
    )


class DummyAIClient:
    def __init__(self, responses=None):
        self.multimodal_calls = []
        self.text_calls = []
        self.chat_calls = []
        self.responses = list(responses or [])

    def build_text_message(self, role, content):
        payload = {"role": role, "content": content}
        self.text_calls.append(payload)
        return payload

    def build_multimodal_message(self, role, text, images):
        payload = {
            "role": role,
            "content": [{"type": "text", "text": text}],
            "images": images,
        }
        self.multimodal_calls.append(payload)
        return payload

    async def chat_completion(self, **kwargs):
        self.chat_calls.append(kwargs)
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return AIResponse(content="dummy response")


class DummyImageClient:
    def __init__(self, base64_by_file=None):
        self.base64_by_file = dict(base64_by_file or {})
        self.processed_segments = []
        self.closed = False

    async def process_image_segment(self, data):
        self.processed_segments.append(dict(data))
        file_id = data.get("file") or data.get("file_id")
        return self.base64_by_file.get(file_id, f"base64:{file_id}")

    async def close(self):
        self.closed = True


class DummyVisionResult:
    def __init__(
        self,
        *,
        per_image_descriptions=None,
        merged_description="",
        success_count=0,
        failure_count=0,
        source="vision",
        error="",
        sticker_flags=None,
        sticker_confidences=None,
        sticker_reasons=None,
    ):
        self.per_image_descriptions = list(per_image_descriptions or [])
        self.merged_description = merged_description
        self.success_count = success_count
        self.failure_count = failure_count
        self.source = source
        self.error = error
        self.sticker_flags = list(sticker_flags or [])
        self.sticker_confidences = list(sticker_confidences or [])
        self.sticker_reasons = list(sticker_reasons or [])

    def is_sticker(self, index):
        return bool(self.sticker_flags[index]) if 0 <= index < len(self.sticker_flags) else False

    def get_sticker_confidence(self, index):
        return float(self.sticker_confidences[index]) if 0 <= index < len(self.sticker_confidences) else 0.0

    def get_sticker_reason(self, index):
        return str(self.sticker_reasons[index]) if 0 <= index < len(self.sticker_reasons) else ""

    def get_description(self, index):
        return str(self.per_image_descriptions[index]) if 0 <= index < len(self.per_image_descriptions) else ""

    def to_prompt_fields(self):
        return {
            "per_image_descriptions": list(self.per_image_descriptions),
            "merged_description": self.merged_description,
            "vision_success_count": self.success_count,
            "vision_failure_count": self.failure_count,
            "vision_source": self.source,
            "vision_error": self.error,
            "vision_available": bool(
                self.merged_description.strip()
                or any(item.strip() for item in self.per_image_descriptions)
            ),
            "sticker_flags": list(self.sticker_flags),
            "sticker_confidences": list(self.sticker_confidences),
            "sticker_reasons": list(self.sticker_reasons),
            "sticker_count": sum(1 for flag in self.sticker_flags if flag),
        }


class DummyVisionClient:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.emotion_calls = []
        self.closed = False

    async def analyze_images(self, *, base64_images, user_text=""):
        self.calls.append(
            {
                "base64_images": list(base64_images),
                "user_text": user_text,
            }
        )
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return DummyVisionResult()

    async def classify_sticker_emotion(self, *, image_base64, emotion_labels):
        self.emotion_calls.append(
            {
                "image_base64": image_base64,
                "emotion_labels": list(emotion_labels),
            }
        )
        primary = emotion_labels[0] if emotion_labels else ""
        tone = "附和"
        intent = f"{tone}-{primary}" if primary else ""
        return {
            "primary_emotion": primary,
            "confidence": 0.8,
            "reason": "dummy",
            "all_emotions": list(emotion_labels[:1]),
            "reply_tones": [tone] if primary else [],
            "reply_intents": [intent] if intent else [],
        }

    async def close(self):
        self.closed = True


class DummyPlanner:
    pass


class RecordingPlanner:
    def __init__(self, result_action="reply"):
        self.calls = []
        self.result_action = result_action

    async def plan(self, event, user_message, recent_messages, window_messages=None):
        self.calls.append(
            {
                "event": event,
                "user_message": user_message,
                "recent_messages": recent_messages,
                "window_messages": window_messages,
            }
        )
        return MessageHandlingPlan(
            action=self.result_action,
            reason="planned",
            source="planner",
        )


@dataclass
class DummyMemory:
    content: str
    owner_user_id: str = ""


class DummyMemoryManager:
    def __init__(self):
        self.search_called = False
        self.registered_turns = []
        self.extraction_scheduled_for = []

    async def get_important_memories(self, **kwargs):
        return [DummyMemory(content="likes cats", owner_user_id="42")]

    async def search_memories_with_context(self, **kwargs):
        self.search_called = True
        return {"memories": [], "history_messages": []}

    def register_dialogue_turn(self, **kwargs):
        self.registered_turns.append(kwargs)

    def schedule_memory_extraction(self, user_id):
        self.extraction_scheduled_for.append(user_id)
        return asyncio.create_task(asyncio.sleep(0))


class ClosableResource:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


class DummyManagedConnection:
    def __init__(self):
        self.disconnected = False
        self.sent = []
        self.run_started = False

    async def send(self, payload):
        self.sent.append(payload)

    async def disconnect(self):
        self.disconnected = True

    async def run(self):
        self.run_started = True
        await asyncio.sleep(0)


class DummyManagedHandler:
    def __init__(self, active_count=0):
        self.closed = False
        self.active_count = active_count

    async def close(self):
        self.closed = True

    def get_active_conversation_count(self):
        return self.active_count


class FakeHTTPSessionManager:
    def __init__(self, outcome):
        self.outcome = outcome
        self.ensure_calls = 0
        self.close_calls = 0
        self.session = object()

    async def ensure_session(self):
        self.ensure_calls += 1
        return self.session

    async def post_text(self, url, payload):
        result = self.outcome(url, payload)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        self.close_calls += 1


class FakeExtractor:
    def __init__(self, *, should_extract=True, returned_memories=None):
        self.should_extract_value = should_extract
        self.returned_memories = list(returned_memories or [])
        self.turns = []
        self.extract_calls = []

    def add_dialogue_turn(self, user_id, user_message, assistant_message):
        self.turns.append((user_id, user_message, assistant_message))

    def should_extract(self, user_id):
        return self.should_extract_value

    async def extract_memories(self, user_id):
        self.extract_calls.append(user_id)
        return list(self.returned_memories)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))



