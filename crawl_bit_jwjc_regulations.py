"""
北京理工大学纪检监察「法规制度」爬虫。
https://jwjc.bit.edu.cn/fgzd/index.htm
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from crawl_common import (
    PlaywrightPdfExporter,
    ensure_unique_path,
    fit_filename,
    is_browser_closed_error,
    normalize_seconds_range,
    path_exists,
    random_delay,
    sanitize_filename,
    sleep_with_stop,
    wait_while_paused,
)
from crawler_state import CrawlerState
from regulation_versions import parse_date_value

logger = logging.getLogger(__name__)

BIT_LIST_BASE = "https://jwjc.bit.edu.cn/fgzd/"
BIT_INDEX = BIT_LIST_BASE + "index.htm"

SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


@dataclass
class BitListItem:
    slug: str
    title: str
    detail_url: str
    publish_date: str


def _fetch_html(url: str, *, attempts: int = 4) -> str:
    """抓列表页；DNS/连接类错误自动重试（校纪检偶发 Temporary failure in name resolution）。"""
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            resp = requests.get(url, headers=SESSION_HEADERS, timeout=60)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
            logger.warning(
                "校纪检页面读取失败（第 %s/%s 次）：%s", attempt, attempts, exc
            )
            if attempt < attempts:
                time.sleep((5, 15, 30)[min(attempt - 1, 2)])
    raise RuntimeError(f"校纪检页面读取失败（已重试 {attempts} 次）：{last_error}")


def _list_page_urls(index_html: str | None = None) -> list[str]:
    """按「共 N 条 / 每页 M 条」生成全部列表页地址。"""
    html = index_html if index_html is not None else _fetch_html(BIT_INDEX)
    total_m = re.search(r"共\s*(\d+)\s*条", html)
    per_m = re.search(r"每页.*?(\d+)\s*条", html, re.S)
    urls = [BIT_INDEX]
    if total_m and per_m:
        total = int(total_m.group(1))
        per_page = max(1, int(per_m.group(1)))
        page_count = max(1, math.ceil(total / per_page))
        for i in range(1, page_count):
            urls.append(urljoin(BIT_LIST_BASE, f"index{i}.htm"))
        return urls

    soup = BeautifulSoup(html, "lxml")
    found = {BIT_INDEX}
    for a in soup.select(".gp-page1 a[href]"):
        href = (a.get("href") or "").strip()
        if href and not href.startswith("javascript"):
            found.add(urljoin(BIT_LIST_BASE, href))
    return sorted(found)


def _make_slug(href: str, detail_url: str) -> str:
    name = href.rsplit("/", 1)[-1]
    if name.endswith(".htm") or name.endswith(".html"):
        return name.rsplit(".", 1)[0]
    digest = hashlib.md5(detail_url.encode("utf-8")).hexdigest()[:16]
    host = urlparse(detail_url).netloc.replace(".", "_")
    return f"{host}_{digest}"


def _parse_list_page(html: str) -> list[BitListItem]:
    """解析一页列表。详情可能是本校 .htm，也可能是中纪委等外站链接，二者都保留。"""
    soup = BeautifulSoup(html, "lxml")
    items: list[BitListItem] = []
    for li in soup.select("ul.block-list57 li"):
        a = li.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        if not href or href.startswith("javascript") or href.startswith("#"):
            continue
        if href.startswith("index") or "/fgzd/index" in href:
            continue
        # 标题取链接文字（导航页 href 后的名称），优先取标题节点，剔除内嵌日期。
        date_el = a.select_one(".gpArticleDate")
        title_el = a.select_one(".gpArticleTitle")
        if title_el is not None:
            title = title_el.get_text(" ", strip=True)
        else:
            title = a.get_text(" ", strip=True)
            if date_el is not None:
                title = title.replace(date_el.get_text(" ", strip=True), "").strip()
        if not title:
            title = urlparse(href).path.rsplit("/", 1)[-1] or _make_slug(
                href, urljoin(BIT_LIST_BASE, href)
            )
        pub = date_el.get_text(strip=True) if date_el else ""
        detail_url = urljoin(BIT_LIST_BASE, href)
        slug = _make_slug(href, detail_url)
        items.append(
            BitListItem(
                slug=slug,
                title=title,
                detail_url=detail_url,
                publish_date=pub,
            )
        )
    return items


def _parse_declared_total(html: str) -> int | None:
    """取列表页上直接显示的「共 N 条」总数。"""
    match = re.search(r"共\s*(\d+)\s*条", html)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def collect_bit_items(index_html: str | None = None) -> tuple[list[BitListItem], int | None]:
    """返回 (全部条目, 页面声明的总数「共 N 条」)。"""
    index_html = index_html if index_html is not None else _fetch_html(BIT_INDEX)
    declared_total = _parse_declared_total(index_html)
    all_items: list[BitListItem] = []
    seen: set[str] = set()
    for page_url in _list_page_urls(index_html):
        html = index_html if page_url == BIT_INDEX else _fetch_html(page_url)
        for item in _parse_list_page(html):
            if item.slug in seen:
                continue
            seen.add(item.slug)
            all_items.append(item)
    logger.info(
        "校纪检列表：页面声明 %s 条，实际解析 %s 条", declared_total, len(all_items)
    )
    return all_items, declared_total


def _bit_filename(seq: int, title: str, publish_date: str = "") -> str:
    """校纪检文件名：`[序号] 标题.pdf`；不再自动追加日期后缀。"""
    del publish_date
    return fit_filename(f"[{seq}] ", sanitize_filename(title))


def backfill_bit_publish_dates(
    state: CrawlerState,
    *,
    rebuild_index: bool = True,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> int:
    """分页补写校纪检发布日期；检查点记录下一页，重启不会从头翻。"""
    before = state.snapshot().get("documents", {})
    bit_rows = [
        meta
        for key, meta in before.items()
        if str(key).startswith("bit:") and isinstance(meta, dict)
    ]
    bit_total = len(bit_rows)
    dated_count = sum(1 for meta in bit_rows if meta.get("publish_date"))
    index_html = _fetch_html(BIT_INDEX)
    declared_total = _parse_declared_total(index_html)
    page_urls = _list_page_urls(index_html)
    page_count = len(page_urls)
    status = ((before and state.snapshot().get("backfill_status", {}).get("bit", {})) or {})
    can_resume = str(status.get("status") or "") in {
        "running",
        "error",
        "interrupted",
    }
    try:
        start_page = (
            int(status.get("next_page") or status.get("page") or 1)
            if can_resume
            and (
                status.get("total_declared") in (None, declared_total)
                or declared_total is None
            )
            else 1
        )
    except (TypeError, ValueError):
        start_page = 1
    start_page = min(max(1, start_page), page_count + 1)
    state.set_backfill_status(
        "bit",
        status="running",
        current=dated_count,
        total=bit_total,
        message=(
            f"校纪检日期补录：从第 {start_page}/{page_count} 页继续"
            if start_page > 1
            else "校纪检日期补录：正在读取目录页…"
        ),
        extra={
            "page": max(1, start_page - 1),
            "next_page": start_page,
            "page_count": page_count,
            "total_declared": declared_total,
        },
    )
    known = state.snapshot().get("documents", {})
    signatures = dict(state.get_source_entry_signatures("bit"))
    update_count = 0
    interrupted = False
    for page_index in range(start_page, page_count + 1):
        if stop_event and stop_event.is_set():
            interrupted = True
            break
        wait_while_paused(pause_event, stop_event)
        html = (
            index_html
            if page_urls[page_index - 1] == BIT_INDEX
            else _fetch_html(page_urls[page_index - 1])
        )
        for item in _parse_list_page(html):
            key = f"bit:{item.slug}"
            signatures[item.slug] = {
                "title": item.title,
                "publish_date": parse_date_value(item.publish_date),
                "url": item.detail_url,
            }
            meta = known.get(key)
            if not isinstance(meta, dict):
                continue
            fields = {}
            if item.publish_date:
                fields["publish_date"] = parse_date_value(item.publish_date)
                fields["date_precision"] = (
                    "day"
                    if len(parse_date_value(item.publish_date)) == 10
                    else "year"
                )
            if not meta.get("title"):
                fields["title"] = item.title
            if fields:
                state.update_document_metadata(
                    key, fields, rebuild_index=rebuild_index
                )
                update_count += 1
        state.set_source_entry_signatures("bit", signatures)
        after = state.snapshot().get("documents", {})
        dated_count = sum(
            1
            for key, meta in after.items()
            if str(key).startswith("bit:")
            and isinstance(meta, dict)
            and meta.get("publish_date")
        )
        state.set_backfill_status(
            "bit",
            status="running",
            current=dated_count,
            total=bit_total,
            message=(
                f"校纪检目录第 {page_index}/{page_count} 页，"
                f"日期已完成 {dated_count}/{bit_total} 条"
            ),
            extra={
                "page": page_index,
                "next_page": min(page_index + 1, page_count + 1),
                "page_count": page_count,
                "total_declared": declared_total,
            },
        )
        state.save_backfill_checkpoint(
            "bit",
            status="running",
            current=dated_count,
            total=bit_total,
            message=f"校纪检目录已核对到第 {page_index}/{page_count} 页",
            page=page_index,
            next_page=min(page_index + 1, page_count + 1),
            page_count=page_count,
            total_declared=declared_total,
        )
    after = state.snapshot().get("documents", {})
    after_rows = [
        meta
        for key, meta in after.items()
        if str(key).startswith("bit:") and isinstance(meta, dict)
    ]
    final_dated = sum(1 for meta in after_rows if meta.get("publish_date"))
    resume_page = int(
        state.snapshot()
        .get("backfill_status", {})
        .get("bit", {})
        .get("next_page", page_count + 1)
    )
    state.set_backfill_status(
        "bit",
        status="interrupted" if interrupted else "done",
        current=final_dated,
        total=len(after_rows),
        message=(
            f"校纪检日期补录已中断，重启后从第 {resume_page} 页继续"
            if interrupted
            else f"校纪检日期补录完成，更新 {update_count} 条"
        ),
        extra={
            "page_count": page_count,
            "next_page": resume_page if interrupted else page_count + 1,
            "total_declared": declared_total,
        },
    )
    if not interrupted:
        state.clear_backfill_checkpoint("bit")
    return update_count


def crawl_bit_jwjc_regulations(
    output_dir: Path,
    state: CrawlerState,
    *,
    headless: bool = True,
    timeout_ms: int = 120_000,
    request_delay_seconds: float | list | tuple = 1.0,
    progress_callback: Callable[[int, int, str], None] | None = None,
    max_items: int | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    pdf_scale: float = 1.0,
    browser_channel: str | None = None,
    stealth: bool = True,
) -> int:
    """爬取校纪检法规页面并导出 PDF，返回本次新增数量。

    request_delay_seconds 可给区间（如 [0.5, 1]），每条之间在该区间内随机取值。
    """
    root = Path(output_dir) / "校纪检"
    root.mkdir(parents=True, exist_ok=True)
    delay_range = normalize_seconds_range(request_delay_seconds, default=(1.0, 1.0))

    state.set_progress("bit", status="running", message="正在检查目录页…", note="")
    # 每日增量：总条数只作提示；始终逐条核对标题与日期，防止同条数替换漏抓。
    index_html = _fetch_html(BIT_INDEX)
    declared_total = _parse_declared_total(index_html)
    stored_total = state.get_source_total("bit")
    if declared_total is not None and stored_total is not None:
        logger.info(
            "校纪检目录条数：%s -> %s；无论条数是否变化，都继续核对标题和日期",
            stored_total,
            declared_total,
        )
    state.set_progress("bit", status="running", message="正在收集列表…", note="")
    catalog_all, declared_total = collect_bit_items(index_html)
    # 站点列表顺序 → 编号（1 起），用于文件名前缀，方便和 index*.htm 上的顺序对照
    seq_by_slug = {item.slug: idx + 1 for idx, item in enumerate(catalog_all)}
    # 运行模式总数优先用页面直接显示的「共 N 条」，取不到时退回实际解析条数
    available = max(declared_total or 0, len(catalog_all))
    base = sum(
        1
        for d in state.snapshot().get("documents", {}).values()
        if d.get("source") == "bit"
    )
    # 每次都要核对标题与日期。总数不变也可能发生“同条数替换”，不能仅凭 total 跳过。
    known = state.snapshot().get("documents", {})
    old_signatures = state.get_source_entry_signatures("bit")
    signatures: dict[str, dict] = {}
    pending_items: list[tuple[BitListItem, bool]] = []
    stable_signatures: dict[str, dict] = {}
    metadata_updates = 0
    changed_count = 0
    for item in catalog_all:
        key = f"bit:{item.slug}"
        existing = known.get(key)
        signature = {
            "title": item.title,
            "publish_date": parse_date_value(item.publish_date),
            "url": item.detail_url,
        }
        signatures[item.slug] = signature
        old_sig = old_signatures.get(item.slug) or {}
        if existing and path_exists(Path(existing.get("pdf_path") or "")):
            old_title = str(existing.get("title") or "")
            old_date = parse_date_value(
                existing.get("publish_date"), existing.get("publish_year")
            )
            if old_sig:
                changed = any(old_sig.get(k) != v for k, v in signature.items())
            else:
                changed = old_title != item.title or bool(
                    old_date
                    and signature["publish_date"]
                    and old_date != signature["publish_date"]
                )
            if changed:
                pending_items.append((item, True))
                changed_count += 1
            else:
                stable_signatures[item.slug] = signature
                updates = {
                    "title": item.title,
                    "url": item.detail_url,
                }
                if signature["publish_date"]:
                    updates["publish_date"] = signature["publish_date"]
                    updates["date_precision"] = (
                        "day" if len(signature["publish_date"]) == 10 else "year"
                    )
                before = (old_title, old_date)
                state.update_document_metadata(key, updates)
                if before != (
                    item.title,
                    parse_date_value(updates.get("publish_date", old_date)),
                ):
                    metadata_updates += 1
        else:
            pending_items.append((item, bool(existing)))
    logger.info(
        "校纪检待爬 %s 条（列表共 %s 条，已登记 %s 条，标题/日期变更 %s 条）",
        len(pending_items),
        len(catalog_all),
        base,
        changed_count,
    )
    confirmed_signatures = {**old_signatures, **stable_signatures}

    if not pending_items:
        logger.info(
            "校纪检无新增或变更：已核对 %s 条标题与发布日期%s",
            len(catalog_all),
            f"，补写日期 {metadata_updates} 条" if metadata_updates else "",
        )
        state.set_progress(
            "bit",
            status="idle",
            current=base,
            total=max(declared_total or 0, base),
            message=(
                f"完成，本次新增 0 个（已逐条核对 {len(catalog_all)} 条标题与日期）"
            ),
        )
        if declared_total is not None:
            state.set_source_total("bit", declared_total)
        state.set_source_entry_signatures("bit", signatures)
        state.mark_pass_done("bit")
        state.set_last_crawl("bit")
        return 0

    if max_items and max_items > 0:
        catalog = pending_items[:max_items]
        done = 0
        total = min(max_items, available)
        if available < max_items:
            state.set_progress(
                "bit", note=f"实际可爬 {available} 条，少于设置的 {max_items} 条"
            )
    else:
        catalog = pending_items
        done = base
        total = max(available, base)
    start_done = done
    state.set_progress("bit", total=total, current=done, message=f"共 {total} 条待处理")

    new_count = 0
    aborted = False
    with PlaywrightPdfExporter(
        headless=headless,
        timeout_ms=timeout_ms,
        pdf_scale=pdf_scale,
        browser_channel=browser_channel,
        stealth=stealth,
    ) as exporter:
        for item, changed in catalog:
            if stop_event and stop_event.is_set():
                logger.info("校纪检爬取被用户终止")
                aborted = True
                break
            wait_while_paused(pause_event, stop_event)
            if stop_event and stop_event.is_set():
                aborted = True
                break

            doc_key = f"bit:{item.slug}"
            msg = f"[{done + 1}/{total}] {item.title}"
            state.set_progress("bit", current=done, message=msg)
            if progress_callback:
                progress_callback(done + 1, total, msg)

            existing = state.snapshot()["documents"].get(doc_key)
            if existing and path_exists(Path(existing["pdf_path"])) and not changed:
                continue

            # 文件名一律用导航页链接文字（哪怕带「...」也原样保留）
            title = item.title

            # 文件名前加站点顺序编号，方便和 index*.htm 上的条目一一对照
            filename = _bit_filename(
                seq_by_slug.get(item.slug, 0), title, item.publish_date
            )
            candidate = root / filename
            record = state.snapshot()["documents"].get(doc_key)
            if candidate.exists() and not (record and Path(record["pdf_path"]) == candidate):
                candidate = ensure_unique_path(candidate)
            pdf_path = candidate

            try:
                exporter.save_url_as_pdf(
                    item.detail_url,
                    pdf_path,
                    wait_until="domcontentloaded",
                    wait_after_load=1.0,
                )
            except Exception as exc:
                if is_browser_closed_error(exc):
                    logger.error("校纪检导出阶段浏览器连接断开，终止本次爬取: %s", exc)
                    aborted = True
                    break
                logger.exception("导出失败 %s", item.detail_url)
                state.set_progress("bit", message=f"失败 {item.detail_url}: {exc}")
                continue

            existing_now = state.snapshot()["documents"].get(doc_key)
            if existing_now:
                state.update_document_version(
                    doc_key,
                    source="bit",
                    title=title,
                    url=item.detail_url,
                    pdf_path=str(pdf_path.resolve()),
                    category_path="校纪检",
                    publish_date=item.publish_date,
                    date_precision=(
                        "day" if parse_date_value(item.publish_date) else ""
                    ),
                    link_suffix=item.slug,
                    count_as_new=changed,
                )
                if changed:
                    new_count += 1
                    done += 1
                    state.set_progress("bit", current=done)
            elif state.register_document(
                doc_key,
                source="bit",
                title=title,
                url=item.detail_url,
                pdf_path=str(pdf_path.resolve()),
                category_path="校纪检",
                publish_date=item.publish_date,
                date_precision=(
                    "day" if parse_date_value(item.publish_date) else ""
                ),
                link_suffix=item.slug,
            ):
                new_count += 1
                done += 1
                state.set_progress("bit", current=done)
            confirmed_signatures[item.slug] = {
                "title": item.title,
                "publish_date": parse_date_value(item.publish_date),
                "url": item.detail_url,
            }

            if delay_range[1] > 0:
                sleep_with_stop(random_delay(delay_range), stop_event, pause_event)

    if aborted:
        state.set_progress("bit", status="idle", message=f"已终止，本次新增 {new_count} 个")
    else:
        state.set_progress("bit", status="idle", message=f"完成，本次新增 {new_count} 个")
        if declared_total is not None and max_items is None:
            state.set_source_total("bit", declared_total)
        # 完整跑完一遍（首次运行据此判断能否收尾）
        state.mark_pass_done("bit")
    state.set_source_entry_signatures("bit", confirmed_signatures)
    state.set_last_crawl("bit")
    return new_count
