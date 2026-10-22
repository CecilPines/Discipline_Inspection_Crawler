from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from crawl_bit_jwjc_regulations import BitListItem, backfill_bit_publish_dates
from crawler_state import CrawlerState


class BitBackfillResumeTests(unittest.TestCase):
    def test_restart_starts_from_next_catalog_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = CrawlerState(root / "crawler_state.json")
            for slug in ("page1", "page2"):
                state.register_document(
                    f"bit:{slug}",
                    source="bit",
                    title=slug,
                    url=f"https://example/{slug}",
                    pdf_path=str(root / f"{slug}.pdf"),
                )
            index_html = "<span>共 2 条</span><span>每页 1 条</span>"
            stop_first = threading.Event()

            def first_parse(_html):
                stop_first.set()
                return [
                    BitListItem(
                        slug="page1",
                        title="page1",
                        detail_url="https://example/page1",
                        publish_date="2024-01-01",
                    )
                ]

            with patch(
                "crawl_bit_jwjc_regulations._fetch_html",
                side_effect=lambda _url: index_html,
            ), patch(
                "crawl_bit_jwjc_regulations._parse_list_page",
                side_effect=first_parse,
            ):
                backfill_bit_publish_dates(
                    state, rebuild_index=False, stop_event=stop_first
                )
            checkpoint_status = state.get_backfill_statuses()["bit"]
            self.assertEqual(checkpoint_status["next_page"], 2)
            self.assertEqual(checkpoint_status["status"], "interrupted")

            reloaded = CrawlerState(root / "crawler_state.json")
            fetched: list[str] = []

            def fetch(url):
                fetched.append(url)
                return index_html

            second_items = [
                BitListItem(
                    slug="page2",
                    title="page2",
                    detail_url="https://example/page2",
                    publish_date="2025-02-02",
                )
            ]
            with patch(
                "crawl_bit_jwjc_regulations._fetch_html", side_effect=fetch
            ), patch(
                "crawl_bit_jwjc_regulations._parse_list_page",
                return_value=second_items,
            ) as parse_page:
                backfill_bit_publish_dates(reloaded, rebuild_index=False)
            self.assertTrue(any(url.endswith("index1.htm") for url in fetched))
            self.assertEqual(parse_page.call_count, 1)
            docs = reloaded.snapshot()["documents"]
            self.assertEqual(docs["bit:page2"]["publish_date"], "2025-02-02")
            self.assertEqual(
                reloaded.get_backfill_statuses()["bit"]["status"], "done"
            )


if __name__ == "__main__":
    unittest.main()
