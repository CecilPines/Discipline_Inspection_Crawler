"""法规文件版本识别与索引。

版本族以“来源 + 去掉末尾修改说明后的标题”为键。带修改说明的标题只在
括号内出现“修改/修订 + 年份”时视为同一版本族，避免把普通括号内容误合并。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


_REVISION_SUFFIX_RE = re.compile(
    r"(?P<label>\s*[\(（\[【]\s*(?P<inner>[^\)）\]】]*"
    r"(?:修改|修订|修正)[^\)）\]】]*)\s*[\)）\]】])\s*$"
)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def normalize_title(title: str) -> str:
    """统一全角/半角、空白和大小写，仅用于比较，不用于展示。"""
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = text.replace("【", "[").replace("】", "]")
    return re.sub(r"\s+", " ", text).strip().casefold()


def split_version_title(title: str) -> tuple[str, str, int | None]:
    """返回 ``(基础标题, 修改说明, 修改年份)``。

    只把标题末尾的“（2017年修改）”之类内容作为修改说明。没有修改说明的
    同名文件属于同一版本族，由发布日期区分新旧。
    """
    normalized = unicodedata.normalize("NFKC", str(title or "")).strip()
    match = _REVISION_SUFFIX_RE.search(normalized)
    if not match:
        return normalized, "", None
    base = normalized[: match.start()].strip()
    label = match.group("label").strip()
    year_match = _YEAR_RE.search(match.group("inner"))
    year = int(year_match.group(1)) if year_match else None
    return (base or normalized), label, year


def version_family_key(source: str, title: str) -> str:
    base, _, _ = split_version_title(title)
    return f"{source}::{normalize_title(base)}"


def parse_date_value(*values: Any) -> str:
    """把日期/年份规范成 YYYY-MM-DD 或 YYYY；无法解析时返回空串。"""
    for value in values:
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not text:
            continue
        match = re.search(r"((?:19|20)\d{2})(?:[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?)?", text)
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2) or 0)
        day = int(match.group(3) or 0)
        if month and day:
            try:
                return datetime(year, month, day).date().isoformat()
            except ValueError:
                pass
        if month:
            return f"{year:04d}-{month:02d}"
        return f"{year:04d}"
    return ""


def date_from_filename(value: str) -> str:
    """从旧文件名或标题中兜底提取日期，通常只有年份。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    stem = Path(text).stem if "." in Path(text).name else text
    patterns = (
        r"_(?P<date>(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))"
        r"(?:_\d+)?$",
        r"_(?P<date>(?:19|20)\d{2})(?:_\d+)?$",
        r"[\(\[【]\s*(?P<date>(?:19|20)\d{2})\s*年"
        r"(?:修订|修改|修正)?\s*[\)\]】]",
        r"(?P<date>(?:19|20)\d{2})\s*年(?:修订|修改|修正)",
    )
    for pattern in patterns:
        match = re.search(pattern, stem)
        if not match:
            continue
        raw = match.group("date")
        if len(raw) == 8:
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        return raw
    return ""


def _date_key(value: str) -> str:
    text = parse_date_value(value)
    if len(text) == 4:
        return text + "-01-01"
    if len(text) == 7:
        return text + "-01"
    return text


def version_sort_key(record: dict[str, Any]) -> tuple:
    """版本排序：发布日期年份优先，其次标题，再按完整日期和爬取时间。"""
    title = str(record.get("title") or "")
    _, _, revision_year = split_version_title(title)
    publish_date = parse_date_value(record.get("publish_date"))
    publish_year = int(publish_date[:4]) if len(publish_date) >= 4 else 0
    try:
        crawled_year = int(str(record.get("crawled_at") or "")[:4] or 0)
    except ValueError:
        crawled_year = 0
    year = publish_year or int(revision_year or 0) or crawled_year
    return (
        year,
        normalize_title(title),
        _date_key(record.get("publish_date") or ""),
        str(record.get("crawled_at") or ""),
        str(record.get("link_suffix") or ""),
        str(record.get("version_id") or ""),
    )


def choose_latest_version(records: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [dict(row) for row in records if isinstance(row, dict)]
    if not rows:
        return None
    return max(rows, key=version_sort_key)


def build_version_index(
    records: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """由版本记录生成界面和上传器使用的版本族索引。"""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for version_id, raw in records.items():
        if not isinstance(raw, dict):
            continue
        row = {"version_id": version_id, **raw}
        source = str(row.get("source") or "")
        title = str(row.get("title") or "")
        family_key = str(
            row.get("version_family_key") or version_family_key(source, title)
        )
        row["version_family_key"] = family_key
        grouped[family_key].append(row)

    result: dict[str, dict[str, Any]] = {}
    for family_key, rows in grouped.items():
        rows.sort(key=version_sort_key, reverse=True)
        latest = choose_latest_version(rows)
        latest_id = str(latest.get("version_id")) if latest else ""
        versions = []
        for row in rows:
            versions.append(
                {
                    "version_id": row.get("version_id"),
                    "document_key": row.get("document_key"),
                    "title": row.get("title"),
                    "link_suffix": row.get("link_suffix"),
                    "location": row.get("pdf_path") or row.get("location") or "",
                    "category_path": row.get("category_path") or "",
                    "url": row.get("url") or "",
                    "publish_date": row.get("publish_date") or "",
                    "crawled_at": row.get("crawled_at") or "",
                    "is_latest": row.get("version_id") == latest_id,
                }
            )
        first = rows[0]
        base, _, _ = split_version_title(str(first.get("title") or ""))
        result[family_key] = {
            "version_family_key": family_key,
            "source": first.get("source") or "",
            "title": base,
            "latest_version_id": latest_id,
            "latest_document_key": latest.get("document_key") if latest else "",
            "versions": versions,
        }
    return result


def stable_version_id(
    document_key: str,
    *,
    title: str,
    url: str,
    pdf_path: str,
    publish_date: str,
) -> str:
    """同一本地文件的稳定版本 ID；替换文件后会生成新的 ID。"""
    raw = "\x1f".join([document_key, title, url, pdf_path, publish_date])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"v:{document_key}:{digest}"


def link_suffix_from_key(document_key: str) -> str:
    return str(document_key or "").split(":", 1)[-1]


def date_suffix(value: str) -> str:
    """文件名日期后缀：有完整日期用 _YYYYMMDD，只有年份用 _YYYY。"""
    text = parse_date_value(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        return f"_{digits}"
    if len(digits) == 6:
        return f"_{digits}01"
    if len(digits) == 4:
        return f"_{digits}"
    return ""


def dated_stem(stem: str, publish_date: str, *, crawled_at: str = "") -> str:
    """给新落盘文件追加日期；已有日期后缀不重复追加。"""
    if re.search(r"_(?:\d{4}|\d{6}|\d{8})$", str(stem or "")):
        return str(stem or "")
    suffix = date_suffix(publish_date)
    if not suffix and crawled_at:
        suffix = date_suffix(str(crawled_at)[:10])
    return f"{stem}{suffix}"


def dated_path(path: Path, publish_date: str, *, crawled_at: str = "") -> Path:
    return path.with_name(dated_stem(path.stem, publish_date, crawled_at=crawled_at) + path.suffix)


