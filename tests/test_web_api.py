from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crawler_state import CrawlerState
from main import JobController
from monitor_web_ui import create_monitor_app


class WebApiTests(unittest.TestCase):
    def test_status_and_version_family_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.register_document(
                "ccdi:1",
                source="ccdi",
                title="测试条例（2010年修改）",
                url="https://example/1",
                pdf_path=str(root / "old.pdf"),
                publish_date="2010",
            )
            state.register_document(
                "ccdi:2",
                source="ccdi",
                title="测试条例",
                url="https://example/2",
                pdf_path=str(root / "new.pdf"),
                publish_date="2024",
            )
            config = {
                "output_dir": str(root),
                "crawl": {},
                "upload": {},
                "ragflow": {"base_url": "", "api_key": ""},
                "schedule": {},
                "logging": {},
            }
            controller = JobController(state, config)
            app = create_monitor_app(controller)
            client = app.test_client()
            self.assertEqual(client.get("/").status_code, 200)
            status_response = client.get("/api/status")
            self.assertEqual(status_response.status_code, 200)
            status_payload = status_response.get_json()
            self.assertIn("date_progress", status_payload["summary"])
            self.assertIn("backfill_status", status_payload["summary"])
            payload = client.get("/api/files").get_json()
            self.assertEqual(payload["total"], 2)
            self.assertEqual(len(payload["multi_versions"]), 1)
            family = payload["multi_versions"][0]
            self.assertEqual(family["latest_document_key"], "ccdi:2")
            self.assertEqual(len(family["versions"]), 2)


if __name__ == "__main__":
    unittest.main()
