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


def _bit_filename(seq: int, title: str) -> str:
    """校纪检文件名：`[序号] 标题.pdf`（序号与站点列表顺序一致）。"""
    return fit_filename(f"[{seq}] ", sanitize_filename(title))


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
    # 每日增量：先看目录页「共 N 条」有没有变化，没变就不抓详情页
    index_html = _fetch_html(BIT_INDEX)
    declared_total = _parse_declared_total(index_html)
    stored_total = state.get_source_total("bit")
    if (
        max_items is None
        and declared_total is not None
        and stored_total is not None
        and declared_total == stored_total
    ):
        logger.info(
            "校纪检无更新：目录页声明 %s 条，与上次记录一致，跳过本轮",
            declared_total,
        )
        # 检查很快，稍微停一下让界面轮询能看到"运行中"再结束
        time.sleep(1.5)
        state.set_progress(
            "bit",
            status="idle",
            message=f"无更新（目录页共 {declared_total} 条，与上次一致）",
        )
        state.set_last_crawl("bit")
        # 目录条数没变 = 这一轮校纪检也是完整核对过的，必须记 pass_done，
        # 否则「首次执行」永远清不掉
        state.mark_pass_done("bit")
        return 0
    if declared_total is not None and stored_total is not None:
        logger.info(
            "校纪检目录条数变化：%s -> %s，开始增量抓取",
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
    # 只保留“还没爬过（或本地 PDF 已丢失）”的条目，保证测试模式每次取到的是新的 N 条
    known = state.snapshot().get("documents", {})
    pending_items = [
        item
        for item in catalog_all
        if not (
            known.get(f"bit:{item.slug}")
            and path_exists(Path(known[f"bit:{item.slug}"]["pdf_path"]))
        )
    ]
    logger.info(
        "校纪检待爬 %s 条（列表共 %s 条，已爬 %s 条）",
        len(pending_items),
        len(catalog_all),
        base,
    )
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
        for item in catalog:
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
            if existing and path_exists(Path(existing["pdf_path"])):
                continue

            # 文件名一律用导航页链接文字（哪怕带「...」也原样保留）
            title = item.title

            # 文件名前加站点顺序编号，方便和 index*.htm 上的条目一一对照
            filename = _bit_filename(seq_by_slug.get(item.slug, 0), title)
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

            if state.register_document(
                doc_key,
                source="bit",
                title=title,
                url=item.detail_url,
                pdf_path=str(pdf_path.resolve()),
                category_path="校纪检",
            ):
                new_count += 1
                done += 1
                state.set_progress("bit", current=done)

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
    state.set_last_crawl("bit")
    return new_count
