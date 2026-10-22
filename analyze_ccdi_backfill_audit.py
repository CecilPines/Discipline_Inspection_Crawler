#!/usr/bin/env python3
"""审计中纪检年份日期补全表中保存的网页编号和本地匹配编号。

本脚本只读取 crawler_state.json，不联网、不修改状态文件。
默认读取项目目录下的 data/regulations/crawler_state.json，也可以通过
命令行参数指定线上的状态文件路径。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "regulations"
    / "crawler_state.json"
)


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _numeric_id_sequence(values: Any) -> list[int]:
    result: list[int] = []
    for value in values or []:
        match = re.fullmatch(r"(?:ccdi:)?(\d+)", str(value or "").strip())
        if match:
            result.append(int(match.group(1)))
    return result


def _numeric_ids(values: Any) -> set[int]:
    return set(_numeric_id_sequence(values))


def analyze_state(data: dict[str, Any]) -> dict[str, Any]:
    backfill = data.get("backfill_status") or {}
    ccdi = backfill.get("ccdi") or {}
    rows = [
        dict(row)
        for row in (ccdi.get("year_rows") or [])
        if isinstance(row, dict)
    ]

    year_expected_count = 0
    page_web_id_count = 0
    page_matched_id_count = 0
    web_years: dict[int, set[str]] = {}
    web_year_occurrences: dict[tuple[str, int], int] = {}
    web_year_pages: dict[tuple[str, int], set[int]] = {}
    matched_ids: set[int] = set()
    unmatched_ids: set[int] = set()
    years_without_page_audit: list[str] = []
    audited_expected_count = 0
    missing_audit_expected_count = 0

    for row in rows:
        year = str(row.get("year") or "")
        expected = _as_int(row.get("year_total_count"))
        if expected is not None:
            year_expected_count += max(0, expected)
        page_results = [
            dict(page)
            for page in (row.get("page_results") or [])
            if isinstance(page, dict)
        ]
        if not page_results:
            years_without_page_audit.append(year)
            if expected is not None:
                missing_audit_expected_count += max(0, expected)
        elif expected is not None:
            audited_expected_count += max(0, expected)

        for page in page_results:
            # 每出现一个编号就计 1；跨页、跨年重复也分别计数。
            web_id_values = _numeric_id_sequence(page.get("web_ids") or [])
            matched_page_values = _numeric_id_sequence(
                page.get("matched_local_ids") or []
            )
            web_ids = set(web_id_values)
            matched_page_ids = set(matched_page_values)
            page_web_id_count += len(web_id_values)
            page_matched_id_count += len(matched_page_values)
            matched_ids.update(matched_page_ids)
            unmatched_ids.update(
                _numeric_ids(page.get("unmatched_ids") or [])
            )
            for web_id in web_ids:
                web_years.setdefault(web_id, set()).add(year)
            page_number = _as_int(page.get("page"))
            if page_number is not None:
                for web_id in web_ids:
                    web_year_pages.setdefault((year, web_id), set()).add(
                        page_number
                    )
            for web_id in web_id_values:
                occurrence_key = (year, web_id)
                web_year_occurrences[occurrence_key] = (
                    web_year_occurrences.get(occurrence_key, 0) + 1
                )
        unmatched_ids.update(
            _numeric_ids(row.get("unmatched_web_ids") or [])
        )

    documents = data.get("documents") or {}
    dated_total = 0
    local_dates: dict[int, str] = {}
    local_ids: set[int] = set()
    document_items = (
        documents.items()
        if isinstance(documents, dict)
        else (
            (str(index), item)
            for index, item in enumerate(documents)
            if isinstance(item, dict)
        )
    )
    for doc_key, meta in document_items:
        if not isinstance(meta, dict) or meta.get("source") != "ccdi":
            continue
        numeric_key = _numeric_ids([doc_key])
        if numeric_key:
            local_id = next(iter(numeric_key))
            local_ids.add(local_id)
            local_dates[local_id] = str(
                meta.get("publish_date") or ""
            ).strip()
        publish_date = str(meta.get("publish_date") or "").strip()
        if publish_date:
            dated_total += 1

    repeated_ids = {
        web_id for web_id, years in web_years.items() if len(years) > 1
    }
    same_year_repeated_ids = {
        web_id
        for (_, web_id), count in web_year_occurrences.items()
        if count > 1
    }
    duplicate_page_groups: dict[tuple[str, tuple[int, ...]], set[int]] = {}
    for (year, web_id), pages in web_year_pages.items():
        if len(pages) <= 1:
            continue
        duplicate_page_groups.setdefault(
            (year, tuple(sorted(pages))), set()
        ).add(web_id)
    same_year_duplicate_details = [
        {
            "year": year,
            "pages": list(pages),
            "web_ids": sorted(web_ids),
        }
        for (year, pages), web_ids in sorted(
            duplicate_page_groups.items(),
            key=lambda item: (
                int(item[0][0]) if item[0][0].isdigit() else -1,
                item[0][1],
            ),
        )
    ]
    duplicate_occurrence_count = sum(
        max(0, count - 1) for count in web_year_occurrences.values()
    )
    matched_dated_ids: set[int] = set()
    matched_missing_date_ids: set[int] = set()
    matched_missing_local_ids: set[int] = set()
    matched_wrong_year_ids: set[int] = set()
    for web_id in matched_ids:
        if web_id not in local_ids:
            matched_missing_local_ids.add(web_id)
            continue
        publish_date = local_dates.get(web_id, "")
        if not publish_date:
            matched_missing_date_ids.add(web_id)
        elif publish_date[:4] not in web_years.get(web_id, set()):
            matched_wrong_year_ids.add(web_id)
        else:
            matched_dated_ids.add(web_id)

    return {
        "year_expected_count": year_expected_count,
        "page_web_id_count": page_web_id_count,
        "page_matched_id_count": page_matched_id_count,
        "local_dated_count": dated_total,
        "unique_web_id_count": len(web_years),
        "years_without_page_audit_count": len(years_without_page_audit),
        "audit_complete": bool(
            year_expected_count > 0 and not years_without_page_audit
        ),
        "audited_expected_count": audited_expected_count,
        "missing_audit_expected_count": missing_audit_expected_count,
        "unique_matched_id_count": len(matched_ids),
        "repeated_web_id_count": len(repeated_ids),
        "same_year_repeated_web_id_count": len(same_year_repeated_ids),
        "duplicate_occurrence_count": duplicate_occurrence_count,
        "same_year_duplicate_details": same_year_duplicate_details,
        "unmatched_web_id_count": len(unmatched_ids),
        "matched_dated_count": len(matched_dated_ids),
        "matched_missing_date_count": len(matched_missing_date_ids),
        "matched_missing_local_count": len(matched_missing_local_ids),
        "matched_wrong_year_count": len(matched_wrong_year_ids),
    }


def print_report(
    report: dict[str, Any], state_path: Path, *, check: bool = False
) -> None:
    print("中纪检日期补全审计")
    print(f"状态文件：{state_path}")
    print(f"各年份应有数量合计：{report['year_expected_count']}")
    print(f"各年份每页网页编号计数合计：{report['page_web_id_count']}")
    print(f"各年份每页匹配编号计数合计：{report['page_matched_id_count']}")
    print(f"本地已有日期的中纪检文件数量：{report['local_dated_count']}")
    if check:
        print()
        print("一致性校验")
        print(
            "逐页编号审计完整性："
            + ("完整" if report["audit_complete"] else "不完整")
        )
        if report["years_without_page_audit_count"]:
            print(
                "缺少逐页编号审计的年份数量："
                f"{report['years_without_page_audit_count']}"
            )
        print(f"有逐页审计年份的应有数量合计：{report['audited_expected_count']}")
        print(
            "缺少逐页审计年份的应有数量合计："
            f"{report['missing_audit_expected_count']}"
        )
        print(f"网页编号去重后数量：{report['unique_web_id_count']}")
        print(
            "跨年份重复网页编号数量："
            f"{report['repeated_web_id_count']}"
        )
        print(
            "同一年份内跨页重复网页编号数量："
            f"{report['same_year_repeated_web_id_count']}"
        )
        print(f"重复出现多计次数：{report['duplicate_occurrence_count']}")
        if report["same_year_duplicate_details"]:
            print("同年跨页重复明细：")
            for group in report["same_year_duplicate_details"]:
                pages = "、".join(f"第{page}页" for page in group["pages"])
                ids = "、".join(str(value) for value in group["web_ids"])
                print(
                    f"  {group['year']} 年 · {pages}："
                    f"{len(group['web_ids'])} 个编号"
                )
                print(f"    重复编号：{ids}")
        print(f"未匹配网页编号数量：{report['unmatched_web_id_count']}")
        print(f"匹配编号去重后数量：{report['unique_matched_id_count']}")
        print(f"匹配编号已有日期数量：{report['matched_dated_count']}")
        print(f"匹配编号缺少日期数量：{report['matched_missing_date_count']}")
        print(f"匹配编号找不到本地文件数量：{report['matched_missing_local_count']}")
        print(f"匹配编号日期年份不符数量：{report['matched_wrong_year_count']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "读取 crawler_state.json，统计中纪检年份表网页编号、"
            "本地匹配编号、历年应有总数和本地已有日期数。"
        )
    )
    parser.add_argument(
        "state_file",
        nargs="?",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"状态文件路径，默认：{DEFAULT_STATE_PATH}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON，而不是文本报告。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="额外输出重复、未匹配和日期落盘一致性计数。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_path = args.state_file.expanduser().resolve()
    if not state_path.is_file():
        print(f"状态文件不存在：{state_path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取状态文件失败：{exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("状态文件顶层不是 JSON 对象", file=sys.stderr)
        return 2

    report = analyze_state(data)
    if args.json:
        payload = report if args.check else {
            key: report[key]
            for key in (
                "year_expected_count",
                "page_web_id_count",
                "page_matched_id_count",
                "local_dated_count",
            )
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_report(report, state_path, check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
