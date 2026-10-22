from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crawler_state import CrawlerState
from regulation_versions import (
    build_version_index,
    choose_latest_version,
    date_from_filename,
    dated_stem,
    split_version_title,
)


class VersionRulesTests(unittest.TestCase):
    def test_newest_year_wins_before_title(self):
        records = {
            "a": {
                "version_id": "a",
                "title": "中国共产党章程",
                "publish_date": "2012",
            },
            "b": {
                "version_id": "b",
                "title": "中国共产党章程（2017年修改）",
                "publish_date": "2017",
            },
            "c": {
                "version_id": "c",
                "title": "中国共产党章程（2022年修改）",
                "publish_date": "2022",
            },
        }
        self.assertEqual(
            choose_latest_version(records.values())["version_id"], "c"
        )
        base, label, year = split_version_title("中国共产党章程（２０１７年修改）")
        self.assertEqual(base, "中国共产党章程")
        self.assertIn("2017", label)
        self.assertEqual(year, 2017)

    def test_same_title_uses_date(self):
        records = {
            "old": {
                "version_id": "old",
                "title": "同一法规",
                "publish_date": "2020-01-02",
            },
            "new": {
                "version_id": "new",
                "title": "同一法规",
                "publish_date": "2024-03-04",
            },
        }
        index = build_version_index(records)
        family = next(iter(index.values()))
        self.assertEqual(family["latest_version_id"], "new")
        self.assertEqual(len(family["versions"]), 2)

    def test_latest_version_prefers_newer_year_over_plain_title(self):
        records = {
            "plain": {
                "version_id": "plain",
                "title": "测试条例",
                "publish_date": "2010",
            },
            "revised": {
                "version_id": "revised",
                "title": "测试条例（2017年修改）",
                "publish_date": "2017",
            },
        }
        self.assertEqual(
            choose_latest_version(records.values())["version_id"], "revised"
        )

    def test_dated_stem(self):
        self.assertEqual(dated_stem("法规", "2024-03-04"), "法规_20240304")
        self.assertEqual(dated_stem("法规", "2024"), "法规_2024")
        self.assertEqual(dated_stem("法规_20240304", "2024-03-04"), "法规_20240304")

    def test_filename_date_fallback_supports_revision_parentheses(self):
        self.assertEqual(
            date_from_filename("党章[1] 中国共产党章程（2003年修订）.pdf"),
            "2003",
        )


class StateVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = CrawlerState(Path(self.tmp.name) / "crawler_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_latest_version_is_pending_upload(self):
        self.state.register_document(
            "ccdi:1",
            source="ccdi",
            title="示例条例（2010年修改）",
            url="https://example/1",
            pdf_path="/tmp/1.pdf",
            category_path="条例",
            publish_date="2010",
        )
        self.state.register_document(
            "ccdi:2",
            source="ccdi",
            title="示例条例",
            url="https://example/2",
            pdf_path="/tmp/2.pdf",
            category_path="条例",
            publish_date="2024-03-04",
        )
        pending = self.state.pending_upload_keys(
            "ccdi", 0, 999, immediate=True
        )
        self.assertEqual(pending, ["ccdi:2"])

    def test_replacement_keeps_old_version_record(self):
        self.state.register_document(
            "bit:same",
            source="bit",
            title="同名规定",
            url="https://example/old",
            pdf_path="/tmp/old.pdf",
            publish_date="2023-01-01",
        )
        old_version = self.state.snapshot()["documents"]["bit:same"]["version_id"]
        self.state.mark_uploaded("bit:same", "dataset-old", ragflow_document_id="doc-old")
        self.state.update_document_version(
            "bit:same",
            source="bit",
            title="同名规定",
            url="https://example/new",
            pdf_path="/tmp/new.pdf",
            publish_date="2025-02-02",
        )
        snap = self.state.snapshot()
        current = snap["documents"]["bit:same"]
        self.assertNotEqual(current["version_id"], old_version)
        self.assertIn(old_version, snap["version_records"])
        superseded = self.state.superseded_uploaded_versions("bit:same")
        self.assertEqual(superseded[0]["ragflow_document_id"], "doc-old")

    def test_import_unregistered_local_pdf(self):
        folder = Path(self.tmp.name) / "中纪检" / "条例"
        folder.mkdir(parents=True)
        pdf = folder / "条例[3] 本地规定_20240304.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        imported = self.state.import_unregistered_local_files(Path(self.tmp.name))
        self.assertEqual(imported, 1)
        docs = self.state.list_documents("ccdi")
        self.assertEqual(docs[0]["title"], "本地规定")
        self.assertEqual(docs[0]["publish_date"], "2024-03-04")
        self.assertEqual(docs[0]["category_path"], "条例")

    def test_dates_can_be_backfilled_before_final_index_rebuild(self):
        self.state.register_document(
            "ccdi:20",
            source="ccdi",
            title="索引顺序测试",
            url="https://example/20",
            pdf_path="/tmp/20.pdf",
        )
        before = self.state.snapshot()["version_index"]
        changed = self.state.set_document_publish_dates(
            {"ccdi:20": "2024"}, precision="year", rebuild_index=False
        )
        self.assertEqual(changed, 1)
        self.assertEqual(self.state.snapshot()["version_index"], before)
        self.state.rebuild_version_index()
        index = self.state.snapshot()["version_index"]
        family = next(iter(index.values()))
        self.assertEqual(family["versions"][0]["publish_date"], "2024")

    def test_version_indexes_are_separate_by_source(self):
        self.state.register_document(
            "ccdi:30",
            source="ccdi",
            title="中纪检索引",
            url="https://example/ccdi",
            pdf_path="/tmp/ccdi.pdf",
            publish_date="2024",
        )
        self.state.register_document(
            "bit:30",
            source="bit",
            title="校纪检索引",
            url="https://example/bit",
            pdf_path="/tmp/bit.pdf",
            publish_date="2025-02-03",
        )
        self.state.rebuild_version_index()
        snap = self.state.snapshot()
        self.assertEqual(set(snap["version_indices"]), {"ccdi", "bit"})
        self.assertTrue(snap["version_index_meta"]["sources"]["ccdi"]["ready"])
        self.assertTrue(snap["version_index_meta"]["sources"]["bit"]["ready"])
        self.assertTrue(
            all(
                family["source"] == "ccdi"
                for family in snap["version_indices"]["ccdi"].values()
            )
        )
        self.assertTrue(
            all(
                family["source"] == "bit"
                for family in snap["version_indices"]["bit"].values()
            )
        )

    def test_source_index_pending_preserves_the_other_source(self):
        self.state.register_document(
            "ccdi:31",
            source="ccdi",
            title="中纪检待重建",
            url="https://example/ccdi31",
            pdf_path="/tmp/ccdi31.pdf",
            publish_date="2024",
        )
        self.state.register_document(
            "bit:31",
            source="bit",
            title="校纪检已完成",
            url="https://example/bit31",
            pdf_path="/tmp/bit31.pdf",
            publish_date="2025-01-01",
        )
        self.state.rebuild_version_index()
        self.state.mark_source_index_pending("ccdi", "测试重建")
        meta = self.state.get_version_index_status()
        self.assertFalse(meta["sources"]["ccdi"]["ready"])
        self.assertTrue(meta["sources"]["bit"]["ready"])

    def test_backfill_checkpoint_survives_reload(self):
        self.state.save_backfill_checkpoint(
            "ccdi",
            status="running",
            year=2014,
            page=12,
            next_page=13,
            page_count=49,
            year_filled=7,
        )
        reloaded = CrawlerState(Path(self.tmp.name) / "crawler_state.json")
        status = reloaded.get_backfill_statuses()["ccdi"]
        self.assertEqual(status["year"], 2014)
        self.assertEqual(status["page"], 12)
        self.assertEqual(status["next_page"], 13)

    def test_new_workflow_migration_discards_old_breakpoint_once(self):
        state_path = Path(self.tmp.name) / "crawler_state.json"
        checkpoint_path = Path(self.tmp.name) / "backfill_resume.json"
        state_path.write_text(
            json.dumps(
                {
                    "maintenance_flags": {
                        "schema_version": 3,
                        "ccdi": True,
                        "bit": True,
                    },
                    "documents": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        checkpoint_path.write_text(
            json.dumps({"ccdi": {"year": 2007, "next_page": 13}}),
            encoding="utf-8",
        )
        migrated = CrawlerState(state_path)
        self.assertEqual(
            migrated.get_maintenance_flags(),
            {"schema_version": 4, "ccdi": False, "bit": False},
        )
        self.assertFalse(checkpoint_path.exists())

    def test_missing_date_is_filled_from_revision_filename(self):
        self.state.register_document(
            "ccdi:60",
            source="ccdi",
            title="文件名日期测试",
            url="https://example/60",
            pdf_path="/tmp/文件名日期测试（2003年修订）.pdf",
        )
        counts = self.state.fill_missing_dates_from_filenames(("ccdi",))
        self.assertEqual(counts["ccdi"], 1)
        doc = self.state.snapshot()["documents"]["ccdi:60"]
        self.assertEqual(doc["publish_date"], "2003")
        self.assertEqual(doc["date_source"], "filename")


if __name__ == "__main__":
    unittest.main()
