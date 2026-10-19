#!/usr/bin/env python3
"""找出「党内法规制度」一级分类里有哪些条目还没进 crawler_state.json。

独立脚本，不依赖项目里的其它模块，也不会修改任何文件（除非加 --json 输出结果）。

用法：
    .venv/bin/python find_missing_ccdi.py
    .venv/bin/python find_missing_ccdi.py --headed          # 遇到滑动验证时用
    .venv/bin/python find_missing_ccdi.py --json missing.json

逻辑：
1. 打开 https://www.ccdi.gov.cn/fgk/law_validity_01，逐页解析列表里的
   `law_display/<编号>` 条目（不打开详情页）；
2. 先按标题到 crawler_state.json 的 documents 里找（source=ccdi）；
3. 标题带「...」、标题没找到、或编号没登记时，再打开详情页取完整标题/编号核对；
4. 输出仍然对不上的条目（编号、列表标题、详情页标题、分类、URL）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://www.ccdi.gov.cn/fgk/law_validity_01"
DEFAULT_STATE = Path(
    "/home/bit/projects/Discipline_Inspection_Crawler/data/regulations/crawler_state.json"
)
FALLBACK_STATE = Path(__file__).resolve().parent / "data" / "regulations" / "crawler_state.json"
DISPLAY_PREFIX = "https://www.ccdi.gov.cn/fgk/law_display/"

# 与主爬虫保持一致：真实 UA + 隐藏 webdriver，否则翻页会被滑到验证页
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
STEALTH_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "window.chrome = window.chrome || {runtime: {}};"
    "Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});"
    "Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});"
)
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


def norm(text: str) -> str:
    """标题归一化：全角转半角、去掉所有空白。"""
    text = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", "", text)


def is_truncated(text: str) -> bool:
    return "..." in text or "…" in text or "。。。" in text


def load_state(path: Path) -> tuple[set[str], set[int], int]:
    """返回 (标题集合, 编号集合, ccdi 文档总数)。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    docs = raw.get("documents") or {}
    titles: set[str] = set()
    ids: set[int] = set()
    total = 0
    for key, meta in docs.items():
        if meta.get("source") != "ccdi":
            continue
        total += 1
        titles.add(norm(meta.get("title", "")))
        match = re.fullmatch(r"ccdi:(\d+)", str(key))
        if match:
            ids.add(int(match.group(1)))
    return titles, ids, total


def extract_title(form) -> str:
    """取导航页链接文字（href 后面的名称），拿不到再退回 title 属性。"""
    container = form.find_parent("li") or form.parent
    if container is not None:
        for a in container.find_all("a"):
            text = a.get_text(" ", strip=True)
            if text:
                return text
    for a in form.find_all("a"):
        title_attr = (a.get("title") or "").strip()
        if title_attr:
            return title_attr
    return ""


def parse_items(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []
    seen: set[str] = set()
    for form in soup.find_all("form", id=True):
        fid = form.get("id", "")
        action = form.get("action") or ""
        if not fid.startswith("searchFrom_") or "law_display" not in action:
            continue
        doc_id = action.rsplit("/", 1)[-1].strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        items.append(
            {
                "doc_id": doc_id,
                "title": extract_title(form),
                "url": urljoin("https://www.ccdi.gov.cn/fgk/", action),
            }
        )
    return items


def parse_max_page(html: str) -> int:
    pages = [int(m.group(1)) for m in re.finditer(r"submitForm_1\((\d+)\s*,", html)]
    return max(pages) if pages else 1


def open_page(pw, headed: bool = False):
    """启动浏览器并返回 page（带反自动化参数，翻页才不会被滑到验证页）。"""
    browser = pw.chromium.launch(headless=not headed, args=list(LAUNCH_ARGS))
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="zh-CN",
        viewport={"width": 1440, "height": 900},
    )
    ctx.add_init_script(STEALTH_SCRIPT)
    return browser, ctx.new_page()


def safe_content(page, tries: int = 8) -> str:
    for _ in range(tries):
        try:
            return page.content()
        except Exception:
            time.sleep(1.0)
    return ""


def goto_page(page, page_num: int) -> None:
    if page_num <= 1:
        return
    cstart = ((page_num - 1) // 7) * 7 + 1
    page.evaluate(
        """([p, cstart]) => {
            if (typeof submitForm_1 === 'function') {
                submitForm_1(p, cstart);
                return true;
            }
            return false;
        }""",
        [page_num, cstart],
    )
    # 翻页后页面还会再跳一次，等到真的解析出条目为止
    for _ in range(10):
        time.sleep(1.0)
        html = safe_content(page)
        if html and parse_items(html):
            return
    time.sleep(1.0)


def read_detail(page, url: str, attempts: int = 2) -> tuple[str, str, bool]:
    """打开详情页，返回 (完整标题, 分类, 是否成功读到页面)。站点偶发慢加载，重试几次。"""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("div[align=left]", timeout=10_000)
            except Exception:
                pass
            time.sleep(0.8)
            soup = BeautifulSoup(page.content(), "lxml")
            h2 = soup.select_one("div.flater_tab h2.tit") or soup.select_one("h2.tit")
            title = h2.get_text(" ", strip=True) if h2 else ""
            category = ""
            for div in soup.select("div[align=left]"):
                match = re.search(r"分类[：:]\s*(.+)", div.get_text(" ", strip=True))
                if match:
                    category = match.group(1).strip()
                    break
            if not category:
                # 兜底：从整页文本里抓「分类：xxx-->yyy」
                match = re.search(
                    r"分类[：:]\s*(\S*(?:\s*-->\s*\S*)?)",
                    soup.get_text(" ", strip=True),
                )
                if match:
                    category = re.sub(r"\s+", "", match.group(1))
            if title:
                return title, category, True
        except Exception as exc:  # noqa: BLE001
            if attempt >= attempts:
                print(f"    [警告] 详情页打开失败（{attempts} 次）：{url}：{exc}")
        time.sleep(2 * attempt)
    return "", "", False


def main() -> int:
    parser = argparse.ArgumentParser(description="找出中纪委某分类里漏爬的条目")
    parser.add_argument("--url", default=DEFAULT_URL, help="分类页地址")
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help=f"crawler_state.json（默认 {DEFAULT_STATE}，不存在则用项目内的）",
    )
    parser.add_argument("--headed", action="store_true", help="有头模式（遇到滑动验证时用）")
    parser.add_argument("--max-pages", type=int, default=0, help="只扫前 N 页（调试用）")
    parser.add_argument(
        "--limit-details",
        type=int,
        default=0,
        help="最多打开 N 个详情页核对（调试用，0=不限）",
    )
    parser.add_argument("--json", type=Path, default=None, help="把漏爬结果写成 JSON")
    args = parser.parse_args()

    state_path = args.state or (DEFAULT_STATE if DEFAULT_STATE.exists() else FALLBACK_STATE)
    if not state_path.exists():
        print(f"找不到状态文件：{state_path}")
        return 2
    titles, ids, ccdi_total = load_state(state_path)
    print(f"状态文件：{state_path}")
    print(f"其中 ccdi 文档：{ccdi_total} 条\n")

    matched_by_title = 0
    matched_by_id = 0
    checked_detail = 0
    missing: list[dict] = []
    seen_items: dict[str, dict] = {}

    with sync_playwright() as pw:
        browser, page = open_page(pw, args.headed)
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(1.5)
        html = safe_content(page)
        body_head = page.inner_text("body")[:500]
        if "滑动拼图" in body_head or "完成验证" in body_head:
            print("分类页触发了滑动验证，请加 --headed 手动完成后再运行。")
            browser.close()
            return 3
        if not parse_items(html):
            print(
                f"分类页没有解析到条目（可能被限流/维护）：{page.url}\n"
                f"页面开头：{body_head[:120].replace(chr(10), ' ')}\n"
                "请过几分钟再试；如果一直这样，加 --headed 看看是不是验证页。"
            )
            browser.close()
            return 4
        pages = parse_max_page(html)
        if args.max_pages:
            pages = min(pages, args.max_pages)
        print(f"分类页：{args.url}")
        print(f"共 {pages} 页，开始逐页解析…\n")

        empty_streak = 0
        for page_num in range(1, pages + 1):
            if page_num > 1:
                goto_page(page, page_num)
                html = safe_content(page)
            items = parse_items(html)
            if items:
                empty_streak = 0
                print(f"  第 {page_num}/{pages} 页：{len(items)} 条")
            else:
                empty_streak += 1
                print(
                    f"  第 {page_num}/{pages} 页：0 条"
                    "（可能被限流/验证，这一页及后续结果不可靠）"
                )
                if empty_streak >= 3:
                    print("  连续 3 页为空，停止翻页；请稍后重试或加 --headed。")
                    break
            for item in items:
                key = f"{item['title']}|{item['doc_id']}"
                if key in seen_items:
                    continue
                seen_items[key] = item

        browser.close()

    print(f"\n站点条目合计：{len(seen_items)} 条（去重后）\n")

    # 先用无头浏览器批量核对标题；必要时才打开详情页
    with sync_playwright() as pw:
        browser, page = open_page(pw, args.headed)
        for idx, item in enumerate(seen_items.values(), start=1):
            title = item["title"]
            nid = int(item["doc_id"]) if item["doc_id"].isdigit() else None
            hit_title = bool(title) and norm(title) in titles
            if hit_title:
                matched_by_title += 1
                continue
            # 标题没命中：编号已登记也算爬过（标题可能只是排版差异）
            if nid is not None and nid in ids:
                matched_by_id += 1
                continue
            # 标题带省略号、或编号/标题都没登记 → 打开详情页核对
            detail_title, category, loaded = ("", "", False)
            skipped = False
            try:
                if args.limit_details and checked_detail >= args.limit_details:
                    skipped = True
                else:
                    checked_detail += 1
                    detail_title, category, loaded = read_detail(page, item["url"])
            except Exception as exc:  # noqa: BLE001
                print(f"  [警告] 打开详情页失败 {item['url']}：{exc}")
            if detail_title and norm(detail_title) in titles:
                matched_by_title += 1
                continue
            if nid is not None and nid in ids:
                matched_by_id += 1
                continue
            missing.append(
                {
                    "doc_id": item["doc_id"],
                    "list_title": title,
                    "detail_title": detail_title,
                    "category": category,
                    "url": item["url"],
                    "reason": (
                        "state 里没有对应标题/编号"
                        if loaded
                        else (
                            "未核对（--limit-details）"
                            if skipped
                            else "详情页打不开，未能核对"
                        )
                    ),
                }
            )
            print(
                f"  [缺失] 编号 {item['doc_id']}：{detail_title or title}"
                f"（分类：{category or '未取到'}；"
                f"{'未核对' if skipped else ('详情页未打开，待复核' if not loaded else '已核对')}）"
            )
        browser.close()

    print("\n统计：")
    print(f"  站点条目：{len(seen_items)}")
    print(f"  按标题命中：{matched_by_title}")
    print(f"  按编号命中：{matched_by_id}")
    print(f"  打开详情页核对：{checked_detail}")
    print(f"  仍然缺失：{len(missing)}")
    if missing:
        print("\n漏爬清单：")
        for item in missing:
            print(
                f"  - 编号 {item['doc_id']}｜{item['detail_title'] or item['list_title']}"
                f"｜分类：{item['category'] or '（空/未分类）'}"
                f"｜{item['reason']}\n    {item['url']}"
            )
        if args.json:
            args.json.write_text(
                json.dumps(missing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n结果已写入：{args.json}")
    else:
        print("\n没有发现漏爬的条目。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
