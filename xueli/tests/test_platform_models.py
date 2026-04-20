from __future__ import annotations

import unittest

from src.core.platform_models import NoopAction, ReplyAction, SessionRef


class PlatformModelsTests(unittest.TestCase):
    def test_session_ref_preserves_conversation_key(self) -> None:
        session = SessionRef(
            platform="qq",
            scope="group",
            conversation_id="group:123:456",
            user_id="456",
            account_id="999",
            channel_id="123",
        )

        self.assertEqual(session.key, "group:123:456")
        self.assertEqual(session.qualified_key, "qq:group:123:456")

    def test_qualified_key_does_not_duplicate_existing_platform_prefix(self) -> None:
        session = SessionRef(
            platform="api",
            scope="private",
            conversation_id="api:session:bridge-1",
            user_id="external-user",
        )

        self.assertEqual(session.key, "api:session:bridge-1")
        self.assertEqual(session.qualified_key, "api:session:bridge-1")

    def test_reply_action_has_reply_type(self) -> None:
        session = SessionRef(platform="qq", scope="private", conversation_id="private:42", user_id="42")
        action = ReplyAction(session=session, text="hello", quote_message_id="1001")

        self.assertEqual(action.action_type, "reply")
        self.assertEqual(action.text, "hello")
        self.assertEqual(action.quote_message_id, "1001")
        self.assertIs(action.session, session)

    def test_noop_action_has_reason(self) -> None:
        action = NoopAction(reason="ignored")

        self.assertEqual(action.action_type, "no_op")
        self.assertEqual(action.reason, "ignored")


if __name__ == "__main__":
    unittest.main()
