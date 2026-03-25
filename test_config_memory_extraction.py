import json
import os
import tempfile
import unittest

from src.core.config import Config


class MemoryExtractionConfigTests(unittest.TestCase):
    def test_config_loader_supports_inline_comments(self):
        raw_config = """
        {
          // main model
          "ai_service": {
            "api_base": "https://main.example/v1", // inline comment
            "api_key": "main-key",
            "model": "main-model",
            "extra_params": {
              "temperature": 0.7 /* block comment */
            },
            "extra_headers": {},
            "response_path": "choices.0.message.content"
          },
          "memory": {
            "enabled": true,
            "extraction_model": null // fallback to main model
          }
        }
        """

        config = self._load_raw_config(raw_config)
        client_config = config.get_memory_extraction_client_config()

        self.assertEqual(client_config["model"], "main-model")
        self.assertEqual(client_config["api_base"], "https://main.example/v1")
        self.assertEqual(client_config["extra_params"], {"temperature": 0.7})

    def test_memory_extraction_falls_back_to_main_model(self):
        config_data = {
            "ai_service": {
                "api_base": "https://main.example/v1",
                "api_key": "main-key",
                "model": "main-model",
                "extra_params": {"temperature": 0.7},
                "extra_headers": {"X-Main": "1"},
                "response_path": "choices.0.message.content",
            },
            "memory": {
                "enabled": True,
                "extraction_model": None,
            },
        }

        config = self._load_config(config_data)
        client_config = config.get_memory_extraction_client_config()

        self.assertEqual(client_config["model"], "main-model")
        self.assertEqual(client_config["api_base"], "https://main.example/v1")
        self.assertEqual(client_config["api_key"], "main-key")
        self.assertEqual(client_config["extra_params"], {"temperature": 0.7})
        self.assertEqual(client_config["extra_headers"], {"X-Main": "1"})
        self.assertEqual(client_config["response_path"], "choices.0.message.content")

    def test_memory_extraction_uses_dedicated_config_when_present(self):
        config_data = {
            "ai_service": {
                "api_base": "https://main.example/v1",
                "api_key": "main-key",
                "model": "main-model",
                "extra_params": {"temperature": 0.7},
                "extra_headers": {"X-Main": "1"},
                "response_path": "choices.0.message.content",
            },
            "memory": {
                "enabled": True,
                "extraction_api_base": "https://memory.example/v1",
                "extraction_api_key": "memory-key",
                "extraction_model": "memory-model",
                "extraction_extra_params": {"temperature": 0.2},
                "extraction_extra_headers": {"X-Memory": "1"},
                "extraction_response_path": "output.text",
            },
        }

        config = self._load_config(config_data)
        client_config = config.get_memory_extraction_client_config()

        self.assertEqual(client_config["model"], "memory-model")
        self.assertEqual(client_config["api_base"], "https://memory.example/v1")
        self.assertEqual(client_config["api_key"], "memory-key")
        self.assertEqual(client_config["extra_params"], {"temperature": 0.2})
        self.assertEqual(client_config["extra_headers"], {"X-Memory": "1"})
        self.assertEqual(client_config["response_path"], "output.text")

    def _load_config(self, config_data):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as temp_file:
            json.dump(config_data, temp_file, ensure_ascii=False)
            temp_path = temp_file.name

        try:
            return Config(path=temp_path)
        finally:
            os.unlink(temp_path)

    def _load_raw_config(self, raw_config):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as temp_file:
            temp_file.write(raw_config)
            temp_path = temp_file.name

        try:
            return Config(path=temp_path)
        finally:
            os.unlink(temp_path)


if __name__ == "__main__":
    unittest.main()
