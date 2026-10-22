from __future__ import annotations

import unittest

from analyze_ccdi_backfill_audit import analyze_state


class BackfillAuditTests(unittest.TestCase):
    def test_counts_year_pages_matches_and_local_dates(self):
        data = {
            "backfill_status": {
                "ccdi": {
                    "year_rows": [
                        {
                            "year": 2020,
                            "year_total_count": 2,
                            "page_results": [
                                {
                                    "page": 1,
                                    "crawled": 2,
                                    "expected": 2,
                                    "web_ids": [1, 2],
                                    "matched_local_ids": ["1", "2"],
                                    "unmatched_ids": [],
                                }
                            ],
                        },
                        {
                            "year": 2019,
                            "year_total_count": 1,
                            "page_results": [
                                {
                                    "page": 1,
                                    "crawled": 1,
                                    "expected": 1,
                                    "web_ids": [2],
                                    "matched_local_ids": [],
                                    "unmatched_ids": [2],
                                }
                            ],
                        },
                    ]
                }
            },
            "documents": {
                "ccdi:1": {
                    "source": "ccdi",
                    "publish_date": "2020",
                    "date_source": "web",
                },
                "ccdi:2": {
                    "source": "ccdi",
                    "publish_date": "2019",
                    "date_source": "filename",
                },
                "ccdi:3": {
                    "source": "ccdi",
                    "publish_date": "",
                    "date_source": "",
                },
                "bit:1": {
                    "source": "bit",
                    "publish_date": "2024-01-01",
                    "date_source": "web",
                },
            },
        }

        report = analyze_state(data)

        self.assertEqual(report["year_expected_count"], 3)
        self.assertEqual(report["page_web_id_count"], 3)
        self.assertEqual(report["page_matched_id_count"], 2)
        self.assertEqual(report["local_dated_count"], 2)
        self.assertEqual(report["unique_web_id_count"], 2)
        self.assertEqual(report["unique_matched_id_count"], 2)
        self.assertEqual(report["repeated_web_id_count"], 1)
        self.assertEqual(report["same_year_repeated_web_id_count"], 0)
        self.assertEqual(report["duplicate_occurrence_count"], 0)
        self.assertEqual(report["unmatched_web_id_count"], 1)
        self.assertEqual(report["matched_dated_count"], 2)
        self.assertEqual(report["matched_missing_date_count"], 0)
        self.assertEqual(report["matched_wrong_year_count"], 0)

    def test_counts_duplicate_ids_once_per_page_entry(self):
        report = analyze_state(
            {
                "backfill_status": {
                    "ccdi": {
                        "year_rows": [
                            {
                                "year": 2018,
                                "year_total_count": 20,
                                "page_results": [
                                    {
                                        "page": 1,
                                        "web_ids": [10, 10, 11],
                                        "matched_local_ids": ["10"],
                                    },
                                    {
                                        "page": 2,
                                        "web_ids": [10],
                                        "matched_local_ids": ["10", "11"],
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
        )

        self.assertEqual(report["year_expected_count"], 20)
        self.assertEqual(report["page_web_id_count"], 4)
        self.assertEqual(report["page_matched_id_count"], 3)
        self.assertEqual(report["local_dated_count"], 0)
        self.assertEqual(report["unique_web_id_count"], 2)
        self.assertEqual(report["repeated_web_id_count"], 0)
        self.assertEqual(report["same_year_repeated_web_id_count"], 1)
        self.assertEqual(report["duplicate_occurrence_count"], 2)
        self.assertEqual(
            report["same_year_duplicate_details"],
            [{"year": "2018", "pages": [1, 2], "web_ids": [10]}],
        )


if __name__ == "__main__":
    unittest.main()
