from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler_state import CrawlerState
from main import JobController


class StartupIndexTests(unittest.TestCase):
    def _controller(self, root: Path, state: CrawlerState) -> JobController:
        return JobController(
            state,
            {
                "output_dir": str(root),
                "crawl": {"playwright_headless": True},
                "upload": {},
                "ragflow": {},
                "schedule": {},
                "logging": {},
            },
        )

    def test_reload_backfills_missing_dates_then_builds_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.register_document(
                "bit:startup",
                source="bit",
                title="启动补日期测试",
                url="https://example/startup",
                pdf_path=str(root / "startup.pdf"),
            )
            controller = self._controller(root, state)

            def fake_backfill(target_state, *, rebuild_index=True, **_kwargs):
                target_state.set_document_publish_dates(
                    {"bit:startup": "2024-03-04"},
                    precision="day",
                    rebuild_index=rebuild_index,
                )
                return 1

            with patch("main.backfill_bit_publish_dates", side_effect=fake_backfill) as mocked:
                result = controller.prepare_version_index_on_startup()
            self.assertTrue(result["ready"])
            mocked.assert_called_once()
            meta = state.get_version_index_status()
            self.assertTrue(meta["ready"])
            self.assertEqual(meta["missing_dates"], 0)

    def test_ready_index_does_not_repeat_network_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.register_document(
                "bit:ready",
                source="bit",
                title="已就绪测试",
                url="https://example/ready",
                pdf_path=str(root / "ready.pdf"),
                publish_date="2025-01-02",
            )
            controller = self._controller(root, state)
            with patch("main.backfill_bit_publish_dates") as bit_backfill, patch(
                "main.backfill_ccdi_publish_years"
            ) as ccdi_backfill:
                result = controller.prepare_version_index_on_startup()
            self.assertTrue(result["ready"])
            bit_backfill.assert_not_called()
            ccdi_backfill.assert_not_called()

    def test_source_error_is_kept_in_that_sources_index_card_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.register_document(
                "bit:ok",
                source="bit",
                title="校纪检已完成",
                url="https://example/bit-ok",
                pdf_path=str(root / "bit-ok.pdf"),
                publish_date="2025-01-01",
            )
            state.register_document(
                "ccdi:40",
                source="ccdi",
                title="中纪检待补",
                url="https://example/ccdi-40",
                pdf_path=str(root / "ccdi-40.pdf"),
            )
            controller = self._controller(root, state)
            with patch(
                "main.backfill_ccdi_publish_years",
                side_effect=RuntimeError("测试补录错误"),
            ):
                result = controller.prepare_version_index_on_startup()
            self.assertFalse(result["ready"])
            snap = state.snapshot()
            self.assertIn("测试补录错误", snap["version_index_meta"]["sources"]["ccdi"]["error"])
            self.assertTrue(snap["version_index_meta"]["sources"]["bit"]["ready"])
            self.assertEqual(
                snap["backfill_status"]["ccdi"]["status"], "error"
            )

    def test_new_date_maintenance_clears_existing_year_page_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.register_document(
                "ccdi:50",
                source="ccdi",
                title="断点法规",
                url="https://example/ccdi-50",
                pdf_path=str(root / "ccdi-50.pdf"),
            )
            state.set_backfill_status(
                "ccdi",
                status="running",
                current=100,
                total=200,
                message="2007 年补录中",
                extra={"force_full": True},
            )
            state.save_backfill_checkpoint(
                "ccdi",
                status="running",
                year=2007,
                page=12,
                next_page=13,
                page_count=20,
            )
            controller = self._controller(root, state)

            def fake_ccdi(target_state, **_kwargs):
                status = target_state.get_backfill_statuses()["ccdi"]
                self.assertIsNone(status.get("year"))
                self.assertEqual(status["next_page"], 1)
                self.assertFalse(
                    (root / "backfill_resume.json").exists()
                )
                return 0

            with patch(
                "main.backfill_ccdi_publish_years", side_effect=fake_ccdi
            ):
                controller.prepare_version_index_on_startup()
            status = state.get_backfill_statuses()["ccdi"]
            self.assertIsNone(status.get("year"))
            self.assertEqual(status["next_page"], 1)

    def test_true_maintenance_flags_skip_all_network_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.set_all_maintenance_flags(True)
            controller = self._controller(root, state)
            with patch("main.backfill_ccdi_publish_years") as ccdi, patch(
                "main.backfill_bit_publish_dates"
            ) as bit:
                result = controller.prepare_version_index_on_startup()
            self.assertTrue(result["ready"])
            ccdi.assert_not_called()
            bit.assert_not_called()

    def test_first_run_forces_every_ccdi_year_to_be_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.set_first_run(True)
            state.set_all_maintenance_flags(False)
            controller = self._controller(root, state)
            with patch.object(
                controller,
                "run_date_index_maintenance",
                return_value={"ccdi": True, "bit": True, "errors": {}},
            ) as run:
                controller.prepare_version_index_on_startup()
            run.assert_called_once_with(
                ("ccdi", "bit"), force_full_dates=True
            )

    def test_saved_first_run_intent_survives_startup_reconciliation_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            # 模拟 reconcile_startup 已把标志清掉，但本次 reload 仍携带重跑意图。
            state.set_first_run(False)
            state.set_all_maintenance_flags(False)
            controller = self._controller(root, state)
            with patch.object(
                controller,
                "run_date_index_maintenance",
                return_value={"ccdi": True, "bit": True, "errors": {}},
            ) as run:
                controller.prepare_version_index_on_startup(
                    force_full_dates=True
                )
            run.assert_called_once_with(
                ("ccdi", "bit"), force_full_dates=True
            )

    def test_completed_date_maintenance_keeps_unmatched_audit_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.register_document(
                "ccdi:60",
                source="ccdi",
                title="2018 年法规",
                url="https://example/ccdi-60",
                pdf_path=str(root / "ccdi-60.pdf"),
                publish_date="2018",
            )
            state.set_backfill_status(
                "ccdi",
                status="done",
                current=1,
                total=1,
                message="年份扫描完成",
                extra={
                    "unmatched_local_count": 2,
                    "unmatched_local_ids": ["ccdi:61", "ccdi:62"],
                    "year_rows": [
                        {
                            "year": 2018,
                            "status": "done",
                            "page_results": [
                                {
                                    "page": 1,
                                    "web_ids": [60],
                                    "matched_local_ids": ["60"],
                                    "unmatched_ids": [],
                                }
                            ],
                        }
                    ],
                    "incomplete_years": [],
                },
            )
            controller = self._controller(root, state)

            controller._maintain_source("ccdi")

            status = state.get_backfill_statuses()["ccdi"]
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["unmatched_local_count"], 2)
            self.assertEqual(
                status["unmatched_local_ids"], ["ccdi:61", "ccdi:62"]
            )
            self.assertEqual(status["year_rows"][0]["year"], 2018)
            self.assertEqual(
                status["year_rows"][0]["page_results"][0]["web_ids"], [60]
            )

    def test_no_daily_update_does_not_run_maintenance_or_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.set_all_maintenance_flags(True)
            controller = self._controller(root, state)
            controller._last_crawl_results = {"ccdi": 0, "bit": 0}
            with patch.object(controller, "run_date_index_maintenance") as run, patch.object(
                controller, "upload_now_sync"
            ) as upload:
                controller._post_crawl_maintenance_and_upload(
                    "all", was_first_run=False
                )
            run.assert_not_called()
            upload.assert_not_called()

    def test_update_invalidates_flag_then_maintains_and_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            state.set_all_maintenance_flags(True)
            controller = self._controller(root, state)
            controller._last_crawl_results = {"ccdi": 2, "bit": 0}

            def maintenance(sources, **_kwargs):
                self.assertEqual(tuple(sources), ("ccdi",))
                state.set_all_maintenance_flags(True)
                return {"ccdi": True, "bit": True, "errors": {}}

            with patch.object(
                controller, "run_date_index_maintenance", side_effect=maintenance
            ), patch.object(controller, "upload_now_sync") as upload:
                controller._post_crawl_maintenance_and_upload(
                    "ccdi", was_first_run=False
                )
            upload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
