from __future__ import annotations

import unittest

from crawl_ccdi_regulations import (
    audit_ccdi_match_dates,
    _exact_result_count,
    _extract_result_total_count,
    _extract_search_years,
    _local_ccdi_keys_by_year,
    _resume_page_for_year,
    _update_incomplete_year,
    _update_page_result,
    _update_year_row,
    _year_rows_from_state,
)
from crawler_state import CrawlerState
from pathlib import Path
import tempfile


class CcdiDateParsingTests(unittest.TestCase):
    def test_extracts_years_from_option_values(self):
        values = ["全部年份", "2026", "2025年", "", "not-a-year"]
        self.assertEqual(_extract_search_years(values), [2026, 2025])

    def test_accepts_empty_options(self):
        self.assertEqual(_extract_search_years([]), [])
        self.assertEqual(_extract_search_years(None), [])

    def test_extracts_declared_result_count(self):
        self.assertEqual(_extract_result_total_count("<div>共 93 条</div>"), 93)
        self.assertEqual(
            _extract_result_total_count(
                "<p>检索关键词：&nbsp;&nbsp;||检索数量：156</p>"
            ),
            156,
        )
        self.assertIsNone(_extract_result_total_count("<div>无结果</div>"))

    def test_resumes_same_year_from_saved_next_page(self):
        status = {
            "status": "running",
            "year": 2014,
            "page": 12,
            "next_page": 13,
        }
        page, fields = _resume_page_for_year(status, 2014, 49)
        self.assertEqual(page, 13)
        self.assertEqual(fields["year"], 2014)
        self.assertEqual(_resume_page_for_year(status, 2013, 49)[0], 1)

    def test_exact_result_count_uses_last_page(self):
        self.assertEqual(_exact_result_count(17, 10, 6), 166)
        self.assertEqual(_exact_result_count(1, 10, 7), 7)

    def test_incomplete_year_is_visible_and_clears_after_page_is_fixed(self):
        records = _update_incomplete_year(
            [],
            year=2014,
            page=3,
            crawled=7,
            expected=10,
            year_total=166,
            complete=False,
        )
        self.assertEqual(records[0]["pages"][0]["crawled"], 7)
        records = _update_incomplete_year(
            records,
            year=2014,
            page=3,
            crawled=10,
            expected=10,
            year_total=166,
            complete=True,
        )
        self.assertEqual(records, [])

    def test_year_rows_include_completed_partial_and_pending_years(self):
        rows = _year_rows_from_state(
            [2024, 2023, 2022],
            {2024},
            [
                {
                    "year": 2023,
                    "year_total": 90,
                    "pages": [{"page": 2, "crawled": 7, "expected": 10}],
                }
            ],
        )
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[1]["status"], "partial")
        self.assertEqual(rows[2]["status"], "pending")
        rows = _update_year_row(
            rows,
            2022,
            status="running",
            year_scanned=20,
            year_total_count=100,
        )
        self.assertEqual(rows[2]["year_scanned"], 20)

    def test_local_year_count_uses_metadata_or_filename(self):
        docs = {
            "ccdi:1": {
                "source": "ccdi",
                "publish_date": "2013",
                "pdf_path": "/tmp/a.pdf",
            },
            "ccdi:2": {
                "source": "ccdi",
                "publish_date": "",
                "pdf_path": "/tmp/条例[2] 测试_20130304.pdf",
            },
            "ccdi:3": {
                "source": "ccdi",
                "publish_date": "",
                "pdf_path": "/tmp/no-date.pdf",
            },
        }
        counts = _local_ccdi_keys_by_year(docs)
        self.assertEqual(counts[2013], {"ccdi:1", "ccdi:2"})

    def test_page_crawled_count_is_monotonic_during_retries(self):
        rows = _update_page_result(
            [],
            2013,
            4,
            30,
            30,
            True,
            web_ids=[1, 2, 3],
            matched_local_ids=["1", "2"],
            unmatched_ids=[3],
        )
        rows = _update_page_result(
            rows, 2013, 4, 20, 30, False
        )
        page = rows[0]["page_results"][0]
        self.assertEqual(page["crawled"], 30)
        self.assertFalse(page["complete"])
        self.assertEqual(page["web_ids"], [1, 2, 3])
        self.assertEqual(page["matched_local_ids"], ["1", "2"])
        self.assertEqual(page["unmatched_ids"], [3])

    def test_match_audit_repairs_dates_and_detects_cross_year_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = CrawlerState(Path(tmp) / "crawler_state.json")
            for doc_id in (1, 2, 3):
                state.register_document(
                    f"ccdi:{doc_id}",
                    source="ccdi",
                    title=f"法规 {doc_id}",
                    url=f"https://example/{doc_id}",
                    pdf_path=str(Path(tmp) / f"{doc_id}.pdf"),
                )
            state.set_backfill_status(
                "ccdi",
                status="done",
                extra={
                    "year_rows": [
                        {
                            "year": 2018,
                            "page_results": [
                                {
                                    "page": 1,
                                    "web_ids": [1, 2],
                                    "matched_local_ids": ["1", "2"],
                                    "matched_local_keys": [
                                        "ccdi:1",
                                        "ccdi:2",
                                    ],
                                    "unmatched_ids": [],
                                }
                            ],
                        },
                        {
                            "year": 2017,
                            "page_results": [
                                {
                                    "page": 1,
                                    "web_ids": [1, 3],
                                    "matched_local_ids": ["1", "3"],
                                    "matched_local_keys": [
                                        "ccdi:1",
                                        "ccdi:3",
                                    ],
                                    "unmatched_ids": [],
                                }
                            ],
                        },
                    ]
                },
            )

            report = audit_ccdi_match_dates(state, repair=True)

            self.assertEqual(report["unique_web_count"], 3)
            self.assertEqual(report["unique_matched_count"], 3)
            self.assertEqual(report["repeated_count"], 1)
            self.assertEqual(report["repeated_web_ids"], [1])
            self.assertEqual(report["same_year_repeated_count"], 0)
            self.assertEqual(report["duplicate_occurrence_count"], 0)
            self.assertEqual(report["matched_missing_date_count"], 1)
            self.assertEqual(report["matched_dated_count"], 2)
            self.assertEqual(report["repaired_count"], 2)
            self.assertFalse(report["consistent"])
            docs = state.snapshot()["documents"]
            self.assertEqual(docs["ccdi:2"]["publish_date"], "2018")
            self.assertEqual(docs["ccdi:3"]["publish_date"], "2017")
            self.assertEqual(docs["ccdi:1"]["publish_date"], "")


if __name__ == "__main__":
    unittest.main()
