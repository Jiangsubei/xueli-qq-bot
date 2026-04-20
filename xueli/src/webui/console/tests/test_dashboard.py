from __future__ import annotations

import asyncio
import json
import shutil
import tomllib
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse

from console.services import MASKED_SECRET, build_dashboard_context
from src.core.toml_utils import dumps_toml_document
from src.memory.storage.important_memory_store import ImportantMemoryStore
from src.memory.storage.markdown_store import MarkdownMemoryStore


class DashboardViewTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        repo_root = Path(__file__).resolve().parents[4]
        temp_root = repo_root / '.tmp_tests'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f'webui_dashboard_{uuid4().hex}'
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

        self.config_path = self.temp_dir / 'config.toml'
        self.snapshot_path = self.temp_dir / 'webui_snapshot.json'
        self.avatar_root = self.temp_dir / 'avatar'
        self.emoji_root = self.temp_dir / 'emojis'
        self.emoji_root.mkdir(parents=True, exist_ok=True)
        (self.emoji_root / 'index.json').write_text(
            json.dumps(
                {
                    'version': 2,
                    'items': {
                        'emoji-a': {'emotion_status': 'pending', 'disabled': False},
                        'emoji-b': {'emotion_status': 'classified', 'disabled': False},
                        'emoji-c': {'emotion_status': 'pending', 'disabled': True},
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding='utf-8',
        )
        self.config_path.write_text("# dashboard test config\n" + dumps_toml_document(self._build_config()), encoding='utf-8')
        override = self.settings(
            WEBUI_CONFIG_PATH=self.config_path,
            WEBUI_RUNTIME_SNAPSHOT_PATH=self.snapshot_path,
            WEBUI_SNAPSHOT_TTL_SECONDS=15,
            WEBUI_AVATAR_ROOT=self.avatar_root,
            WEBUI_AVATAR_MAX_BYTES=3 * 1024 * 1024,
        )
        override.enable()
        self.addCleanup(override.disable)

    def _build_config(self):
        return {
            'napcat': {'ws_url': 'ws://127.0.0.1:8095', 'http_url': 'http://127.0.0.1:6700'},
            'ai_service': {'api_base': 'https://api.main.example/v1', 'api_key': 'test-main-secret', 'model': 'main-model', 'extra_params': {}, 'extra_headers': {}, 'response_path': 'choices.0.message.content'},
            'vision_service': {'enabled': True, 'api_base': 'https://api.vision.example/v1', 'api_key': 'test-vision-secret', 'model': 'vision-model', 'extra_params': None, 'extra_headers': None, 'response_path': None},
            'emoji': {'enabled': True, 'storage_path': str(self.emoji_root), 'capture_enabled': True, 'classification_enabled': True, 'idle_seconds_before_classify': 45, 'classification_interval_seconds': 30, 'classification_windows': [], 'emotion_labels': ['开心', '无语'], 'reply_enabled': False, 'reply_cooldown_seconds': 180},
            'bot_behavior': {'max_context_length': 10, 'max_message_length': 4000, 'response_timeout': 60, 'rate_limit_interval': 1.0, 'log_full_prompt': False},
            'assistant_profile': {'name': '测试助手', 'alias': '小助', 'avatar_path': ''},
            'group_reply': {
                'only_reply_when_at': False,
                'interest_reply_enabled': True,
                'plan_request_interval': 3,
                'plan_request_max_parallel': 1,
                'plan_context_message_count': 5,
                'at_user_when_proactive_reply': False,
                'repeat_echo_enabled': True,
                'repeat_echo_window_seconds': 20,
                'repeat_echo_min_count': 2,
                'repeat_echo_cooldown_seconds': 90,
                'burst_merge_enabled': True,
                'burst_window_seconds': 5,
                'burst_min_messages': 3,
                'burst_max_messages': 8,
            },
            'group_reply_decision': {'api_base': 'https://api.group.example/v1', 'api_key': 'test-group-secret', 'model': 'group-model', 'extra_params': None, 'extra_headers': None, 'response_path': None},
            'personality': {'content': '温和耐心'},
            'dialogue_style': {'content': '简洁自然'},
            'behavior': {'content': '避免过度打扰'},
            'memory_rerank': {'api_base': None, 'api_key': None, 'model': None, 'extra_params': None, 'extra_headers': None, 'response_path': None},
            'memory': {
                'enabled': True,
                'storage_path': 'memories',
                'read_scope': 'global',
                'bm25_top_k': 100,
                'rerank_top_k': 20,
                'pre_rerank_top_k': 12,
                'dynamic_memory_limit': 8,
                'dynamic_dedup_enabled': True,
                'dynamic_dedup_similarity_threshold': 0.72,
                'rerank_candidate_max_chars': 160,
                'rerank_total_prompt_budget': 2400,
                'auto_extract': True,
                'extract_every_n_turns': 3,
                'extraction_api_base': None,
                'extraction_api_key': None,
                'extraction_model': None,
                'extraction_extra_params': None,
                'extraction_extra_headers': None,
                'extraction_response_path': None,
                'ordinary_decay_enabled': True,
                'ordinary_half_life_days': 30,
                'ordinary_forget_threshold': 0.5,
                'local_bm25_weight': 1.0,
                'local_importance_weight': 0.35,
                'local_mention_weight': 0.2,
                'local_recency_weight': 0.15,
                'local_scene_weight': 0.3,
            },
        }
    def _read_config(self):
        with self.config_path.open('rb') as handle:
            return tomllib.load(handle)

    def _write_snapshot(self, *, snapshot_at: datetime | None = None, connected: bool = True):
        payload = {
            'snapshot_at': (snapshot_at or datetime.now(timezone.utc)).isoformat(),
            'ready': True,
            'connected': connected,
            'uptime_seconds': 3661,
            'last_error_at': None,
            'assistant': {'name': 'Test Assistant', 'alias': 'Buddy'},
            'services': {'vision_status': 'enabled', 'memory_enabled': True, 'emoji_enabled': True},
            'messages': {'messages_received': 12, 'messages_replied': 8, 'reply_parts_sent': 15, 'message_errors': 1},
            'activity': {'active_conversations': 3, 'active_message_tasks': 2, 'background_tasks': 4},
            'planner': {},
            'vision': {},
            'emoji': {'emoji_total': 25, 'emoji_pending_classification': 4},
            'memory': {'memory_reads': 7, 'memory_writes': 2},
        }
        self.snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_dashboard_returns_sections_and_avatar_trigger(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('WebUI', content)
        self.assertIn('assistantAvatarTrigger', content)
        self.assertIn('page-memory', content)
        self.assertIn('assistantAvatarTrigger', content)
        for section_id in ['page-home', 'page-status', 'page-network', 'page-model', 'page-reply', 'page-emoji', 'page-memory', 'page-recall']:
            self.assertIn(section_id, content)

    def test_dashboard_hides_real_secrets_but_keeps_mask(self):
        response = self.client.get(reverse('dashboard'))
        content = response.content.decode('utf-8')
        self.assertNotIn('test-main-secret', content)
        self.assertNotIn('test-group-secret', content)
        self.assertNotIn('test-vision-secret', content)
        self.assertIn(MASKED_SECRET, content)

    def test_dashboard_uses_runtime_snapshot_and_real_emoji_stats(self):
        self._write_snapshot()
        response = self.client.get(reverse('dashboard-data'))
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['runtime']['online'])
        self.assertEqual(payload['runtime']['assistant_name'], 'Test Assistant')
        self.assertEqual(payload['runtime']['messages_received'], '12')
        self.assertEqual(payload['runtime']['messages_replied'], '15')
        self.assertEqual(payload['runtime']['active_tasks'], '2')
        self.assertEqual(payload['runtime']['emoji_total'], '3')
        self.assertEqual(payload['runtime']['emoji_pending'], '1')

    def test_dashboard_marks_stale_snapshot_offline(self):
        self._write_snapshot(snapshot_at=datetime.now(timezone.utc) - timedelta(seconds=60))
        response = self.client.get(reverse('dashboard-data'))
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload['runtime']['online'])
        self.assertTrue(payload['runtime']['online_label'])
        self.assertEqual(payload['runtime']['messages_received'], '--')
        self.assertEqual(payload['runtime']['emoji_total'], '3')

    def test_model_save_keeps_masked_api_key(self):
        response = self.client.post(
            reverse('save-model-settings'),
            data=json.dumps(
                {
                    'ai_service': {'api_base': 'https://api.changed.example/v1', 'model': 'changed-model', 'api_key': MASKED_SECRET},
                    'group_reply_decision': {'api_base': 'https://api.group.example/v2', 'model': 'group-model-v2', 'api_key': MASKED_SECRET},
                    'vision_service': {'api_base': 'https://api.vision.example/v2', 'model': 'vision-model-v2', 'api_key': MASKED_SECRET},
                    'memory_rerank': {'api_base': 'https://api.rerank.example/v2', 'model': 'rerank-model-v2', 'api_key': ''},
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = self._read_config()
        self.assertEqual(payload['ai_service']['api_key'], 'test-main-secret')
        self.assertEqual(payload['group_reply_decision']['api_key'], 'test-group-secret')
        self.assertEqual(payload['vision_service']['api_key'], 'test-vision-secret')
        self.assertNotIn('api_key', payload['memory_rerank'])
        self.assertEqual(payload['ai_service']['model'], 'changed-model')

    def test_memory_save_preserves_comments_and_unknown_sections(self):
        self.config_path.write_text(
            self.config_path.read_text(encoding='utf-8') + "\n# keep this comment\n[custom_section]\nflag = true\n",
            encoding='utf-8',
        )

        response = self.client.post(
            reverse('save-memory-settings'),
            data=json.dumps(
                {
                    'enabled': True,
                    'auto_extract': True,
                    'read_scope': 'global',
                    'bm25_top_k': 18,
                    'rerank_top_k': 9,
                    'extract_every_n_turns': 4,
                    'ordinary_decay_enabled': True,
                    'ordinary_half_life_days': 30,
                    'ordinary_forget_threshold': 0.5,
                    'storage_path': 'memories',
                }
            ),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        updated_text = self.config_path.read_text(encoding='utf-8')
        self.assertIn('# dashboard test config', updated_text)
        self.assertIn('# keep this comment', updated_text)
        payload = self._read_config()
        self.assertTrue(payload['custom_section']['flag'])

    def test_non_model_save_does_not_recreate_absent_optional_model_sections(self):
        reduced_config = self._build_config()
        reduced_config.pop('group_reply_decision', None)
        reduced_config.pop('vision_service', None)
        reduced_config.pop('memory_rerank', None)
        self.config_path.write_text("# dashboard test config\n" + dumps_toml_document(reduced_config), encoding='utf-8')

        response = self.client.post(
            reverse('save-memory-settings'),
            data=json.dumps(
                {
                    'enabled': True,
                    'auto_extract': True,
                    'read_scope': 'global',
                    'bm25_top_k': 24,
                    'rerank_top_k': 12,
                    'extract_every_n_turns': 5,
                    'ordinary_decay_enabled': False,
                    'ordinary_half_life_days': 45,
                    'ordinary_forget_threshold': 0.35,
                    'storage_path': 'data/custom_memories',
                }
            ),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = self._read_config()
        self.assertNotIn('group_reply_decision', payload)
        self.assertNotIn('vision_service', payload)
        self.assertNotIn('memory_rerank', payload)

    def test_invalid_memory_settings_return_400(self):
        response = self.client.post(
            reverse('save-memory-settings'),
            data=json.dumps({'enabled': True, 'auto_extract': True, 'read_scope': 'broken', 'bm25_top_k': 10}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['message'], '\u8bbe\u7f6e\u6709\u95ee\u9898\uff0c\u8bf7\u68c0\u67e5\u540e\u518d\u8bd5')


    def test_dashboard_renders_advanced_setting_cards(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="advancedToggleButton"', content)
        self.assertNotIn('advanced-callout-card', content)
        self.assertIn('memory_extraction__api_base', content)
        self.assertIn('memory_rerank__api_base', content)
        self.assertIn('ai_service__temperature', content)
        self.assertIn('class="config-editor-body" data-kv-field="extra_params_rows"', content)
        self.assertIn('data-kv-field="extra_params_rows"', content)
        self.assertIn('data-window-list', content)
        self.assertIn('data-tag-list', content)
        self.assertIn('name="repeat_echo_window_seconds"', content)
        self.assertIn('name="dynamic_memory_limit"', content)
        self.assertNotIn('name="rerank_enabled"', content)

    def test_model_save_persists_advanced_fields(self):
        response = self.client.post(
            reverse('save-model-settings'),
            data=json.dumps(
                {
                    'ai_service': {
                        'api_base': 'https://api.changed.example/v2',
                        'model': 'reply-v2',
                        'api_key': MASKED_SECRET,
                        'temperature': '0.7',
                        'response_path': 'data.reply',
                        'extra_params_rows': [{'key': 'stream', 'value': 'true'}],
                        'extra_headers_rows': [{'key': 'X-Test', 'value': 'reply'}],
                    },
                    'group_reply_decision': {
                        'api_base': 'https://api.group.example/v3',
                        'model': 'judge-v3',
                        'api_key': MASKED_SECRET,
                        'response_path': 'payload.answer',
                        'extra_params_rows': [{'key': 'top_p', 'value': '0.8'}],
                        'extra_headers_rows': [{'key': 'X-Group', 'value': 'judge'}],
                    },
                    'vision_service': {
                        'api_base': 'https://api.vision.example/v3',
                        'model': 'vision-v3',
                        'api_key': MASKED_SECRET,
                        'response_path': 'vision.text',
                        'extra_params_rows': [{'key': 'detail', 'value': 'high'}],
                        'extra_headers_rows': [{'key': 'X-Vision', 'value': 'enabled'}],
                    },
                    'memory_rerank': {
                        'api_base': 'https://api.rerank.example/v1',
                        'model': 'rerank-v1',
                        'api_key': 'rerank-secret',
                        'temperature': '0.2',
                        'response_path': 'result.rerank',
                        'extra_params_rows': [],
                        'extra_headers_rows': [{'key': 'X-Rerank', 'value': 'enabled'}],
                    },
                    'memory_extraction': {
                        'api_base': 'https://api.memory.example/v1',
                        'model': 'memory-v1',
                        'api_key': 'memory-secret',
                        'temperature': '0.1',
                        'response_path': 'result.memory',
                        'extra_params_rows': [{'key': 'max_tokens', 'value': '512'}],
                        'extra_headers_rows': [{'key': 'X-Memory', 'value': 'extract'}],
                    },
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = self._read_config()
        self.assertEqual(payload['ai_service']['extra_params']['temperature'], 0.7)
        self.assertEqual(payload['ai_service']['extra_params']['stream'], True)
        self.assertEqual(payload['ai_service']['extra_headers']['X-Test'], 'reply')
        self.assertEqual(payload['ai_service']['response_path'], 'data.reply')
        self.assertEqual(payload['group_reply_decision']['extra_params']['top_p'], 0.8)
        self.assertEqual(payload['group_reply_decision']['extra_headers']['X-Group'], 'judge')
        self.assertEqual(payload['vision_service']['extra_params']['detail'], 'high')
        self.assertEqual(payload['vision_service']['extra_headers']['X-Vision'], 'enabled')
        self.assertEqual(payload['memory_rerank']['api_base'], 'https://api.rerank.example/v1')
        self.assertEqual(payload['memory_rerank']['model'], 'rerank-v1')
        self.assertEqual(payload['memory_rerank']['api_key'], 'rerank-secret')
        self.assertEqual(payload['memory_rerank']['extra_params']['temperature'], 0.2)
        self.assertEqual(payload['memory_rerank']['extra_headers']['X-Rerank'], 'enabled')
        self.assertEqual(payload['memory_rerank']['response_path'], 'result.rerank')
        self.assertEqual(payload['memory']['extraction_api_base'], 'https://api.memory.example/v1')
        self.assertEqual(payload['memory']['extraction_model'], 'memory-v1')
        self.assertEqual(payload['memory']['extraction_api_key'], 'memory-secret')
        self.assertEqual(payload['memory']['extraction_extra_params']['temperature'], 0.1)
        self.assertEqual(payload['memory']['extraction_extra_params']['max_tokens'], 512)
        self.assertEqual(payload['memory']['extraction_extra_headers']['X-Memory'], 'extract')
        self.assertEqual(payload['memory']['extraction_response_path'], 'result.memory')

    def test_assistant_save_persists_advanced_fields(self):
        response = self.client.post(
            reverse('save-assistant-settings'),
            data=json.dumps(
                {
                    'name': 'Assistant',
                    'alias': 'Buddy',
                    'max_context_length': 18,
                    'max_message_length': 6000,
                    'response_timeout': 90,
                    'group_strategy': 'mixed',
                    'personality': 'warm',
                    'dialogue_style': 'clear',
                    'rate_limit_interval': 2.5,
                    'log_full_prompt': True,
                    'private_quote_reply_enabled': True,
                    'plan_request_interval': 8,
                    'plan_request_max_parallel': 3,
                    'plan_context_message_count': 9,
                    'at_user_when_proactive_reply': True,
                    'repeat_echo_enabled': True,
                    'repeat_echo_window_seconds': 18,
                    'repeat_echo_min_count': 4,
                    'repeat_echo_cooldown_seconds': 120,
                    'behavior': 'stay concise',
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = self._read_config()
        self.assertEqual(payload['bot_behavior']['rate_limit_interval'], 2.5)
        self.assertTrue(payload['bot_behavior']['log_full_prompt'])
        self.assertTrue(payload['bot_behavior']['private_quote_reply_enabled'])
        self.assertEqual(payload['group_reply']['plan_request_interval'], 8)
        self.assertEqual(payload['group_reply']['plan_request_max_parallel'], 3)
        self.assertEqual(payload['group_reply']['plan_context_message_count'], 9)
        self.assertTrue(payload['group_reply']['at_user_when_proactive_reply'])
        self.assertNotIn('burst_merge_enabled', payload['group_reply'])
        self.assertNotIn('burst_window_seconds', payload['group_reply'])
        self.assertTrue(payload['group_reply']['repeat_echo_enabled'])
        self.assertEqual(payload['group_reply']['repeat_echo_window_seconds'], 18.0)
        self.assertEqual(payload['group_reply']['repeat_echo_min_count'], 4)
        self.assertEqual(payload['group_reply']['repeat_echo_cooldown_seconds'], 120.0)
        self.assertNotIn('burst_min_messages', payload['group_reply'])
        self.assertNotIn('burst_max_messages', payload['group_reply'])
        self.assertEqual(payload['behavior']['content'], 'stay concise')

    def test_emoji_save_persists_advanced_fields(self):
        response = self.client.post(
            reverse('save-emoji-settings'),
            data=json.dumps(
                {
                    'enabled': True,
                    'capture_enabled': True,
                    'classification_enabled': True,
                    'reply_enabled': True,
                    'idle_seconds_before_classify': 120,
                    'classification_interval_seconds': 45,
                    'classification_windows': [{'start': '09:00', 'end': '11:30'}, {'start': '18:00', 'end': '20:00'}],
                    'emotion_labels': ['happy', 'calm', 'surprised'],
                    'reply_cooldown_seconds': 240,
                    'storage_path': 'data/custom_emojis',
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = self._read_config()
        self.assertEqual(payload['emoji']['idle_seconds_before_classify'], 120)
        self.assertEqual(payload['emoji']['classification_interval_seconds'], 45)
        self.assertEqual(payload['emoji']['classification_windows'], ['09:00-11:30', '18:00-20:00'])
        self.assertEqual(payload['emoji']['emotion_labels'], ['happy', 'calm', 'surprised'])
        self.assertEqual(payload['emoji']['reply_cooldown_seconds'], 240)
        self.assertEqual(payload['emoji']['storage_path'], 'data/custom_emojis')

    def test_memory_save_persists_advanced_fields(self):
        response = self.client.post(
            reverse('save-memory-settings'),
            data=json.dumps(
                {
                    'enabled': True,
                    'auto_extract': True,
                    'read_scope': 'global',
                    'bm25_top_k': 24,
                    'rerank_top_k': 12,
                    'extract_every_n_turns': 5,
                    'pre_rerank_top_k': 10,
                    'dynamic_memory_limit': 6,
                    'dynamic_dedup_enabled': False,
                    'dynamic_dedup_similarity_threshold': 0.61,
                    'rerank_candidate_max_chars': 120,
                    'rerank_total_prompt_budget': 900,
                    'ordinary_decay_enabled': False,
                    'ordinary_half_life_days': 45,
                    'ordinary_forget_threshold': 0.35,
                    'storage_path': 'data/custom_memories',
                    'local_bm25_weight': 0.9,
                    'local_importance_weight': 0.4,
                    'local_mention_weight': 0.25,
                    'local_recency_weight': 0.18,
                    'local_scene_weight': 0.5,
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = self._read_config()
        self.assertEqual(payload['memory']['bm25_top_k'], 24)
        self.assertEqual(payload['memory']['rerank_top_k'], 12)
        self.assertEqual(payload['memory']['pre_rerank_top_k'], 10)
        self.assertEqual(payload['memory']['dynamic_memory_limit'], 6)
        self.assertFalse(payload['memory']['dynamic_dedup_enabled'])
        self.assertEqual(payload['memory']['dynamic_dedup_similarity_threshold'], 0.61)
        self.assertEqual(payload['memory']['rerank_candidate_max_chars'], 120)
        self.assertEqual(payload['memory']['rerank_total_prompt_budget'], 900)
        self.assertNotIn('rerank_enabled', payload['memory'])
        self.assertEqual(payload['memory']['extract_every_n_turns'], 5)
        self.assertNotIn('conversation_save_interval', payload['memory'])
        self.assertFalse(payload['memory']['ordinary_decay_enabled'])
        self.assertEqual(payload['memory']['ordinary_half_life_days'], 45.0)
        self.assertEqual(payload['memory']['ordinary_forget_threshold'], 0.35)
        self.assertEqual(payload['memory']['storage_path'], 'data/custom_memories')
        self.assertEqual(payload['memory']['local_bm25_weight'], 0.9)
        self.assertEqual(payload['memory']['local_importance_weight'], 0.4)
        self.assertEqual(payload['memory']['local_mention_weight'], 0.25)
        self.assertEqual(payload['memory']['local_recency_weight'], 0.18)
        self.assertEqual(payload['memory']['local_scene_weight'], 0.5)

    def test_avatar_upload_updates_config_and_serves_file(self):
        upload = SimpleUploadedFile('avatar.png', bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x50, 0x4E, 0x47]), content_type='image/png')
        response = self.client.post(reverse('assistant-avatar-upload'), data={'avatar': upload})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertIn('avatar_url', payload)

        config_payload = self._read_config()
        self.assertEqual(config_payload['assistant_profile']['avatar_path'], 'data/webui/avatar/assistant.png')
        self.assertTrue((self.avatar_root / 'assistant.png').exists())

        avatar_response = self.client.get(reverse('assistant-avatar'))
        self.assertEqual(avatar_response.status_code, 200)
        self.assertEqual(avatar_response.headers['Content-Type'], 'image/png')
        self.assertGreater(len(b''.join(avatar_response.streaming_content)), 0)

    def test_invalid_avatar_upload_returns_400(self):
        upload = SimpleUploadedFile('avatar.txt', b'not-image', content_type='text/plain')
        response = self.client.post(reverse('assistant-avatar-upload'), data={'avatar': upload})
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload['ok'])

    def test_recall_endpoint_returns_placeholder_items(self):
        response = self.client.get(reverse('recall-data'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'items': []})


    def test_dashboard_sets_csrf_cookie_for_ajax_posts(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)

    def test_memory_items_endpoint_groups_and_updates_real_memories(self):
        memory_root = self.temp_dir / 'memories'
        storage = MarkdownMemoryStore(base_path=str(memory_root))
        important_store = ImportantMemoryStore(base_path=str(memory_root / 'important'))

        async def seed():
            await important_store.add_memory('u100', 'Important memory', source='manual', priority=3, metadata={'source_message_type': 'private'})
            await storage.add_memory('Group memory', user_id='u200', source='manual', metadata={'source_message_type': 'group', 'group_id': 'g1', 'applicability_scope': {'kind': 'group', 'group_id': 'g1'}})
            await storage.add_memory('Private memory', user_id='u300', source='manual', metadata={'source_message_type': 'private'})

        asyncio.run(seed())

        response = self.client.get(reverse('memory-items'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        sections = {section['key']: section for section in payload['sections']}
        self.assertEqual(len(sections['important']['items']), 1)
        self.assertEqual(len(sections['group']['items']), 1)
        self.assertEqual(len(sections['private']['items']), 1)

        private_item = sections['private']['items'][0]
        update_response = self.client.post(
            reverse('memory-item-update'),
            data=json.dumps({'id': private_item['id'], 'kind': private_item['kind'], 'owner_user_id': private_item['owner_user_id'], 'content': 'Updated private memory'}),
            content_type='application/json',
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertTrue(update_response.json()['ok'])

        refreshed = self.client.get(reverse('memory-items')).json()
        private_after = next(section for section in refreshed['sections'] if section['key'] == 'private')['items'][0]
        self.assertEqual(private_after['content'], 'Updated private memory')

        delete_response = self.client.post(
            reverse('memory-item-delete'),
            data=json.dumps({'id': private_after['id'], 'kind': private_after['kind'], 'owner_user_id': private_after['owner_user_id']}),
            content_type='application/json',
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(delete_response.json()['ok'])

        deleted = self.client.get(reverse('memory-items')).json()
        private_items = next(section for section in deleted['sections'] if section['key'] == 'private')['items']
        self.assertEqual(private_items, [])


    @patch("console.views.restart_backend_runtime")
    def test_restart_runtime_endpoint_returns_json(self, restart_runtime_mock):
        restart_runtime_mock.return_value = {"ok": True, "message": "restarted", "state": "running"}
        response = self.client.post(reverse("runtime-restart"), data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "restarted")

    @patch("console.views.restart_backend_runtime")
    def test_restart_runtime_endpoint_surfaces_failures(self, restart_runtime_mock):
        restart_runtime_mock.side_effect = RuntimeError("閲嶅惎澶辫触锛岃鏌ョ湅鏃ュ織")
        response = self.client.post(reverse("runtime-restart"), data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    @patch("console.services._supervisor_runtime_state")
    def test_dashboard_runtime_payload_shows_restarting_state(self, supervisor_state_mock):
        supervisor_state_mock.return_value = {"state": "restarting", "last_error": ""}
        response = self.client.get(reverse("dashboard-data"))
        payload = response.json()
        self.assertEqual(payload["runtime"]["run_status"], "重启中")
        self.assertEqual(payload["runtime"]["online_label"], "重启中")

    def test_memory_extraction_card_uses_main_model_when_dedicated_unconfigured(self):
        context = build_dashboard_context()
        item = next(entry for entry in context['model_forms'] if entry['slug'] == 'memory_extraction')
        self.assertEqual('调用主模型提取', item['status_label'])
        self.assertIn('主模型', item['description'])

    def test_memory_extraction_card_shows_unavailable_when_main_model_also_missing(self):
        config_payload = self._build_config()
        config_payload['ai_service']['api_base'] = ''
        config_payload['ai_service']['model'] = ''
        self.config_path.write_text("# dashboard test config\n" + dumps_toml_document(config_payload), encoding='utf-8')

        context = build_dashboard_context()
        item = next(entry for entry in context['model_forms'] if entry['slug'] == 'memory_extraction')
        self.assertEqual('无法提取', item['status_label'])
        self.assertIn('无法提取', item['description'])

    def test_model_save_marks_vision_enabled_without_api_key(self):
        response = self.client.post(
            reverse('save-model-settings'),
            data=json.dumps(
                {
                    'vision_service': {
                        'api_base': 'https://api.vision.example/v9',
                        'model': 'vision-v9',
                        'api_key': '',
                    }
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = self._read_config()
        self.assertTrue(payload['vision_service']['enabled'])

