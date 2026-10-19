"""
中央纪委国家监委「党纪法规库」爬虫。
https://www.ccdi.gov.cn/fgk/index

抓取方式按站点真实结构来：首页固定，左侧导航在各页一致，点击导航进入
类目子页，子页里的法规链接才是目标页面。因此采用「点击导航 → 进入类目 →
逐页就地抓取并导出 PDF」的流式方式，不再预先收集完整目录（站点在类目页
可能触发滑动拼图人机验证，无法在验证通过前拿到列表）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import Page

from crawl_common import (
    BrowserConnectionClosed,
    DEFAULT_THROTTLE_WAIT_RANGES,
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

CCDI_BASE = "https://www.ccdi.gov.cn/fgk/"
CCDI_INDEX = CCDI_BASE + "index"
CCDI_ADVANCED_SEARCH = CCDI_BASE + "law_advancedsearch"
CCDI_DISPLAY_URL = CCDI_BASE + "law_display/{nid}"

# (一级分类, 二级分类, 左侧导航点击码)
CCDI_CATEGORY_PATHS: list[tuple[str, str, str]] = [
    ("党内法规制度", "党章", "law_validity_0101"),
    ("党内法规制度", "准则", "law_validity_0102"),
    ("党内法规制度", "条例", "law_validity_0103"),
    ("党内法规制度", "规则", "law_validity_0104"),
    ("党内法规制度", "规定", "law_validity_0105"),
    ("党内法规制度", "办法", "law_validity_0106"),
    ("党内法规制度", "细则", "law_validity_0107"),
    ("党内法规制度", "规范性文件", "law_validity_0108"),
    ("党内法规制度", "解释答复", "law_validity_0109"),
    ("国家法律法规", "宪法", "law_validity_0209"),
    ("国家法律法规", "法律", "law_validity_0206"),
    ("国家法律法规", "法律规范性文件", "law_validity_0201"),
    ("国家法律法规", "行政法规", "law_validity_0202"),
    ("国家法律法规", "部委规章", "law_validity_0207"),
    ("国家法律法规", "司法解释", "law_validity_0204"),
    ("国家法律法规", "国际公约", "law_validity_0210"),
]

SKIP_FORM_IDS = {
    "searchFrom0",
    "searchFrom_1",
    "searchFrom_2",
    "searchFrom_3",
    "searchFrom_00",
}
LIST_ITEM_SELECTOR = 'form[action*="law_display"]'
CATEGORY_ITEM_SELECTOR = f'li.fixed, {LIST_ITEM_SELECTOR}'
VERIFICATION_MARKERS = ("滑动拼图", "完成验证", "请完成验证")

VERIFICATION_ALERT_HEADLESS = (
    "中纪检类目页触发人机验证（滑动拼图），无头模式无法通过。"
    "请把 crawl.playwright_headless 设为 false 后再次运行，"
    "在弹出的浏览器窗口中手动完成验证。"
)
VERIFICATION_ALERT_TIMEOUT = (
    "中纪检类目页的人机验证等待超时。"
    "请在有头模式下（crawl.playwright_headless: false）重新运行并手动完成滑动验证。"
)
VERIFICATION_ALERT_KEY = "ccdi_verification"

# 未取到列表 / 缺少导航 / 落到错误页时：等待若干秒 → 回首页 → 重新进入子页
RETRY_DELAY_SECONDS = 2.0
CATEGORY_ATTEMPTS = 3
# 站点把请求拦到别的页面时的特征：
#   /error/error.html  —— "您的请求过于频繁"（真限流，需要长时间退避）
#   weihu.ccdi.gov.cn  —— 维护/拦截页（约 4KB、没有列表）；实测翻页时偶发，
#                          紧接着访问别的分类又正常，所以只做短重试、不做长退避
ERROR_PAGE_MARKER = "/error/"
MAINTENANCE_HOST = "weihu.ccdi.gov.cn"
# 单次尝试里等待列表出现的上限（配合重试：失败会回首页再试，所以不必太长）
LIST_WAIT_SECONDS = 15
# 某个具体页码取不到条目时：回类目页重新翻到该页再试的次数
PAGE_RETRY_ATTEMPTS = 3
# 落到维护页（weihu）后：先等 3 秒重试同一页
MAINTENANCE_RETRY_DELAYS = (3.0,)
# 实测：站点拦截（error.html / weihu）约 6 分钟解除。
# 维护页短重试都失败后，按这个区间等一次再试同一页（与"限流"分开，不走限流退避轮次）
MAINTENANCE_COOLDOWN_SECONDS = (360.0, 420.0)

# 各子分类条数的基线（daily 维护时对比用）
CATEGORY_TOTALS_FILE = "category_totals.json"
# 站点限流页（/error/error.html）上的提示语；命中即按「请求过于频繁」处理
THROTTLE_MARKERS = ("请求过于频繁", "访问过于频繁", "访问频繁")
# 默认限流等待区间（秒），按轮次取用：第 1 次 10~15 分钟、第 2 次 2~3 分钟、第 3 次 1~2 分钟。
# 实际取值由 crawl.throttle_wait_ranges（分钟）决定。
DEFAULT_THROTTLE_WAIT = DEFAULT_THROTTLE_WAIT_RANGES
# 连续限流等待次数上限（默认 3）：每次成功后计数归零，
# 所以「每 20~30 条限流一次」时每次都能重新拥有 3 次退避机会。
DEFAULT_THROTTLE_ROUNDS = 3
# 单轮爬取里浏览器意外关闭时最多重启几次（超过就结束本轮，重启程序可继续）
BROWSER_RELAUNCH_LIMIT = 5
THROTTLE_ALERT_KEY = "ccdi_throttle"
THROTTLE_ALERT_TEXT = (
    "中纪检站点持续限流（「您的请求过于频繁」）：连续多次退避等待后仍被限流，"
    "已暂停本轮爬取并登记未完成的分类，稍后会自动重试。"
    "可调大 crawl.request_delay_min_seconds / _max_seconds，或减少并发（crawl.parallel_sources: false）。"
)

# 连续限流等待计数（中途一旦成功即归零）
_throttle_lock = threading.Lock()
_throttle_round = 0
# 本次退避的「到点重试时间」（epoch 秒）；中途被终止时保留，重启后只补剩余时间
_throttle_resume_at: float | None = None


def _normalize_wait_ranges(value) -> list[tuple[float, float]]:
    """把传入的等待区间归一化成 [(秒下限, 秒上限), ...]；为空/非法时用默认逐轮区间。"""
    result: list[tuple[float, float]] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            pair = normalize_seconds_range(item, default=(-1.0, -1.0))
            if pair[0] >= 0:
                result.append(pair)
    return result or list(DEFAULT_THROTTLE_WAIT_RANGES)


def _reset_throttle_budget() -> None:
    """每轮爬取开始时重置连续限流计数。"""
    global _throttle_round, _throttle_resume_at
    with _throttle_lock:
        _throttle_round = 0
        _throttle_resume_at = None


def _restore_throttle_state(state: CrawlerState | None) -> None:
    """跨进程恢复限流退避：没到点的继续等剩余时间，过点的接着用下一次的短区间。"""
    global _throttle_round, _throttle_resume_at
    _reset_throttle_budget()
    if state is None:
        return
    rec = state.get_throttle_state("ccdi")
    if not rec:
        return
    # 记录太旧（比如隔了一天才重启）就直接丢掉，按新一轮处理
    try:
        updated = datetime.fromisoformat(str(rec.get("updated_at") or ""))
        if (datetime.now() - updated).total_seconds() > 12 * 3600:
            return
    except (TypeError, ValueError):
        return
    round_no = max(1, int(rec.get("round_no") or 1))
    with _throttle_lock:
        _throttle_round = round_no
    resume_raw = str(rec.get("resume_at") or "")
    try:
        deadline = datetime.strptime(resume_raw, "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return
    if deadline <= time.time() + 1:
        logger.info(
            "上次的限流等待已过点：继续第 %s 轮的退避预算（不会重新从第 1 轮等起）",
            round_no,
        )
        return
    with _throttle_lock:
        _throttle_resume_at = deadline
    logger.info(
        "检测到上次未完成的限流等待：只补剩余时间，到 %s 再重试（不再重新随机）",
        resume_raw,
    )


def _throttle_recovered(state: CrawlerState | None = None) -> None:
    """成功拿到列表 / 成功导出，说明限流已解除，连续计数归零。"""
    global _throttle_round, _throttle_resume_at
    with _throttle_lock:
        _throttle_round = 0
        _throttle_resume_at = None
    if state is not None:
        state.clear_throttle_state("ccdi")


def _throttle_backoff(
    state: CrawlerState | None,
    stop_event: threading.Event | None,
    pause_event: threading.Event | None,
    wait_ranges: list[tuple[float, float]],
    max_rounds: int,
    what: str,
    visit_extra: dict | None = None,
) -> str:
    """遇到限流页后的退避等待。

    wait_ranges 按轮次取用（第 1/2/3 次各不同，超出条数沿用最后一条）。
    若上一次等待被中断且还没到点，则只补剩余时间（不重新随机、不占新轮次）。
    返回 "retry"（已等待，可以立即重试）/ "exhausted"（连续等待次数用尽）/ "stopped"。
    """
    global _throttle_round, _throttle_resume_at
    resumed = False
    wait_range: tuple[float, float] | None = None
    with _throttle_lock:
        now = time.time()
        if _throttle_resume_at and now < _throttle_resume_at - 1:
            # 上次的等待还没到期：接着等，不重新抽区间
            resumed = True
            round_no = max(1, _throttle_round)
            wait = _throttle_resume_at - now
            retry_stamp = datetime.fromtimestamp(_throttle_resume_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            if max_rounds > 0 and _throttle_round >= max_rounds:
                # 放弃本轮：清掉记录，下次（含定时重试）重新拥有完整预算
                _throttle_resume_at = None
                if state is not None:
                    state.clear_throttle_state("ccdi")
                return "exhausted"
            _throttle_round += 1
            round_no = _throttle_round
            index = min(round_no - 1, len(wait_ranges) - 1)
            wait_range = wait_ranges[index]
            wait = random_delay(wait_range)
            _throttle_resume_at = now + wait
            retry_stamp = datetime.fromtimestamp(_throttle_resume_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
    if resumed:
        logger.warning(
            "限流等待续算：还剩 %.1f 分钟，到 %s 再重试（不再重新随机，%s）",
            wait / 60.0,
            retry_stamp,
            what,
        )
    else:
        logger.warning(
            "站点限流（请求过于频繁）：第 %s/%s 次退避（本轮区间 %.0f~%.0f 分钟），"
            "暂停 %.1f 分钟，预计 %s 重试（%s）",
            round_no,
            max_rounds or "∞",
            (wait_range or (0, 0))[0] / 60.0,
            (wait_range or (0, 0))[1] / 60.0,
            wait / 60.0,
            retry_stamp,
            what,
        )
    if state is not None:
        state.set_throttle_state(
            "ccdi", round_no=round_no, resume_at=retry_stamp
        )
    if state is not None:
        state.set_progress(
            "ccdi",
            message=(
                f"继续上次未完成的限流等待：还剩 {wait / 60:.1f} 分钟"
                f"（第 {round_no}/{max_rounds or '∞'} 次，原定 {retry_stamp} 重试）…"
                if resumed
                else f"站点限流（请求过于频繁），暂停 {wait / 60:.1f} 分钟后重试"
                f"（第 {round_no}/{max_rounds or '∞'} 次，预计 {retry_stamp}）…"
            ),
            extra=visit_extra or {},
        )
    finished = sleep_with_stop(wait, stop_event, pause_event)
    if finished:
        # 等到点了：清掉到点标记（轮次保留，成功后会整体归零）
        with _throttle_lock:
            _throttle_resume_at = None
        if state is not None:
            state.set_throttle_state("ccdi", round_no=round_no, resume_at="")
    return "retry" if finished else "stopped"

# 跳过的子分类原因（界面展示用）
SKIP_REASON_TEXT = {
    "error_page": "站点限流/错误页",
    "maintenance": "站点维护页",
    "empty": "未取到列表",
    "nav_error": "页面打开失败",
    "page_nav_error": "翻页中断",
    "verification": "人机验证",
    "verification_timeout": "人机验证超时",
}


def _skipped_note(skipped: list[dict[str, str]]) -> str:
    """进度卡片上展示的“已跳过子分类”提示文案。"""
    if not skipped:
        return ""
    names = "、".join(item["category"] for item in skipped[:3])
    if len(skipped) > 3:
        names += "…"
    return f"已跳过 {len(skipped)} 个子分类：{names}"


def _visit_extras(exporter: PlaywrightPdfExporter, *, pending_url: str = "") -> dict:
    """带上「当前页面 / 上一个页面」的网址与状态码，供界面实时显示。

    pending_url 用于「正准备访问、还没拿到响应」的时刻：把界面上显示的网址换成
    即将访问的目标（状态码留空显示为“请求中”），避免卡片一直停在上一页
    （例如启动时估算总量用到的 /fgk/law_pagenumb）。
    """
    last_url = getattr(exporter, "last_url", "")
    last_status = getattr(exporter, "last_status", 0)
    extra = {
        "last_url": last_url,
        "last_status": last_status,
        "prev_url": getattr(exporter, "prev_url", ""),
        "prev_status": getattr(exporter, "prev_status", 0),
        "pending": False,
    }
    if pending_url:
        if last_url:
            extra["prev_url"], extra["prev_status"] = last_url, last_status
        extra["last_url"] = pending_url
        extra["last_status"] = 0
        extra["pending"] = True
    return extra


def _is_error_page(page: Page) -> bool:
    return ERROR_PAGE_MARKER in (page.url or "")


def _is_maintenance_page(page: Page) -> bool:
    url = (page.url or "").lower()
    return MAINTENANCE_HOST in url


def _blocked_label(page: Page) -> str:
    """给「被站点拦下」的页面一个可读的名字，方便日志定位。"""
    if _is_maintenance_page(page):
        return "站点维护页（网站正在维护中）"
    if _is_error_page(page):
        return "站点限流页（请求过于频繁）"
    return "站点异常页"


def _is_throttle_page(page: Page) -> bool:
    """是否为站点限流页（正文提示「您的请求过于频繁，请稍后再试」）。"""
    try:
        text = page.inner_text("body")[:1000]
    except Exception:
        return False
    return any(marker in text for marker in THROTTLE_MARKERS)


def _safe_goto(
    page: Page,
    url: str,
    timeout_ms: int | None = None,
    wait_until: str = "domcontentloaded",
) -> bool:
    """容错导航：网络类错误（如 net::ERR_CONNECTION_RESET）只记日志并返回 False。"""
    try:
        if timeout_ms:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        else:
            page.goto(url, wait_until=wait_until)
        return True
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserConnectionClosed(str(exc)) from exc
        logger.warning("打开页面失败 %s：%s", url, exc)
        return False


def _has_nav(page: Page) -> bool:
    """当前页面是否有左侧导航（异常页没有；导航在跳转中时可能抛错，按“没有”处理）。"""
    try:
        return bool(page.query_selector('a[onclick*="law_validity_"]'))
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserConnectionClosed(str(exc)) from exc
        logger.warning("检查左侧导航失败：%s", exc)
        return False


@dataclass
class CcdiListItem:
    doc_id: str
    title: str
    detail_url: str


def _parse_max_page(html: str) -> int:
    """取分页函数 submitForm_1(页码, cstart) 中的最大页码。"""
    pages = [int(m.group(1)) for m in re.finditer(r"submitForm_1\((\d+)\s*,", html)]
    return max(pages) if pages else 1


def _extract_item_title(form) -> str:
    """从列表项中提取链接文字（导航页链接后的名称），而非详情页 <title>。"""
    container = form.find_parent("li") or form.parent
    for a in container.find_all("a"):
        text = a.get_text(" ", strip=True)
        if text:
            return text
    for a in container.find_all("a"):
        title_attr = (a.get("title") or "").strip()
        if title_attr:
            return title_attr
    return ""


def _parse_list_items(html: str) -> list[CcdiListItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[CcdiListItem] = []
    seen: set[str] = set()

    for form in soup.find_all("form", id=True):
        fid = form.get("id", "")
        if fid in SKIP_FORM_IDS or not fid.startswith("searchFrom_"):
            continue
        action = form.get("action") or ""
        if "law_display" not in action:
            continue
        doc_id = action.rsplit("/", 1)[-1].strip()
        if not doc_id or doc_id in seen:
            continue
        title = _extract_item_title(form) or doc_id
        detail_url = urljoin(CCDI_BASE, action)
        seen.add(doc_id)
        items.append(CcdiListItem(doc_id=doc_id, title=title, detail_url=detail_url))
    return items


def _is_verification_page(page: Page) -> bool:
    try:
        text = page.inner_text("body")[:400]
    except Exception:
        return False
    return any(marker in text for marker in VERIFICATION_MARKERS)


def _has_category_items(page: Page) -> bool:
    try:
        return bool(page.query_selector(CATEGORY_ITEM_SELECTOR))
    except Exception:
        return False


def _wait_category_list(
    page: Page,
    stop_event: threading.Event | None,
    verification_wait_seconds: int,
    headless: bool,
) -> tuple[bool, str]:
    """等待类目列表出现；遇到人机验证时（有头模式）等待人工完成。

    返回 (是否成功, 原因)，原因取值：ok / stopped / verification / verification_timeout / error_page / empty。
    """
    start = time.time()
    verify_deadline = start + max(0, verification_wait_seconds)
    warned = False
    while True:
        if stop_event and stop_event.is_set():
            return False, "stopped"
        try:
            if _has_category_items(page):
                return True, "ok"
            if _is_error_page(page) or _is_maintenance_page(page):
                # 站点错误页/维护页：不必等满 15 秒，直接交给上层处理
                logger.warning(
                    "页面落到%s：%s%s",
                    _blocked_label(page),
                    page.url,
                    "（正文提示请求过于频繁）" if _is_throttle_page(page) else "",
                )
                # 只有 /error/error.html 才算"限流"，维护页单独一种原因（不走长退避）
                return False, "error_page" if _is_error_page(page) else "maintenance"
            if _is_verification_page(page):
                if not warned:
                    warned = True
                    if headless:
                        logger.error(VERIFICATION_ALERT_HEADLESS)
                        return False, "verification"
                    logger.warning(
                        "中纪检类目页触发人机验证（滑动拼图），请在浏览器窗口中手动完成，"
                        "程序最多等待 %s 秒…",
                        verification_wait_seconds,
                    )
                if time.time() <= verify_deadline:
                    time.sleep(1.0)
                    continue
                logger.error("等待人工验证超时，跳过该分类")
                return False, "verification_timeout"
        except Exception as exc:
            if is_browser_closed_error(exc):
                raise BrowserConnectionClosed(str(exc)) from exc
            raise
        if time.time() - start > LIST_WAIT_SECONDS:
            return False, "empty"
        time.sleep(0.5)


def _open_category(
    exporter: PlaywrightPdfExporter,
    code: str,
    stop_event: threading.Event | None,
    verification_wait_seconds: int,
    headless: bool,
    *,
    state: CrawlerState | None = None,
    pause_event: threading.Event | None = None,
    throttle_wait_ranges: list | None = None,
    throttle_max_rounds: int = DEFAULT_THROTTLE_ROUNDS,
) -> tuple[bool, str]:
    """通过点击左侧导航进入类目子页。

    未取到列表 / 缺少左侧导航 / 落到站点错误页时：等 RETRY_DELAY_SECONDS 秒 →
    重新进入法规库首页 → 再跳转该子页，最多重试 CATEGORY_ATTEMPTS 次。

    站点限流页（/error/error.html，提示「您的请求过于频繁」）：按 throttle_wait_ranges
    逐轮随机暂停（默认第 1 次 10~15 分钟、第 2 次 2~3 分钟、第 3 次 1~2 分钟）后重试，
    最多连续 throttle_max_rounds 次（退避后的重试不占用普通尝试次数）；次数用尽仍被限流则
    返回 "throttled"，由上层报错并结束本轮。普通失败（未取到列表/页面打不开）仍最多尝试
    CATEGORY_ATTEMPTS 次。

    返回 (是否成功, 原因)，原因取值：ok / stopped / verification /
    verification_timeout / error_page / empty / nav_error / throttled。
    """
    page = exporter.page
    selector = f'a[onclick*="getContent(\'{code}\')"]'
    last_reason = "empty"
    attempt = 0
    rotator = getattr(exporter, "rotate_user_agent", None)
    if callable(rotator):
        rotator(page)  # 每个分类随机换一次 UA

    while True:
        if attempt >= CATEGORY_ATTEMPTS:
            logger.error(
                "分类 %s 连续 %s 次未取到列表（%s），跳过",
                code,
                CATEGORY_ATTEMPTS,
                SKIP_REASON_TEXT.get(last_reason, last_reason),
            )
            return False, last_reason
        attempt += 1
        if stop_event and stop_event.is_set():
            return False, "stopped"

        # 1) 异常页（错误页/验证页）可能没有导航栏：等一下再回首页恢复
        if not _has_nav(page):
            logger.warning(
                "第 %s/%s 次尝试：当前页面没有左侧导航（%s），%s 秒后重新进入首页",
                attempt,
                CATEGORY_ATTEMPTS,
                page.url,
                RETRY_DELAY_SECONDS,
            )
            time.sleep(RETRY_DELAY_SECONDS)
            if not _safe_goto(page, CCDI_INDEX):
                # 连首页都打不开（网络类错误）：等一会儿再来一轮
                last_reason = "nav_error"
                continue
            _wait_category_list(page, stop_event, 0, True)
        if stop_event and stop_event.is_set():
            return False, "stopped"

        # 2) 点击导航进入子页（点击不可用时退回直接访问）
        try:
            has_link = bool(page.query_selector(selector))
        except Exception as exc:
            if is_browser_closed_error(exc):
                raise BrowserConnectionClosed(str(exc)) from exc
            logger.warning("查找导航链接 %s 失败：%s", code, exc)
            has_link = False
        clicked = False
        if has_link:
            try:
                page.click(selector, timeout=8_000)
                clicked = True
            except Exception as exc:
                if is_browser_closed_error(exc):
                    raise BrowserConnectionClosed(str(exc)) from exc
                logger.warning("点击导航 %s 失败，改用直接访问: %s", code, exc)
        else:
            logger.warning("页面缺少 %s 的导航链接，改用直接访问", code)
        if clicked:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            if code not in page.url:
                # 点击未真正跳转到目标类目，退回直接访问
                logger.warning(
                    "点击导航后未跳转到 %s（当前 %s），改用直接访问", code, page.url
                )
                clicked = False
        if not clicked:
            # 直接用 URL 打开子页；网络类错误（如 net::ERR_CONNECTION_RESET）只记日志
            if not _safe_goto(page, urljoin(CCDI_BASE, code)):
                last_reason = "nav_error"
                if attempt < CATEGORY_ATTEMPTS:
                    _recover_index(exporter, stop_event)
                continue

        # 3) 等待列表
        ok, reason = _wait_category_list(
            page, stop_event, verification_wait_seconds, headless
        )
        if ok:
            _throttle_recovered(state)
            return True, "ok"
        last_reason = reason
        if reason in ("verification", "verification_timeout", "stopped"):
            return False, reason

        # 4) 错误页 / 未取到列表：等一会儿，回首页，再跳一次
        try:
            logger.warning(
                "分类 %s 未取到列表（第 %s/%s 次，原因=%s，url=%s，title=%s）",
                code,
                attempt,
                CATEGORY_ATTEMPTS,
                reason,
                page.url,
                (page.title() or "")[:40],
            )
        except Exception:
            pass

        if reason == "error_page":
            # 站点限流：按逐轮区间随机暂停（默认 10~15 / 2~3 / 1~2 分钟）后重试，
            # 最多连续 throttle_max_rounds 次；等完仍限流则报错并结束本轮
            action = _throttle_backoff(
                state,
                stop_event,
                pause_event,
                _normalize_wait_ranges(throttle_wait_ranges),
                throttle_max_rounds,
                code,
                    visit_extra=_visit_extras(exporter),
            )
            if action == "stopped":
                return False, "stopped"
            if action == "exhausted":
                logger.error(
                    "连续 %s 次限流退避（含退避后的重试）后仍被限流，放弃本轮爬取：%s",
                    throttle_max_rounds,
                    code,
                )
                return False, "throttled"
            # 退避后的这一次重试不占用普通尝试次数：下一轮循环重新尝试打开该分类
            attempt -= 1
            continue

        _recover_index(exporter, stop_event)


def _recover_index(
    exporter: PlaywrightPdfExporter, stop_event: threading.Event | None
) -> None:
    """等一会儿重新进入法规库首页，把页面从错误页/半途状态拉回来。"""
    page = exporter.page
    logger.info("%s 秒后重新进入法规库首页并重试该分类", RETRY_DELAY_SECONDS)
    time.sleep(RETRY_DELAY_SECONDS)
    if not _safe_goto(page, CCDI_INDEX):
        return
    _wait_category_list(page, stop_event, 0, True)


def _goto_list_page_number(
    page: Page, page_num: int, stop_event: threading.Event | None = None
) -> bool:
    """翻到指定页码；返回是否成功（失败只记日志，由调用方决定是否中断该分类）。"""
    if page_num <= 1:
        return True
    # 站点每 7 个页码为一组展示，cstart 为该组的起始页码。
    cstart = ((page_num - 1) // 7) * 7 + 1
    try:
        called = page.evaluate(
            """([p, cstart]) => {
                if (typeof submitForm_1 === 'function') {
                    submitForm_1(p, cstart);
                    return true;
                }
                return false;
            }""",
            [page_num, cstart],
        )
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserConnectionClosed(str(exc)) from exc
        logger.warning("翻到第 %s 页失败：%s", page_num, exc)
        return False
    if not called:
        # 当前不在类目列表页（例如刚导出 PDF、页面停在详情页），无法调用分页函数
        logger.warning("当前页面没有分页函数（%s），无法翻到第 %s 页", page.url, page_num)
        return False
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        # 站点是表单提交式翻页，domcontentloaded 之后还会再跳一次，
        # 多等一会儿让页面真正稳定下来，避免随后 page.content() 读到一半
        page.wait_for_load_state("load", timeout=15_000)
    except Exception:
        pass
    time.sleep(1.2)
    return True


def _read_list_html(
    page: Page, stop_event: threading.Event | None = None, tries: int = 3
) -> str | None:
    """读取当前列表页 HTML；页面正在跳转时自动重试（site 翻页后会再跳一次）。"""
    for attempt in range(1, max(1, tries) + 1):
        if stop_event and stop_event.is_set():
            return None
        try:
            return page.content()
        except Exception as exc:
            if is_browser_closed_error(exc):
                raise BrowserConnectionClosed(str(exc)) from exc
            logger.warning(
                "读取列表页内容失败（第 %s/%s 次）：%s", attempt, tries, exc
            )
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            time.sleep(1.5)
    return None


def _load_category_totals(path: Path) -> dict[str, int]:
    """读各子分类条数基线（category_totals.json）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                value = value.get("total")
            try:
                out[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return out


def _save_category_totals(path: Path, totals: dict[str, int]) -> None:
    """写回各子分类条数基线（同时记录时间，方便人工核对）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **{code: {"total": int(total)} for code, total in sorted(totals.items())},
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("子分类条数基线已更新：%s（%s 个分类）", path, len(totals))
    except OSError as exc:
        logger.warning("写入子分类条数基线失败：%s", exc)


def _measure_category_total(
    exporter: PlaywrightPdfExporter,
    code: str,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> int | None:
    """按「(页数-1)×每页条数 + 末页条数」量出某个子分类的条数（与 6444 同口径）。"""
    page = exporter.page
    try:
        if not _safe_goto(page, urljoin(CCDI_BASE, code)):
            return None
        _wait_category_list(page, stop_event, 0, True)
        html = _read_list_html(page, stop_event) or ""
        per_page = len(_parse_list_items(html))
        pages = _parse_max_page(html)
        if per_page <= 0 or pages <= 0:
            return None
        if pages == 1:
            logger.info("子分类 %s 条数：单页 %s 条", code, per_page)
            return per_page
        wait_while_paused(pause_event, stop_event)
        if not _goto_list_page_number(page, pages, stop_event):
            return None
        last = len(_parse_list_items(_read_list_html(page, stop_event) or ""))
        if last <= 0:
            return None
        total = per_page * (pages - 1) + last
        logger.info(
            "子分类 %s 条数：每页 %s × %s 页 + 末页 %s = %s",
            code,
            per_page,
            pages - 1,
            last,
            total,
        )
        return total
    except BrowserConnectionClosed:
        raise
    except Exception as exc:
        logger.warning("测量子分类 %s 条数失败：%s", code, exc)
        return None


def _estimate_all_data_total(
    exporter: PlaywrightPdfExporter,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    attempts: int = 3,
) -> int | None:
    """在「全部数据」页估算总数：每页条数 ×（总页数-1）+ 末页条数。

    页面偶尔还在跳转（content 取不到）时重试几次，避免总数退化成 0 让进度条失去分母。
    """
    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1:
            # 上一次可能已经翻到末页了，先回到第 1 页再重新估算
            if not _safe_goto(exporter.page, CCDI_INDEX):
                return None
            _wait_category_list(exporter.page, stop_event, 0, True)
            logger.info("重新估算中纪检总数（第 %s 次）", attempt)
        total = _estimate_all_data_total_once(exporter, stop_event, pause_event)
        if total is not None:
            return total
        if attempt < attempts:
            time.sleep(2.0)
    return None


def _estimate_all_data_total_once(
    exporter: PlaywrightPdfExporter,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> int | None:
    page = exporter.page
    try:
        if "fgk/index" not in page.url:
            if not _safe_goto(page, CCDI_INDEX):
                return None
            _wait_category_list(page, stop_event, 0, True)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        html = page.content()
        per_page = len(_parse_list_items(html))
        pages = _parse_max_page(html)
        if per_page <= 0 or pages <= 0:
            return None
        if pages == 1:
            return per_page
        if not _goto_list_page_number(page, pages, stop_event):
            return None
        wait_while_paused(pause_event, stop_event)
        last_count = len(_parse_list_items(page.content()))
        if last_count <= 0:
            return None
        total = per_page * (pages - 1) + last_count
        logger.info(
            "中纪检「全部数据」估算：每页 %s 条 × %s 页 + 末页 %s 条 = %s 条",
            per_page,
            pages - 1,
            last_count,
            total,
        )
        return total
    except Exception as exc:
        logger.warning("估算中纪检「全部数据」总数失败: %s", exc)
        return None


def _export_item(
    exporter: PlaywrightPdfExporter,
    state: CrawlerState,
    root: Path,
    main_cat: str,
    sub_cat: str,
    item: CcdiListItem,
    seq: int | None = None,
    detail_page: Page | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    detail_delay: tuple[float, float] | None = None,
) -> str:
    """导出单个法规为 PDF 并登记。

    返回：
    - "new"：新增并已登记；
    - "exists"：此前已爬过且 PDF 完好，本次跳过；
    - "refreshed"：状态里有记录但 PDF 文件丢了，本次重新导出并修正记录（不算新增）；
    - "throttled"：详情页被站点限流，导出的 PDF 已删除且未登记，稍后重试。
    """
    doc_key = f"ccdi:{item.doc_id}"
    # 文件名一律用导航页链接文字（哪怕带「...」也原样保留），不再进详情页取 <title>。
    # 前缀 `子分类[序号]`：序号 = 该条目在子分类列表里的位置（与站点顺序一致）
    title = item.title
    folder = root / main_cat / sub_cat
    base_name = sanitize_filename(title)
    filename = (
        fit_filename(f"{sub_cat}[{seq}] ", base_name)
        if seq
        else fit_filename("", base_name)
    )

    existing = state.snapshot()["documents"].get(doc_key)
    if existing and Path(existing["pdf_path"]).exists():
        # 已存在的文件保持原样（不重命名），编号只对之后新下载的文件生效
        return "exists"
    missing_pdf = bool(existing)

    candidate = folder / filename
    record = state.snapshot()["documents"].get(doc_key)
    # 记录里的路径是 resolve 过的绝对路径，这里统一 resolve 后再比较，
    # 避免“相对路径 != 绝对路径”导致每次都另存成 xxx_2.pdf
    same_as_record = bool(
        record and _same_path(Path(record["pdf_path"]), candidate)
    )
    if candidate.exists() and not same_as_record:
        candidate = ensure_unique_path(candidate)
    pdf_path = candidate

    used_page = (
        exporter.save_url_as_pdf(
            item.detail_url,
            pdf_path,
            wait_until="domcontentloaded",
            wait_after_load=1.5,
            page=detail_page,
        )
        or exporter.page
    )

    # 详情页导出后额外歇 1~2 秒（可配置），别紧接着开下一个页面
    if detail_delay and detail_delay[1] > 0:
        sleep_with_stop(random_delay(detail_delay), stop_event, pause_event)

    # 详情页若落到站点限流/错误页，导出的是提示页：删掉且不登记，下次重爬该条
    try:
        if _is_error_page(used_page) or _is_throttle_page(used_page):
            logger.warning("详情页被站点限流或错误页，丢弃本次导出：%s", item.detail_url)
            try:
                pdf_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("删除限流页 PDF 失败 %s：%s", pdf_path, exc)
            return "throttled"
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserConnectionClosed(str(exc)) from exc
        logger.warning("检查详情页是否被限流失败：%s", exc)

    registered = state.register_document(
        doc_key,
        source="ccdi",
        title=title,
        url=item.detail_url,
        pdf_path=str(pdf_path.resolve()),
        category_path=f"{main_cat}/{sub_cat}",
    )
    if registered:
        return "new"
    if missing_pdf:
        # 记录在但 PDF 丢了：把路径修正回磁盘上的实际文件，避免每轮重复导出
        state.mark_pdf_path(doc_key, str(pdf_path.resolve()))
        return "refreshed"
    return "exists"


def _same_path(left: Path, right: Path) -> bool:
    """比较两个路径是否指向同一个文件（忽略相对/绝对与大小写差异）。"""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def _cursor_is_fresh(cursor: dict, max_age_hours: float) -> bool:
    """中断位置是否还在可续爬的时间窗内（避免隔太久还从中间开始，漏掉前面的新文件）。"""
    try:
        updated = datetime.fromisoformat(str(cursor.get("updated_at") or ""))
    except (TypeError, ValueError):
        return False
    age_hours = (datetime.now() - updated).total_seconds() / 3600.0
    return 0 <= age_hours <= max_age_hours


def crawl_ccdi_regulations(
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
    verification_wait_seconds: int = 300,
    pdf_scale: float = 1.0,
    browser_channel: str | None = None,
    stealth: bool = True,
    categories: list[str] | None = None,
    throttle_wait_ranges: list | None = None,
    throttle_max_rounds: int = DEFAULT_THROTTLE_ROUNDS,
    resume_max_age_hours: float = 12.0,
    user_agents: list[str] | None = None,
    detail_page_delay_seconds: float | list | tuple = (1.0, 2.0),
    first_run: bool = False,
) -> int:
    """
    点击左侧导航逐类目爬取中纪检法规并保存为 PDF，返回本次新增数量。
    不预先收集完整目录，按类目流式抓取。

    categories 给出时只爬取这些导航码（用于定时重试被跳过的子分类）。
    request_delay_seconds 可给区间（如 [1, 3]），每条之间在该区间内随机取值。
    throttle_wait_ranges 为限流退避逐轮的随机等待区间（秒），默认第 1 次 10~15 分钟、
    第 2 次 2~3 分钟、第 3 次 1~2 分钟；throttle_max_rounds 为连续退避次数上限
    （默认 3，成功后计数归零）。
    resume_max_age_hours > 0 时，若上一轮是中途被中断的（且中断时间在该小时数内），
    本次从上次断掉的那个分类/页码接着爬，不再从第一个分类重新扫描；0 = 总是全量扫描。
    user_agents 给定时，每进入一个分类 / 导出每条详情页都随机换一个 User-Agent。
    detail_page_delay_seconds 为「每导出一条详情页后」额外停顿的随机区间（默认 1~2 秒）。
    first_run=True 时（首次运行/首次运行中断后继续）：跳过已经标记完成的子分类，
    并把每个子分类的条数写进 output_dir/category_totals.json 作为日常对比基线。
    """
    root = Path(output_dir) / "中纪检"
    root.mkdir(parents=True, exist_ok=True)

    delay_range = normalize_seconds_range(request_delay_seconds, default=(1.0, 1.0))
    wait_ranges = _normalize_wait_ranges(throttle_wait_ranges)
    detail_delay = normalize_seconds_range(
        detail_page_delay_seconds, default=(1.0, 2.0)
    )
    # 若上次的限流等待还没到点，这里会接着等，而不是重新随机一个区间
    _restore_throttle_state(state)

    path_list = CCDI_CATEGORY_PATHS
    if categories:
        wanted = set(categories)
        path_list = [c for c in CCDI_CATEGORY_PATHS if c[2] in wanted]
        if not path_list:
            logger.warning("指定的重试分类不存在，跳过：%s", ", ".join(sorted(wanted)))
            return 0
    retrying = bool(categories)

    # 子分类条数基线：首次运行只记录、日常维护用于比对
    totals_path = Path(output_dir) / CATEGORY_TOTALS_FILE
    daily_check = not first_run and not retrying
    baseline = {} if (first_run or retrying) else _load_category_totals(totals_path)
    measured_totals: dict[str, int] = {}

    # 子分类状态：新的一轮全量把还没跑过的分类标为 pending（已跑过的保持上次结果）
    if not retrying:
        current = state.get_category_status("ccdi")
        updates = {}
        for m, s, cd in path_list:
            entry = current.get(cd) or {}
            if first_run and entry.get("status") == "done":
                continue  # 首次运行续跑：已完成的分类保持完成，不再重置
            updates[cd] = {"main": m, "sub": s, "status": "pending", "reason": ""}
        if updates:
            state.update_category_statuses("ccdi", updates)

    # 中断后续爬：读上次记录的位置（仅非重试模式、且上次没跑完、且中断时间够近）
    resume_code = ""
    resume_page = 1
    if not retrying:
        cursor = state.get_crawl_cursor("ccdi")
        if resume_max_age_hours <= 0:
            logger.info("resume_max_age_hours=0：本轮从第一个分类开始全量扫描")
        elif not cursor:
            logger.info("没有续爬记录：本轮从第一个分类开始全量扫描")
        elif cursor.get("finished"):
            logger.info("上一轮已完整跑完：本轮从第一个分类开始全量扫描")
        elif not _cursor_is_fresh(cursor, resume_max_age_hours):
            logger.info(
                "续爬记录已过期（记录时间 %s，超出 %s 小时）：本轮从第一个分类开始全量扫描",
                cursor.get("updated_at"),
                resume_max_age_hours,
            )
        else:
            wanted_code = str(cursor.get("code") or "")
            if any(cd == wanted_code for _, _, cd in path_list):
                resume_code = wanted_code
                resume_page = max(1, int(cursor.get("page") or 1))
                logger.info(
                    "检测到上次中断的位置，从 %s 第 %s 页接着爬",
                    wanted_code,
                    resume_page,
                )
            else:
                logger.info(
                    "续爬记录里的分类 %s 不在本次爬取范围：本轮从头扫描",
                    wanted_code,
                )

    # 续爬时，断点「之前」被跳过的分类不会被走到：把它们插到本轮最前面补上，
    # 这样重启一次既能续爬、也能把跳过·翻页中断的分类补完。
    if resume_code:
        cursor_index = next(
            i for i, (_, _, cd) in enumerate(path_list) if cd == resume_code
        )
        pending_codes = {
            str(s.get("code") or "")
            for s in state.get_skipped_categories("ccdi", retryable_only=True)
        }
        head = [c for c in path_list[:cursor_index] if c[2] in pending_codes]
        # 注意：无论有没有要补的分类，都要把断点之前的分类裁掉，
        # 否则本轮会从第一个分类重新扫（曾经因此从头跑了一遍）
        path_list = head + path_list[cursor_index:]
        if head:
            logger.info(
                "续爬前先补上之前被跳过的 %s 个分类：%s",
                len(head),
                "、".join(f"{m}/{s}" for m, s, _ in head),
            )

    opening_msg = "正在打开法规库…"
    if resume_code:
        _m, _s, _ = next(c for c in path_list if c[2] == resume_code)
        if resume_page > 1:
            opening_msg = f"从中断处继续：{_m}/{_s} 第 {resume_page} 页…"
        else:
            opening_msg = f"从中断处继续：{_m}/{_s}…"
    state.set_progress(
        "ccdi", status="running", current=0, total=0, message=opening_msg, note=""
    )
    new_count = 0
    done = 0
    total = 0
    aborted = False
    verify_blocked = False
    throttle_blocked = False
    skipped: list[dict[str, str]] = []
    started_at = time.time()
    cat_index = -1
    # 本轮扫描序号：只统计「本次运行真正走过的不重复条目」，
    # 重试/重读同一页不会重复累加（按 doc_id 去重）
    scanned = 0
    scanned_ids: set[str] = set()
    # 本轮真正加载过的详情页数量（= 站点承受的页面请求数）
    detail_loads = 0

    with PlaywrightPdfExporter(
        headless=headless,
        timeout_ms=timeout_ms,
        pdf_scale=pdf_scale,
        browser_channel=browser_channel,
        stealth=stealth,
        user_agents=user_agents,
    ) as exporter:
        page = exporter.page
        # 详情页用独立标签页导出：主页面始终停在类目列表页，翻页函数才不会失效
        try:
            detail_page = exporter.new_page()
        except Exception:
            detail_page = None
        if not _safe_goto(page, CCDI_INDEX):
            logger.error("中纪检法规库首页打不开，本次爬取结束")
            state.set_progress(
                "ccdi",
                status="idle",
                message="法规库首页打不开（网络类错误），本次未爬取",
            )
            state.set_last_crawl("ccdi")
            return 0
        time.sleep(1.5)

        # 已登记数量作为累计起点；可用总数按「全部数据」页估算
        base = sum(
            1
            for d in state.snapshot().get("documents", {}).values()
            if d.get("source") == "ccdi"
        )
        available = (
            None if retrying else _estimate_all_data_total(exporter, stop_event, pause_event)
        )
        if max_items and max_items > 0:
            done = 0
            total = min(max_items, available) if available else max_items
            if available and available < max_items:
                state.set_progress(
                    "ccdi", note=f"实际可爬 {available} 条，少于设置的 {max_items} 条"
                )
        elif retrying:
            # 重试模式：只回补被跳过的子分类，进度按“已处理条数”展示
            done = base
            total = 0
        else:
            done = base
            total = max(available, base) if available else 0
        start_done = done
        if retrying:
            names = "、".join(f"{m}/{s}" for m, s, _ in path_list)
            state.set_progress(
                "ccdi",
                current=done,
                total=total,
                message=f"重试跳过的子分类：{names}",
            )
        else:
            state.set_progress(
                "ccdi", current=done, total=total, message=f"共 {total} 条待处理"
            )
        logger.info("中纪检进度：起点 %s，总数 %s（可用估算 %s）", done, total, available)

        for cat_index, (main_cat, sub_cat, code) in enumerate(path_list):
            if stop_event and stop_event.is_set():
                aborted = True
                break
            wait_while_paused(pause_event, stop_event)
            if stop_event and stop_event.is_set():
                aborted = True
                break
            if max_items and max_items > 0 and (done - start_done) >= max_items:
                break

            stored = state.get_category_status("ccdi").get(code) or {}
            if first_run and stored.get("status") == "done":
                # 首次运行续跑：此前已完整的分类不再重新扫描/爬取
                logger.info(
                    "首次运行续跑：%s/%s 此前已完成，跳过（不重新扫描）",
                    main_cat,
                    sub_cat,
                )
                continue
            if daily_check:
                measured = _measure_category_total(
                    exporter, code, stop_event, pause_event
                )
                if measured is not None:
                    measured_totals[code] = measured
                    base_total = baseline.get(code)
                    if base_total is not None and measured <= base_total:
                        logger.info(
                            "分类 %s/%s 数量未变（%s 条，基线 %s），跳过",
                            main_cat,
                            sub_cat,
                            measured,
                            base_total,
                        )
                        state.set_category_status(
                            "ccdi",
                            code,
                            status="done",
                            total=measured,
                            total_estimated=False,
                            reason="",
                            page=1,
                        )
                        continue
                    if base_total is not None:
                        logger.info(
                            "分类 %s/%s 数量增加：%s → %s，需要重爬",
                            main_cat,
                            sub_cat,
                            base_total,
                            measured,
                        )

            msg = f"进入分类 {main_cat}/{sub_cat}"
            logger.info(msg)
            if not retrying:
                # 进入分类前先记位置：这一步之后随时可能被 Ctrl+C / 限流打断
                state.set_crawl_cursor(
                    "ccdi", code=code, page=1, scanned=scanned
                )
            state.set_category_status("ccdi", code, status="running")
            state.set_progress(
                "ccdi",
                message=msg,
                extra={
                    "category": f"{main_cat}/{sub_cat}",
                    "category_code": code,
                    **_visit_extras(exporter),
                },
            )
            try:
                opened, reason = _open_category(
                    exporter,
                    code,
                    stop_event,
                    verification_wait_seconds,
                    headless,
                    state=state,
                    pause_event=pause_event,
                    throttle_wait_ranges=wait_ranges,
                    throttle_max_rounds=throttle_max_rounds,
                )
            except BrowserConnectionClosed:
                logger.warning("中纪检浏览器连接断开，重启浏览器后继续")
                exporter.relaunch()
                page = exporter.page
                try:
                    detail_page = exporter.new_page()
                except Exception:
                    detail_page = None
                _safe_goto(page, CCDI_INDEX)
                time.sleep(1.0)
                continue

            if opened:
                state.clear_alert(VERIFICATION_ALERT_KEY)
            elif reason == "stopped":
                aborted = True
                break
            elif reason == "verification":
                state.set_alert(
                    VERIFICATION_ALERT_KEY, VERIFICATION_ALERT_HEADLESS, level="error"
                )
                verify_blocked = True
                break
            elif reason == "verification_timeout":
                state.set_alert(
                    VERIFICATION_ALERT_KEY, VERIFICATION_ALERT_TIMEOUT, level="error"
                )
                verify_blocked = True
                break
            elif reason == "throttled":
                # 连续退避后仍被限流：结束本轮（报错与登记在循环外统一处理）
                throttle_blocked = True
                logger.error(
                    "站点持续限流，本轮在 %s/%s 处停止",
                    main_cat,
                    sub_cat,
                )
                break
            else:
                logger.warning(
                    "分类 %s/%s 未取到列表（%s），跳过并等待重试",
                    main_cat,
                    sub_cat,
                    SKIP_REASON_TEXT.get(reason, reason),
                )
                skipped.append(
                    {
                        "category": f"{main_cat}/{sub_cat}",
                        "main_cat": main_cat,
                        "sub_cat": sub_cat,
                        "code": code,
                        "reason": reason,
                    }
                )
                # 立即落盘：万一下一秒被 Ctrl+C，这批被跳过的分类也不会丢
                state.set_skipped_categories("ccdi", skipped, retryable=True)
                state.set_category_status("ccdi", code, status="skipped", reason=reason)
                state.set_progress("ccdi", note=_skipped_note(skipped))
                # 回首页重置状态，避免异常页影响下一个分类
                if _safe_goto(exporter.page, CCDI_INDEX):
                    time.sleep(1.0)
                continue

            page_num = 1
            seen: set[str] = set()
            page_break = False
            cat_scanned = 0
            per_page_hint = 0
            start_page = 1
            if code == resume_code and resume_page > 1:
                start_page = resume_page
            elif retrying:
                # 这个分类上次是「翻页中断」被跳过的：直接从断掉的那一页续，不用从第 1 页重扫
                entry = state.get_category_status("ccdi").get(code) or {}
                recorded_page = int(entry.get("page") or 1)
                if entry.get("reason") == "page_nav_error" and recorded_page > 1:
                    start_page = recorded_page
            if start_page > 1:
                # 接着断掉的那一页继续（该页已爬过的条目会被自动跳过）
                if _goto_list_page_number(page, start_page, stop_event):
                    page_num = start_page
                    logger.info(
                        "从第 %s 页继续：%s/%s（跳过前面的页面，不再重复扫描）",
                        start_page,
                        main_cat,
                        sub_cat,
                    )
            # 本轮是从第几页开始扫这个分类的（续爬时会 >1，此时扫到的条数不等于总数）
            cat_start_page = page_num
            while True:
                if stop_event and stop_event.is_set():
                    aborted = True
                    break
                wait_while_paused(pause_event, stop_event)
                if stop_event and stop_event.is_set():
                    aborted = True
                    break
                if not retrying:
                    # 每翻一页记一次位置：中途被 Ctrl+C 打断也能从这里接着爬
                    state.set_crawl_cursor(
                        "ccdi", code=code, page=page_num, scanned=scanned
                    )
                # 读取当前页；取不到内容/解析不出条目时，回类目页重新翻到这一页再试
                html = ""
                items: list[CcdiListItem] = []
                fail_note = ""
                for page_try in range(1, PAGE_RETRY_ATTEMPTS + 1):
                    if page_try > 1:
                        logger.warning(
                            "分类 %s/%s 第 %s 页没有条目（第 %s/%s 次，%s；当前页面 %s），"
                            "直接访问类目页 %s 并重新翻到第 %s 页",
                            main_cat,
                            sub_cat,
                            page_num,
                            page_try,
                            PAGE_RETRY_ATTEMPTS,
                            fail_note or "原因未知",
                            page.url,
                            urljoin(CCDI_BASE, code),
                            page_num,
                        )
                        if not _safe_goto(page, urljoin(CCDI_BASE, code)):
                            time.sleep(RETRY_DELAY_SECONDS)
                            continue
                        _wait_category_list(page, stop_event, 0, True)
                        if page_num > 1 and not _goto_list_page_number(
                            page, page_num, stop_event
                        ):
                            time.sleep(RETRY_DELAY_SECONDS)
                            continue
                    elif page_num > 1 and code not in (page.url or "") and "law_pagenumb" not in (page.url or ""):
                        # 主页面可能被带到详情页了：翻页前先确认还在类目列表上
                        logger.warning(
                            "翻页前发现页面不在类目列表（%s），先直接访问类目页 %s",
                            page.url,
                            urljoin(CCDI_BASE, code),
                        )
                        if _safe_goto(page, urljoin(CCDI_BASE, code)):
                            _wait_category_list(page, stop_event, 0, True)
                            _goto_list_page_number(page, page_num, stop_event)
                    html = _read_list_html(page, stop_event) or ""
                    if not html:
                        fail_note = "页面内容读取失败（可能仍在跳转）"
                        items = []
                    else:
                        items = _parse_list_items(html)
                        if not items:
                            if _is_maintenance_page(page):
                                # 维护/拦截页：实测紧接着访问别的分类就恢复正常，
                                # 说明不是长时限流——短等后重新进类目页再翻一次
                                fail_note = (
                                    f"翻到第 {page_num} 页后落到{_blocked_label(page)}"
                                    f"（{page.url}）"
                                )
                                if page_try >= 2:
                                    # 等 3 秒重试后仍是维护页 → 退出本轮重试，
                                    # 交给下面的「6~7 分钟冷却」再试一次
                                    break
                                sleep_with_stop(
                                    MAINTENANCE_RETRY_DELAYS[0], stop_event, pause_event
                                )
                                if _safe_goto(page, urljoin(CCDI_BASE, code)):
                                    _wait_category_list(page, stop_event, 0, True)
                                    _goto_list_page_number(page, page_num, stop_event)
                                continue
                            if _is_error_page(page) or _is_throttle_page(page):
                                # 翻页的 POST 被站点限流：走退避，而不是当成解析失败
                                fail_note = (
                                    f"翻到第 {page_num} 页后落到{_blocked_label(page)}"
                                    f"（{page.url}）"
                                )
                                action = _throttle_backoff(
                                    state,
                                    stop_event,
                                    pause_event,
                                    wait_ranges,
                                    throttle_max_rounds,
                                    f"{code} 第 {page_num} 页",
                                    visit_extra=_visit_extras(exporter),
                                )
                                if action == "stopped":
                                    aborted = True
                                elif action == "exhausted":
                                    throttle_blocked = True
                                else:
                                    if _safe_goto(page, urljoin(CCDI_BASE, code)):
                                        _wait_category_list(page, stop_event, 0, True)
                                        _goto_list_page_number(
                                            page, page_num, stop_event
                                        )
                                if aborted or throttle_blocked:
                                    break
                                continue
                            fail_note = f"读到 {len(html)} 字节但没有解析出条目"
                    if items:
                        break
                    if stop_event and stop_event.is_set():
                        break
                    time.sleep(1.5)
                if not items and "维护页" in fail_note:
                    # 维护页是页面级拦截（实测约 6 分钟解除）：短重试用尽后
                    # 再等一轮 5~7 分钟试同一页；仍不行才按翻页中断跳过。
                    # 注意：这不是"站点限流"，文案与限流退避严格区分。
                    cool = random_delay(MAINTENANCE_COOLDOWN_SECONDS)
                    cool_stamp = (datetime.now() + timedelta(seconds=cool)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    logger.warning(
                        "分类 %s/%s 第 %s 页连续落到维护页，暂停 %.1f 分钟后再试同一页"
                        "（预计 %s 重试，页面级拦截，不是限流）：%s",
                        main_cat,
                        sub_cat,
                        page_num,
                        cool / 60.0,
                        cool_stamp,
                        page.url,
                    )
                    state.set_progress(
                        "ccdi",
                        message=(
                            f"遇到站点维护/拦截页，暂停 {cool / 60:.1f} 分钟后重试第 {page_num} 页"
                            f"（预计 {cool_stamp}）…"
                        ),
                        extra=_visit_extras(exporter),
                    )
                    if not sleep_with_stop(cool, stop_event, pause_event):
                        aborted = True
                        break
                    if _safe_goto(page, urljoin(CCDI_BASE, code)):
                        _wait_category_list(page, stop_event, 0, True)
                        _goto_list_page_number(page, page_num, stop_event)
                        html = _read_list_html(page, stop_event) or ""
                        items = _parse_list_items(html) if html else []
                        if items:
                            fail_note = ""
                    if not items:
                        logger.warning(
                            "分类 %s/%s 第 %s 页等待后仍取不到条目，按翻页异常处理",
                            main_cat,
                            sub_cat,
                            page_num,
                        )
                if aborted or throttle_blocked:
                    break
                if stop_event and stop_event.is_set():
                    aborted = True
                    break
                logger.info("分类 %s/%s 第 %s 页共 %s 条", main_cat, sub_cat, page_num, len(items))
                if not items:
                    # 多次重新进入/翻页仍取不到条目：按异常处理，跳过该分类稍后重试，
                    # 不能当成"已经到最后一页"，否则会被误判成已爬完
                    logger.warning(
                        "分类 %s/%s 第 %s 页重试 %s 次仍没有条目（%s；最后停在 %s），"
                        "按翻页异常处理（稍后重试）",
                        main_cat,
                        sub_cat,
                        page_num,
                        PAGE_RETRY_ATTEMPTS,
                        fail_note or "原因未知",
                        page.url,
                    )
                    page_break = True
                    break
                # 每读完一页刷新一次「最近访问的网址/状态码」，界面上能实时看到
                state.set_progress(
                    "ccdi",
                    extra={
                        **_visit_extras(exporter),
                    },
                )
                if page_num == 1 and cat_scanned == 0:
                    # 用第 1 页「每页条数 × 总页数」估个总数（跑完该分类后会用精确值覆盖）
                    prev = state.get_category_status("ccdi").get(code) or {}
                    if prev.get("total_estimated", True) or not prev.get("total"):
                        state.set_category_status(
                            "ccdi",
                            code,
                            total=len(items) * max(1, _parse_max_page(html)),
                            total_estimated=True,
                        )
                # 分类内的站点顺序号：第 N 页第 i 条 = 前面各页条数 + i + 1
                if per_page_hint <= 0:
                    prev_entry = state.get_category_status("ccdi").get(code) or {}
                    pages_all = max(1, _parse_max_page(html))
                    est_total = prev_entry.get("total")
                    if prev_entry.get("total_estimated") and est_total and pages_all > 1:
                        # 估算总数 = 每页条数 × 页数，可反推每页条数（续爬时也能对上号）
                        per_page_hint = max(1, int(est_total) // pages_all)
                    else:
                        per_page_hint = len(items)
                if cat_start_page <= 1:
                    page_base = cat_scanned  # 从第 1 页开始：已扫过的条数就是前面的条数
                else:
                    page_base = (page_num - 1) * per_page_hint

                for item_index, item in enumerate(items):
                    if max_items and max_items > 0 and (done - start_done) >= max_items:
                        break
                    if stop_event and stop_event.is_set():
                        aborted = True
                        break
                    wait_while_paused(pause_event, stop_event)
                    if item.doc_id in seen:
                        continue
                    seen.add(item.doc_id)
                    if item.doc_id not in scanned_ids:
                        scanned_ids.add(item.doc_id)
                        scanned += 1
                    cat_scanned += 1
                    while True:
                        # 分母优先用「全部数据」页估算的库总量（测试模式下 total 只是
                        # 本次要爬的条数，拿它当扫描分母会显得超过 100%）
                        scan_total = available or total
                        if scan_total and scan_total < scanned:
                            # 站点估算偏小时，避免出现「扫描 6700/6444」这种反直觉显示
                            scan_total = scanned
                        progress_msg = (
                            f"[扫描 {scanned}/{scan_total} · 已爬 {done}] {item.title}"
                            if scan_total
                            else f"[扫描 {scanned} · 已爬 {done}] {item.title}"
                        )
                        state.set_progress(
                            "ccdi",
                            current=done,
                            message=progress_msg,
                            extra={
                                "scanned": scanned,
                                "new_count": new_count,
                                "loaded": detail_loads,
                                **_visit_extras(exporter),
                            },
                            # 位置与进度一起落盘：Ctrl+C 打断也能精确接着爬
                            cursor=(
                                None
                                if retrying
                                else {"code": code, "page": page_num, "scanned": scanned}
                            ),
                        )
                        if progress_callback:
                            progress_callback(done + 1, total, progress_msg)
                        try:
                            status = _export_item(
                                exporter,
                                state,
                                root,
                                main_cat,
                                sub_cat,
                                item,
                                seq=page_base + item_index + 1,
                                detail_page=detail_page,
                                stop_event=stop_event,
                                pause_event=pause_event,
                                detail_delay=detail_delay,
                            )
                        except Exception as exc:
                            if is_browser_closed_error(exc):
                                logger.error(
                                    "中纪检导出阶段浏览器连接断开，终止本次爬取: %s", exc
                                )
                                aborted = True
                                break
                            logger.exception("导出失败 %s", item.detail_url)
                            state.set_progress(
                                "ccdi", message=f"失败 {item.detail_url}: {exc}"
                            )
                            break
                        if status != "throttled":
                            break
                        # 详情页被限流：按逐轮区间退避后重试本条，最多连续 throttle_max_rounds 次
                        action = _throttle_backoff(
                            state,
                            stop_event,
                            pause_event,
                            wait_ranges,
                            throttle_max_rounds,
                            item.detail_url,
                            visit_extra=_visit_extras(exporter),
                        )
                        if action == "retry":
                            continue
                        if action == "stopped":
                            aborted = True
                            break
                        throttle_blocked = True
                        logger.error(
                            "连续 %s 次限流退避后仍被限流，放弃本轮爬取（%s）",
                            throttle_max_rounds,
                            item.detail_url,
                        )
                        break
                    if aborted or throttle_blocked:
                        break
                    if status != "exists":
                        # 这条真的打开过详情页（用来解释"为什么会被限流"：
                        # 翻页每页只 1 次请求，但每导出 1 个 PDF 就要加载 1 个详情页）
                        detail_loads += 1
                    if status == "new":
                        new_count += 1
                        done += 1
                        state.set_progress("ccdi", current=done)
                    elif status == "refreshed":
                        # 记录在但 PDF 丢了，本次重写成功：进度照样往前推
                        done += 1
                        state.set_progress("ccdi", current=done)
                    if delay_range[1] > 0:
                        sleep_with_stop(random_delay(delay_range), stop_event, pause_event)

                if (
                    aborted
                    or throttle_blocked
                    or (max_items and max_items > 0 and (done - start_done) >= max_items)
                ):
                    break
                max_page = _parse_max_page(html)
                if page_num >= max_page:
                    break
                page_num += 1
                try:
                    turned = _goto_list_page_number(page, page_num, stop_event)
                except BrowserConnectionClosed:
                    raise
                if not turned:
                    # 兜底：页面若不在类目列表页（比如被详情页带走了），回类目页再试一次
                    logger.info("翻页失败，回到类目页重试：%s 第 %s 页", code, page_num)
                    if _safe_goto(page, urljoin(CCDI_BASE, code)):
                        _wait_category_list(page, stop_event, 0, True)
                        try:
                            turned = _goto_list_page_number(page, page_num, stop_event)
                        except BrowserConnectionClosed:
                            raise
                if not turned:
                    logger.warning(
                        "分类 %s/%s 翻到第 %s 页失败，本分类本次中断（下次会继续）",
                        main_cat,
                        sub_cat,
                        page_num,
                    )
                    page_break = True
                    break

            if aborted:
                state.set_category_status("ccdi", code, status="interrupted")
                break
            if throttle_blocked:
                state.set_category_status(
                    "ccdi", code, status="skipped", reason="error_page"
                )
                break
            if page_break:
                skipped.append(
                    {
                        "category": f"{main_cat}/{sub_cat}",
                        "main_cat": main_cat,
                        "sub_cat": sub_cat,
                        "code": code,
                        "reason": "page_nav_error",
                    }
                )
                state.set_skipped_categories("ccdi", skipped, retryable=True)
                state.set_category_status(
                    "ccdi",
                    code,
                    status="skipped",
                    reason="page_nav_error",
                    page=page_num,  # 记下断在哪一页，重试时直接从那页开始
                )
                state.set_progress("ccdi", note=_skipped_note(skipped))
            else:
                if cat_start_page <= 1:
                    # 从第 1 页整轮扫完：实际扫到的条数就是精确总数
                    measured_totals[code] = max(cat_scanned, 0)
                    state.set_category_status(
                        "ccdi",
                        code,
                        status="done",
                        total=max(cat_scanned, 0),
                        total_estimated=False,
                        reason="",
                        page=1,
                    )
                else:
                    # 续爬只补了后半段，不能拿这几页的条数当总数（会显示成 10/10）
                    state.set_category_status(
                        "ccdi", code, status="done", reason="", page=1
                    )
                logger.info(
                    "分类 %s/%s 完成：本轮从第 %s 页起扫描 %s 条",
                    main_cat,
                    sub_cat,
                    cat_start_page,
                    cat_scanned,
                )

        # 站点持续限流：报错横幅 + 把本轮未完成的分类（含当前）登记为待重试，
        # 之后由「跳过分类定时重试」在配置的间隔后继续
        if throttle_blocked:
            state.set_alert(THROTTLE_ALERT_KEY, THROTTLE_ALERT_TEXT, level="error")
            known = {s["code"] for s in skipped}
            for m, s, cd in path_list[cat_index:]:
                if cd in known:
                    continue
                skipped.append(
                    {
                        "category": f"{m}/{s}",
                        "main_cat": m,
                        "sub_cat": s,
                        "code": cd,
                        "reason": "error_page",
                    }
                )
                state.set_category_status(
                    "ccdi", cd, status="skipped", reason="error_page"
                )
            state.set_progress("ccdi", note=_skipped_note(skipped))
            logger.error(
                "站点持续限流：本轮已登记 %s 个分类待重试", len(skipped)
            )

    # 登记被跳过的子分类（供界面标识 + 定时重试）；验证拦截时不做自动重试
    state.set_skipped_categories("ccdi", skipped, retryable=not verify_blocked)
    # 完整跑完才标记 finished：下次从头全量扫描（保证新发布的文件不会漏）；
    # 中途被终止/限流/验证中断则保留位置，下次接着爬
    if not (aborted or verify_blocked or throttle_blocked) and not retrying:
        state.set_crawl_cursor("ccdi", finished=True, scanned=0)
        # 完整跑完（且没有分类被跳过）才算这一轮"全量遍历完成"
        if not skipped:
            state.mark_pass_done("ccdi")
    # 量到的条数随时写回基线（首次运行也用它作为日常对比的起点）
    if not retrying and measured_totals:
        _save_category_totals(totals_path, {**baseline, **measured_totals})
    note = _skipped_note(skipped)
    if note:
        if verify_blocked:
            note += "，本次被人机验证中断"
        else:
            note += "，本次未完成，稍后自动重试"

    elapsed_minutes = (time.time() - started_at) / 60.0
    # 走到第几个分类（续爬时也从实际位置算，不会被误读成“只跑了几类”）
    position = max(0, min(cat_index + 1, len(path_list)))
    if verify_blocked:
        outcome = "被人机验证拦截"
    elif throttle_blocked:
        outcome = "站点持续限流提前结束"
    elif aborted:
        outcome = "被终止"
    else:
        outcome = "正常完成"
    logger.info(
        "中纪检本轮结束（%s）：分类 %s/%s，扫描 %s 条，加载详情页 %s 个，"
        "新增 %s 个，用时 %.1f 分钟",
        outcome,
        position,
        len(path_list),
        scanned,
        detail_loads,
        new_count,
        elapsed_minutes,
    )

    stats = (
        f"分类 {position}/{len(path_list)} · 扫描 {scanned} 条"
        f" · 详情页 {detail_loads} 个 · 用时 {elapsed_minutes:.0f} 分钟"
    )
    if verify_blocked:
        message = f"被人机验证拦截，本次新增 {new_count} 个（请改用有头模式手动验证）"
    elif throttle_blocked:
        message = (
            f"站点持续限流，本轮提前结束，新增 {new_count} 个"
            f"（已登记 {len(skipped)} 个分类待自动重试）"
        )
    elif aborted:
        prefix = "跳过分类重试已终止" if retrying else "已终止"
        where = ""
        if 0 <= cat_index < len(path_list):
            stopped_main, stopped_sub, _ = path_list[cat_index]
            where = f"停在 {stopped_main}/{stopped_sub}，"
        message = f"{prefix}，{where}本次新增 {new_count} 个（{stats}）"
    else:
        # 本轮正常跑完，说明限流已解除：清掉之前的限流告警横幅
        state.clear_alert(THROTTLE_ALERT_KEY)
        prefix = "跳过分类重试完成" if retrying else "完成"
        message = f"{prefix}，本次新增 {new_count} 个（{stats}）"

    state.set_progress(
        "ccdi",
        status="idle",
        message=message,
        note=note,
        # 结束时清掉「当前分类」，避免空闲时还显示最后一个分类
        extra={
            "scanned": scanned,
            "new_count": new_count,
            "loaded": detail_loads,
            "category": "",
            "category_code": "",
        },
    )
    state.set_last_crawl("ccdi")
    return new_count


# ---------------------------------------------------------------------------
# 按详情页编号枚举（law_display/<N>）——不依赖类目翻页，规避翻页触发的拦截
# ---------------------------------------------------------------------------


def _parse_display_page(html: str) -> tuple[str, str, str] | None:
    """解析 law_display 详情页 → (主分类, 子分类, 标题)；无效编号返回 None。

    有效页但分类缺失时的兜底：
    - 只有一级分类（例如「党内法规制度」）→ 子分类记为「其他」；
    - 连一级分类都没有 → 主分类记为「其他」、子分类记为空串（文件名不带前缀）。
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    if "输入有误" in text[:300]:
        return None
    h2 = soup.select_one("div.flater_tab h2.tit") or soup.select_one("h2.tit")
    title = h2.get_text(" ", strip=True) if h2 else ""
    if not title:
        return None
    category = ""
    for div in soup.select("div[align=left]"):
        match = re.search(r"分类[：:]\s*(.+)", div.get_text(" ", strip=True))
        if match:
            category = match.group(1).strip()
            break
    if "-->" in category:
        main_cat, _, sub_cat = category.partition("-->")
    else:
        main_cat, sub_cat = category, ""
    main_cat, sub_cat = main_cat.strip(), sub_cat.strip()
    if not main_cat:
        # 连一级分类都没有 → 根目录下的「其他」，文件名 [序号] 标题
        return "其他", "", title
    if not sub_cat:
        # 只有一级分类 → <主分类>/其他，文件名 其他[序号] 标题
        return main_cat, "其他", title
    return main_cat, sub_cat, title


def _is_invalid_display_page(html: str) -> bool:
    """站点对不存在的编号会返回「输入有误请…返回」的提示页。"""
    if not html:
        return False
    try:
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    except Exception:
        return False
    return "输入有误" in text[:300]


def _scan_current_result_pages(
    exporter: PlaywrightPdfExporter,
    known_ids: set[int],
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    max_pages: int = 8,
) -> tuple[list[int], int | None, str]:
    """扫描「当前已打开的结果页」及其后续页，遇到已爬过的编号就停。

    列表是从新到旧排列的，所以一旦遇到一个**已爬过的编号**就可以停：
    剩下的一定更旧、也一定爬过了。

    返回 ``(新编号列表, 本次见到的最小有效编号, 状态)``：
    - 状态 ``"ok"``：翻页正常（新编号列表可能就是空的）；
    - 状态 ``"failed"``：翻页失败或被拦截，结果不完整。
    """
    page = exporter.page
    html = _read_list_html(page, stop_event) or ""
    if not html:
        return [], None, "failed"
    if (
        _is_error_page(page)
        or _is_throttle_page(page)
        or _is_maintenance_page(page)
        or _is_verification_page(page)
    ):
        logger.warning("结果页被拦截/验证（%s），跳过检查", page.url)
        return [], None, "failed"

    pages = max(1, _parse_max_page(html))
    new_ids: list[int] = []
    seen: set[int] = set()
    min_seen: int | None = None
    status = "ok"
    for page_num in range(1, min(pages, max_pages) + 1):
        if stop_event and stop_event.is_set():
            return new_ids, min_seen, "failed"
        wait_while_paused(pause_event, stop_event)
        if page_num > 1:
            if not _goto_list_page_number(page, page_num, stop_event):
                status = "failed"
                break
            html = _read_list_html(page, stop_event) or ""
            if not html:
                status = "failed"
                break
            if _is_error_page(page) or _is_throttle_page(page) or _is_maintenance_page(page):
                logger.warning(
                    "目录第 %s 页被拦截/维护（%s）", page_num, page.url
                )
                status = "failed"
                break
        items = _parse_list_items(html)
        if not items:
            logger.warning("目录第 %s 页没有解析到条目", page_num)
            status = "failed"
            break
        duplicated = False
        for item in items:
            try:
                nid = int(str(item.doc_id).strip())
            except (TypeError, ValueError):
                continue
            min_seen = nid if min_seen is None else min(min_seen, nid)
            if nid in known_ids:
                duplicated = True
                break
            if nid not in seen:
                seen.add(nid)
                new_ids.append(nid)
        logger.info(
            "目录第 %s/%s 页：%s 条，累计新增候选 %s 个%s",
            page_num,
            pages,
            len(items),
            len(new_ids),
            "（遇到已爬过的编号，停止翻页）" if duplicated else "",
        )
        if duplicated:
            break
    return new_ids, min_seen, status


def _scan_index_listing(
    exporter: PlaywrightPdfExporter,
    known_ids: set[int],
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    max_pages: int = 8,
) -> tuple[list[int], int | None, str]:
    """打开「全部数据」目录首页，再按页扫描。"""
    page = exporter.page
    if not _safe_goto(page, CCDI_INDEX):
        return [], None, "failed"
    wait_while_paused(pause_event, stop_event)
    _wait_category_list(page, stop_event, 0, True)
    return _scan_current_result_pages(
        exporter, known_ids, stop_event, pause_event, max_pages
    )


def _search_by_year(
    exporter: PlaywrightPdfExporter,
    year: int | str,
    known_ids: set[int],
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    max_pages: int = 5,
) -> tuple[list[int], int | None, str]:
    """高级检索：把「发布日期」选成本年，搜索后按结果页扫描。"""
    page = exporter.page
    if not _safe_goto(page, CCDI_ADVANCED_SEARCH):
        return [], None, "failed"
    wait_while_paused(pause_event, stop_event)
    try:
        page.wait_for_selector("#fbrq", timeout=20_000)
        has_year = page.evaluate(
            "y => Array.from(document.querySelectorAll('#fbrq option'))"
            ".some(o => (o.value || '').trim() === y)",
            str(year),
        )
        page.select_option("#fbrq", str(year) if has_year else "all")
        logger.info(
            "高级检索：发布日期选 %s，提交搜索…",
            year if has_year else "全部年份（页面没有该年份）",
        )
        # 站点页面引用了并不存在的 advsearch0()，只能直接提交表单
        with page.expect_navigation(timeout=45_000):
            page.evaluate("document.getElementById('searchFrom0').submit()")
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise BrowserConnectionClosed(str(exc)) from exc
        logger.warning("高级检索提交失败：%s", exc)
        return [], None, "failed"
    time.sleep(1.5)
    if _is_verification_page(page):
        logger.warning(
            "高级检索触发了滑动验证（headless 无法自动完成），改用目录页/编号方式"
        )
        return [], None, "captcha"
    if _is_error_page(page) or _is_throttle_page(page) or _is_maintenance_page(page):
        logger.warning("高级检索结果页被拦截（%s）", page.url)
        return [], None, "failed"
    return _scan_current_result_pages(
        exporter, known_ids, stop_event, pause_event, max_pages
    )


def crawl_ccdi_by_id(
    output_dir: Path,
    state: CrawlerState,
    *,
    headless: bool = True,
    timeout_ms: int = 120_000,
    request_delay_seconds: float | list | tuple = (1.0, 2.0),
    detail_page_delay_seconds: float | list | tuple = (1.0, 2.0),
    max_id: int = 71069,
    start_id: int | None = None,
    turn_back_at: int = 0,
    daily_span: int = 400,
    invalid_streak_stop: int = 0,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    pdf_scale: float = 1.0,
    browser_channel: str | None = None,
    stealth: bool = True,
    user_agents: list[str] | None = None,
    first_run: bool = False,
    retry_ids: list[int] | None = None,
    retry_probe=None,
    throttle_wait_ranges: list | None = None,
    throttle_max_rounds: int = DEFAULT_THROTTLE_ROUNDS,
) -> int:
    """按 law_display/<N> 逐个编号访问并导出 PDF，返回本次新增数量。

    retry_ids 给定时只重试这些编号（失败编号批量重试），不碰日常/首次运行的游标。

    - 编号有效 → 从详情页解析「分类 + 标题」，存为 `子分类[序号] 标题.pdf`；
    - 编号无效（页面提示「输入有误」）→ 跳过，不发额外请求；
    - 已登记且 PDF 还在的编号 → 直接跳过，**不发请求**；
    - 续跑从状态里的 `last_id + 1` 开始（首次运行中断也一样），不会回头重扫；
    - 首次运行扫到 max_id；日常只往后探测 `daily_span` 个编号（新文件编号更大）。
    """
    root = Path(output_dir) / "中纪检"
    root.mkdir(parents=True, exist_ok=True)
    delay_range = normalize_seconds_range(request_delay_seconds, default=(1.0, 1.0))
    detail_delay = normalize_seconds_range(
        detail_page_delay_seconds, default=(1.0, 1.0)
    )
    wait_ranges = _normalize_wait_ranges(throttle_wait_ranges)
    _restore_throttle_state(state)

    snap = state.snapshot()
    docs = snap.get("documents", {})
    base = sum(1 for d in docs.values() if d.get("source") == "ccdi")
    known_ok: dict[str, Path] = {
        key: Path(meta.get("pdf_path") or "")
        for key, meta in docs.items()
        if meta.get("source") == "ccdi" and meta.get("pdf_path")
    }
    # 类目翻页方式登记的编号是站点原样（如 ccdi:0004），按编号枚举用的是
    # ccdi:4，两者指向同一篇法规。归一化后比对，避免把已爬过的文件再下一次。
    known_by_nid: dict[int, tuple[str, Path]] = {}
    for doc_key, meta in docs.items():
        if meta.get("source") != "ccdi":
            continue
        match = re.fullmatch(r"ccdi:(\d+)", str(doc_key))
        if not match:
            continue
        try:
            known_by_nid[int(match.group(1))] = (
                str(doc_key),
                Path(meta.get("pdf_path") or ""),
            )
        except ValueError:
            continue
    per_cat: dict[str, int] = {}
    for meta in docs.values():
        if meta.get("source") == "ccdi":
            path = meta.get("category_path") or ""
            per_cat[path] = per_cat.get(path, 0) + 1

    record = state.get_id_walk_position("ccdi") or {}
    last_done_id = int(record.get("last_id") or 0)
    # 「最大有效编号」：只随真正抓到有效页面而前进。日常增量从这里 +1 开始；
    # 编号递增扫到上限却没有新增时，它保持不变（明天再核对同一段）。
    try:
        last_valid_id = int(record.get("last_valid_id") or 0)
    except (TypeError, ValueError):
        last_valid_id = 0
    if last_valid_id <= 0:
        last_valid_id = last_done_id
    last_seen_url = str(record.get("last_url") or "")
    # scanned_max：1~scanned_max 的编号都已经核对过（有效的已登记、无效的已确认没有）。
    # 重启后据此跳过旧编号，不再从 1 号重新访问。
    try:
        scanned_max = max(0, int(record.get("scanned_max") or 0))
    except (TypeError, ValueError):
        scanned_max = 0
    failed_ids: list[int] = []
    for item in record.get("failed_ids") or []:
        try:
            failed_ids.append(int(item))
        except (TypeError, ValueError):
            continue
    failed_set = set(failed_ids)
    failed_since = str(record.get("failed_since") or "")

    def mark_failed(nid_: int) -> None:
        """把编号记入失败列表；同时记下第一条失败出现的时间（供 30 分钟后重试用）。"""
        nonlocal failed_since
        if nid_ in failed_set:
            return
        failed_set.add(nid_)
        failed_ids.append(nid_)
        if len(failed_ids) > 200:
            failed_set.discard(failed_ids.pop(0))
        if not failed_since:
            failed_since = datetime.now().replace(microsecond=0).isoformat()

    retry_queue = sorted({int(i) for i in (retry_ids or [])})
    retry_mode = bool(retry_queue)
    resume_note = ""
    if retry_mode:
        start = retry_queue[0]
        resume_note = f"失败编号批量重试：{len(retry_queue)} 个"
    elif start_id is not None:
        start = max(1, int(start_id))
    elif last_done_id:
        if bool(record.get("turn_back_done")) or last_done_id >= max_id:
            # 全量扫描早就结束了：last_id 只是被某次重试写小的旧断点，不能当续爬点
            start = max_id
            resume_note = "全量扫描已完成，按日常维护检查总数"
        # 有断点就先核对断点这一号有没有真的完成（PDF 已登记且文件还在），
        # 完成则从下一号继续，没完成就重试这一号——不重扫更早的编号。
        elif (entry := known_by_nid.get(last_done_id)) is not None and path_exists(
            entry[1]
        ):
            start = last_done_id + 1
            resume_note = f"断点 {last_done_id} 号已完成，从 {start} 号续爬"
        else:
            start = last_done_id
            resume_note = f"断点 {last_done_id} 号未完成，先重试这一号"
            mark_failed(last_done_id)
    else:
        start = 1
    # 失败编号已经由开头的「预重试队列」处理，这里不再把 start 拉回它们
    # 日常维护时新发布的文件编号一定比上次更大，所以探测范围要往后放开一段，
    # 而不是被固定的 id_walk_max 卡住；连续若干个编号都不存在就提前收手。
    try:
        daily_span = max(20, int(daily_span))
    except (TypeError, ValueError):
        daily_span = 400
    try:
        # 0 = 不提前收手：每天把 id_walk_daily_span 这段编号扫完，
        # 避免因为连续空号提前停止而错过稍后发布的文件。
        streak_stop = max(0, int(invalid_streak_stop))
    except (TypeError, ValueError):
        streak_stop = 0
    ceiling = max_id
    if not first_run and last_done_id:
        ceiling = max(max_id, last_done_id + daily_span)

    totals_path = Path(output_dir) / CATEGORY_TOTALS_FILE
    measured: dict[str, int] = {}
    new_count = 0
    done = base
    scanned = 0
    loaded = 0
    aborted = False
    throttle_blocked = False
    last_id = last_done_id
    started_at = time.time()

    with PlaywrightPdfExporter(
        headless=headless,
        timeout_ms=timeout_ms,
        pdf_scale=pdf_scale,
        browser_channel=browser_channel,
        stealth=stealth,
        user_agents=user_agents,
    ) as exporter:
        page = exporter.page
        relaunch_count = 0

        def relaunch_browser(tag: str) -> bool:
            """浏览器意外关闭时重启；返回 False 表示重启失败或次数过多。"""
            nonlocal page, relaunch_count
            if relaunch_count >= BROWSER_RELAUNCH_LIMIT:
                logger.error(
                    "%s：浏览器已重启 %s 次仍不可用，结束本轮（重启程序可继续）",
                    tag,
                    relaunch_count,
                )
                return False
            relaunch_count += 1
            logger.warning(
                "%s：浏览器连接已断开，重启浏览器后继续（第 %s/%s 次）",
                tag,
                relaunch_count,
                BROWSER_RELAUNCH_LIMIT,
            )
            state.set_progress(
                "ccdi",
                message=f"{tag}：浏览器已断开，正在重启浏览器后继续…",
            )
            try:
                exporter.relaunch()
                page = exporter.page
                return True
            except Exception:
                logger.exception("%s：重启浏览器失败", tag)
                return False

        def measure_total(code: str) -> int | None:
            """量子分类条数；浏览器断了就重启后补量一次。"""
            try:
                return _measure_category_total(exporter, code, stop_event, pause_event)
            except BrowserConnectionClosed:
                if not relaunch_browser(f"测量 {code}"):
                    return None
                try:
                    return _measure_category_total(
                        exporter, code, stop_event, pause_event
                    )
                except BrowserConnectionClosed:
                    return None

        # 先把"正在估算"写进卡片：估算要访问首页 + 翻到最后一页，慢的时候不能毫无反馈
        # 首次运行可能连续跑好几天，这段时间里缓存不过期：中断重启时直接用
        # 上次算出来的总数，不必再去「全部数据」翻页重算。
        cached_total = state.get_total_estimate(
            "ccdi", None if first_run else 24.0
        )
        # 没有缓存时优先沿用上一次写在进度里的分母，避免每次重启都去访问
        # 「全部数据」再翻到末页（就是界面上看到的 /fgk/law_pagenumb）。
        try:
            prev_total = int(
                ((snap.get("progress") or {}).get("ccdi") or {}).get("total") or 0
            )
        except (TypeError, ValueError):
            prev_total = 0
        state.set_progress(
            "ccdi",
            status="running",
            current=done,
            total=max(cached_total or prev_total, base),
            message="按编号爬取：正在读取库总量…",
            note="",
        )
        if cached_total:
            logger.info(
                "使用已记录的库总量：%s 条（%s）",
                cached_total,
                "首次运行期间不过期" if first_run else "24 小时内有效",
            )
        available = cached_total or (prev_total if prev_total >= base else None)
        if not available:
            state.set_progress(
                "ccdi", message="正在访问「全部数据」页估算总数（可能需要几分钟）…"
            )
            available = _estimate_all_data_total(exporter, stop_event, pause_event)
            if available:
                state.set_total_estimate("ccdi", available)
        elif not cached_total and prev_total >= base:
            logger.info("沿用上次记录的库总量：%s 条（本轮结束后再重算）", prev_total)
        total = max(available or 0, base)
        if available and not cached_total:
            # 首次运行（或缓存过期后沿用上次进度）也把目标总数落盘，
            # 之后中断重启就直接用这个数，不再去站点重算。
            state.set_total_estimate("ccdi", int(available))
        # 估算总量时翻到的「全部数据」末页只是启动准备，别让它一直挂在
        # 卡片的「上一个页面」上，看起来像又回去翻类目了。
        exporter.prev_url = ""
        exporter.prev_status = 0
        logger.info(
            "按编号爬取：%s（%s → %s，已登记 %s 条，库总量 %s 条%s）",
            resume_note or f"从 {start} 号开始",
            start,
            ceiling,
            base,
            available,
            f"，上次访问 {last_seen_url}" if last_seen_url else "",
        )
        state.set_progress(
            "ccdi",
            status="running",
            current=done,
            total=total,
            extra=_visit_extras(exporter),
            message=(
                f"{resume_note}（本轮扫到 {ceiling} 号）"
                if resume_note
                else f"按编号爬取：从 {start} 号开始（本轮扫到 {ceiling} 号）"
            ),
            note="",
        )

        # 日常维护：先比对子分类条数，没有缺口就不用扫编号。
        # 但如果上一轮还没扫到编号上限（首次全量没跑完就中断了），就别先花几分钟
        # 量 26 个子分类——直接接着扫，否则点「开始」后看起来像没反应。
        # 双向扫描已经跑完（turn_back_done）也说明全量扫描结束——不能只看 last_id，
        # 断点游标可能因为某次重试被写成了很小的编号
        turn_back_done = bool(record.get("turn_back_done"))
        sweep_complete = last_done_id >= max_id or turn_back_done
        if first_run and sweep_complete and total and base >= total:
            # 外部状态里可能还留着过期的「首次运行」标记；既然编号扫到了上限、
            # 文件数也够了，就按日常维护走（先比总数，不直接扫编号）
            logger.info(
                "已登记 %s 条、编号已扫到 %s，按日常维护处理（首次运行标记视为过期）",
                base,
                max_id,
            )
            first_run = False
        daily_mode = (not first_run) and sweep_complete and not retry_mode
        if daily_mode:
            # 1) 访问「全部数据」首页，用页码算出总数，和上次保存的对比
            current_total = _estimate_all_data_total(
                exporter, stop_event, pause_event
            )
            saved_total = state.get_total_estimate("ccdi", None) or 0
            if current_total:
                logger.info(
                    "目录总数：%s 条（上次 %s 条）",
                    current_total,
                    saved_total or "无记录",
                )
                state.set_total_estimate("ccdi", current_total)
            if current_total and saved_total and current_total <= saved_total:
                logger.info(
                    "目录总数没有增加（%s → %s），本轮无新增",
                    saved_total,
                    current_total,
                )
                state.set_progress(
                    "ccdi",
                    status="idle",
                    current=done,
                    total=total,
                    message=f"完成，本次新增 0 个（目录总数仍为 {current_total} 条）",
                    extra={"pending": False},
                )
                state.set_last_crawl("ccdi")
                state.mark_pass_done("ccdi")
                return 0

            # 2) 有增加 → 高级检索：发布日期选今年，按结果页顺序爬
            new_ids, min_seen, listing_status = _search_by_year(
                exporter,
                datetime.now().year,
                set(known_by_nid.keys()),
                stop_event,
                pause_event,
            )
            if not new_ids and listing_status in ("failed", "captcha"):
                # 3) 检索不可用（常见：滑动验证）→ 退回「全部数据」目录页扫描
                logger.info("高级检索不可用，改用「全部数据」目录页检查")
                new_ids, min_seen, listing_status = _scan_index_listing(
                    exporter, set(known_by_nid.keys()), stop_event, pause_event
                )
            if new_ids:
                start = min(new_ids)
                ceiling = max(new_ids)
                resume_note = (
                    f"目录页发现 {len(new_ids)} 条新条目"
                    f"（编号 {start}~{ceiling}），按目录页顺序补爬"
                )
                logger.info(resume_note)
            elif (
                listing_status == "failed"
                and min_seen is not None
                and min_seen - 1 >= last_valid_id + 1
            ):
                # 2) 翻页出问题：从这次见到的最小号往前补，一直补到最大有效编号
                start = last_valid_id + 1
                ceiling = min_seen - 1
                resume_note = (
                    f"目录页翻页异常，改为补 {start}~{ceiling} 号"
                    f"（从目录里见到的 {min_seen} 号往前倒推到 {last_valid_id} 号）"
                )
                logger.info(resume_note)
            else:
                # 3) 目录页没有新增（或打不开）→ 按编号从最大有效编号往后探测
                # 起点不能低于已经扫过的游标（last_id），否则会把很早以前的编号当成"新增"重扫
                start = max(last_valid_id, last_id) + 1
                # 只探测 daily_span 这一段；不能拿 max_id 兜底，否则最大有效编号
                # 离上限很远时会一口气扫几万个编号
                ceiling = start + daily_span - 1
                resume_note = (
                    f"目录页没有新增，按编号从 {start} 号往后探测到 {ceiling} 号"
                )
                logger.info(resume_note)
            state.set_progress(
                "ccdi",
                current=done,
                total=total,
                message=resume_note,
                extra={"pending": False},
            )
        elif not first_run:
            logger.info(
                "上一轮只扫到 %s 号（上限 %s），跳过目录检查，直接续扫",
                last_done_id,
                max_id,
            )

        # ---- 双向扫描：正向扫到 turn_back_at 后，从 max_id 倒着往上补 ----
        try:
            turn_back_at = max(0, int(turn_back_at))
        except (TypeError, ValueError):
            turn_back_at = 0
        if turn_back_at >= max_id:
            turn_back_at = 0
        turn_back_done = bool(record.get("turn_back_done"))
        try:
            back_last_id = int(record.get("back_last_id") or 0)
        except (TypeError, ValueError):
            back_last_id = 0
        try:
            forward_last_valid = int(record.get("forward_last_valid") or 0)
        except (TypeError, ValueError):
            forward_last_valid = 0
        try:
            backward_last_valid = int(record.get("backward_last_valid") or 0)
        except (TypeError, ValueError):
            backward_last_valid = 0
        try:
            backward_max_valid = int(record.get("backward_max_valid") or 0)
        except (TypeError, ValueError):
            backward_max_valid = 0
        try:
            # 正向实际扫到过的最大编号（补扫段也会推进它，和断点游标 last_id 区分开）
            forward_scanned_to = int(
                record.get("forward_scanned_to") or last_done_id or 0
            )
        except (TypeError, ValueError):
            forward_scanned_to = last_done_id
        phase = str(record.get("phase") or "")
        backward_mode = (
            not retry_mode
            and turn_back_at > 0
            and not turn_back_done
            and (last_done_id <= turn_back_at or phase == "backward")
        )
        pending_ids = list(retry_queue) if retry_mode else []
        direction = 1
        nid = start
        if retry_mode:
            # 只重试失败编号：不碰主游标，逐个处理 pending_ids
            pass
        elif backward_mode and phase == "backward":
            # 上次停在倒爬阶段：从更小的编号继续往下走
            direction = -1
            nid = (back_last_id - 1) if back_last_id else ceiling
            logger.info(
                "续跑倒爬阶段：从 %s 号继续向下（上次停在 %s 号）", nid, back_last_id
            )
        forward_end = turn_back_at if backward_mode else ceiling
        hit_target = False
        reached_target = False
        if retry_mode:
            main_phase = "forward"
            phase_kind = "retry"
            current_end = start
        else:
            main_phase = "backward" if direction == -1 else "forward"
            phase_kind = main_phase
            current_end = forward_end if direction == 1 else turn_back_at
        # 断点续跑：先把上次失败的编号重试一遍，再继续主流程
        main_dir, main_nid, main_end = direction, nid, current_end
        if total and base >= total and failed_ids and not retry_mode:
            # 已经凑够库总量：剩下的"失败编号"基本都是无效网址，不再重试
            logger.info(
                "已登记 %s 条，达到库总量 %s，清空遗留的失败编号：%s",
                base,
                total,
                failed_ids,
            )
            failed_ids.clear()
            failed_set.clear()
            failed_since = ""
        elif not retry_mode and failed_ids:
            pending_ids = sorted(set(failed_ids))
            phase_kind = "retry"
            logger.info(
                "续跑前先重试失败编号 %s 个：%s", len(pending_ids), pending_ids[:10]
            )

        # ---- 配置改了怎么办：首次运行没跑完时补扫新增出来的区间 ----
        # 例：原来正向到 6405、正在从 71069 倒爬；把正向改成 7200 →
        #     先把 6406~7200 补上，再回到原来的倒爬位置继续往下。
        #     把 id_walk_max 调大也一样：先补「新上限 ~ 旧上限」这一段。
        segments: list[tuple[int, int, int]] = []  # (direction, start, end)
        try:
            prev_turn_back = int(record.get("turn_back_at_used") or last_done_id or 0)
        except (TypeError, ValueError):
            prev_turn_back = last_done_id
        try:
            prev_max = int(record.get("max_id_used") or max_id)
        except (TypeError, ValueError):
            prev_max = max_id
        if first_run and backward_mode and main_phase == "backward":
            if turn_back_at > prev_turn_back and turn_back_at > last_done_id:
                segments.append((1, last_done_id + 1, turn_back_at))
            if max_id > prev_max:
                # 倒爬段的 end 是「到此为止（不含）」：处理到 prev_max+1 就停
                segments.append((-1, max_id, prev_max))
        if segments:
            logger.info(
                "检测到首次运行配置变化，先补扫：%s",
                "、".join(
                    f"{'正向' if d == 1 else '倒爬'} {s}~{e if d == 1 else e + 1}"
                    for d, s, e in segments
                ),
            )
            main_nid, main_end = nid, current_end
            direction, nid, current_end = segments.pop(0)
            phase_kind = "comp"
        else:
            main_nid, main_end = nid, current_end

        def note_position(position: int, *, valid: bool = False) -> None:
            """记录正向/倒爬两套游标，以及各自遇到的最后一个有效编号。"""
            nonlocal last_id, back_last_id, forward_last_valid, backward_last_valid
            nonlocal last_valid_id, forward_scanned_to, backward_max_valid
            if valid:
                last_valid_id = max(last_valid_id, position)
                if daily_mode:
                    # 日常新增：按位置归入「正爬最大 / 倒爬最小 / 倒爬最大」三个边界
                    old_fmax = forward_last_valid
                    old_bmin = backward_last_valid
                    old_bmax = backward_max_valid
                    if old_bmax and position > old_bmax:
                        backward_max_valid = position
                        logger.info(
                            "新增 %s 号落在 [倒爬最大有效 %s, +∞)，"
                            "倒爬最大有效更新为 %s",
                            position,
                            old_bmax,
                            position,
                        )
                    elif (
                        old_fmax
                        and old_bmin
                        and old_fmax < position < old_bmin
                    ):
                        if position - old_fmax <= old_bmin - position:
                            forward_last_valid = position
                            logger.info(
                                "新增 %s 号落在 [正爬最大 %s, 倒爬最小 %s] 空档，"
                                "正爬最大有效更新为 %s",
                                position,
                                old_fmax,
                                old_bmin,
                                position,
                            )
                        else:
                            backward_last_valid = position
                            logger.info(
                                "新增 %s 号落在 [正爬最大 %s, 倒爬最小 %s] 空档，"
                                "倒爬最小有效更新为 %s",
                                position,
                                old_fmax,
                                old_bmin,
                                position,
                            )
                    else:
                        logger.info(
                            "新增 %s 号不在这两个重点区间内，三个边界保持不变",
                            position,
                        )
                elif direction == -1:
                    backward_max_valid = max(backward_max_valid, position)
            if direction == 1:
                if not daily_mode and phase_kind != "retry":
                    forward_scanned_to = max(forward_scanned_to, position)
            if phase_kind == "comp":
                # 补扫段不移动主游标，只更新 last_valid_id（正向段顺带更新正向有效编号）
                if valid and direction == 1 and not daily_mode:
                    forward_last_valid = max(forward_last_valid, position)
                return
            if phase_kind == "retry":
                # 失败编号重试：只更新 last_valid_id，不碰任何主游标
                return
            if direction == 1:
                last_id = max(last_id, position)
                if valid and not daily_mode:
                    forward_last_valid = max(forward_last_valid, position)
            else:
                back_last_id = position if not back_last_id else min(back_last_id, position)
                if valid and not daily_mode:
                    # 倒着走，最后一次遇到的就是最小的有效编号
                    backward_last_valid = position

        def save_position() -> None:
            state.set_id_walk_position(
                "ccdi",
                last_id,
                scanned_max=max(scanned_max, last_id),
                failed_ids=failed_ids,
                failed_since=failed_since or "",
                last_url=last_seen_url,
                back_last_id=back_last_id,
                phase=main_phase,
                forward_last_valid=forward_last_valid,
                backward_last_valid=backward_last_valid,
                backward_max_valid=backward_max_valid,
                last_valid_id=last_valid_id,
                turn_back_done=turn_back_done,
                turn_back_at_used=turn_back_at,
                max_id_used=max_id,
                forward_scanned_to=forward_scanned_to,
            )

        save_every = 20
        since_save = 0
        invalid_streak = 0
        # 只有「比上次核对过的地方还靠后」的连续空号才用来提前收手，
        # 首次运行必须老实扫到 max_id，不能因为中间大段空号就停。
        streak_base = max(scanned_max, last_done_id)
        while True:
            from_queue = False
            if (
                retry_probe is not None
                and not pending_ids
                and phase_kind != "retry"
                and not retry_mode
            ):
                extra_ids = retry_probe()
                if extra_ids:
                    # 抢占式重试：记下当前编号，插队重试失败编号，完成后回到这里
                    main_dir, main_nid, main_end = direction, nid, current_end
                    pending_ids = sorted(set(int(i) for i in extra_ids))
                    phase_kind = "retry"
                    logger.info(
                        "暂停主扫描（停在 %s 号），先重试失败编号 %s 个：%s",
                        nid,
                        len(pending_ids),
                        pending_ids[:10],
                    )
            if pending_ids:
                # 失败编号重试模式：按队列逐个处理
                nid = pending_ids.pop(0)
                direction = 1
                from_queue = True
            if not from_queue and phase_kind == "retry":
                # 预重试队列处理完：独立重试任务到此结束，否则回到主流程
                if retry_mode:
                    break
                phase_kind = main_phase
                direction, nid, current_end = main_dir, main_nid, main_end
                logger.info("失败编号预重试完成，继续主流程")
                continue
            if not from_queue and direction == 1 and nid > current_end:
                if segments:
                    direction, nid, current_end = segments.pop(0)
                    phase_kind = "comp"
                    logger.info(
                        "继续补扫：%s %s~%s",
                        "正向" if direction == 1 else "倒爬",
                        nid,
                        current_end,
                    )
                    continue
                if phase_kind == "comp":
                    # 补扫段结束 → 回到原来的主游标（例如倒爬停在 68554）
                    phase_kind = main_phase
                    direction = -1 if main_phase == "backward" else 1
                    nid, current_end = main_nid, main_end
                    logger.info(
                        "补扫完成，回到主流程：%s %s 号继续",
                        "倒爬" if direction == -1 else "正向",
                        nid,
                    )
                    continue
                if backward_mode:
                    main_phase = "backward"
                    phase_kind = "backward"
                    direction = -1
                    nid = (back_last_id - 1) if back_last_id else ceiling
                    current_end = turn_back_at
                    logger.info(
                        "正向已扫到 %s 号（正向最后一个有效编号 %s），"
                        "改为从 %s 号倒着往上补",
                        forward_end,
                        forward_last_valid or "无",
                        nid,
                    )
                    state.set_progress(
                        "ccdi",
                        current=done,
                        total=total,
                        message=(
                            f"正向已到 {forward_end} 号，改为从 {nid} 号倒着爬…"
                        ),
                        extra={
                            **_visit_extras(exporter),
                            "forward_last_valid": forward_last_valid,
                            "phase": "backward",
                        },
                    )
                    save_position()
                    continue
                break
            if not from_queue and direction == -1 and nid <= current_end:
                if segments:
                    direction, nid, current_end = segments.pop(0)
                    phase_kind = "comp"
                    logger.info(
                        "继续补扫：%s %s~%s",
                        "正向" if direction == 1 else "倒爬",
                        nid,
                        current_end,
                    )
                    continue
                if phase_kind == "comp":
                    phase_kind = main_phase
                    direction = -1 if main_phase == "backward" else 1
                    nid, current_end = main_nid, main_end
                    logger.info(
                        "补扫完成，回到主流程：%s %s 号继续",
                        "倒爬" if direction == -1 else "正向",
                        nid,
                    )
                    continue
                break
            if stop_event and stop_event.is_set():
                aborted = True
                break
            wait_while_paused(pause_event, stop_event)
            if stop_event and stop_event.is_set():
                aborted = True
                break

            key = f"ccdi:{nid}"
            known_entry = known_by_nid.get(nid)
            existing = known_entry[1] if known_entry else known_ok.get(key)
            if path_exists(existing):
                # 已登记且 PDF 还在：这一定是个有效编号
                note_position(nid, valid=True)
                nid += direction
                continue
            if known_entry is not None:
                # 沿用旧记录的主键（避免同一篇法规出现两条记录）
                key = known_entry[0]

            # 之前已经核对过、确认没有内容的编号，直接跳过，不再发请求
            # （日常增量不跳：每天要把「最大有效编号」之后这一段重新核对一遍，
            #  否则昨天扫过的空号会把今天新发布的文件一起跳过）
            if (
                direction == 1
                and not daily_mode
                and phase_kind not in ("comp", "retry")
                and nid <= scanned_max
                and nid not in failed_set
            ):
                note_position(nid)
                nid += direction
                continue

            url = CCDI_DISPLAY_URL.format(nid=nid)
            last_seen_url = url
            parsed = None
            html = ""
            attempt = 0
            while True:
                attempt += 1
                if attempt > 4:
                    break
                # 先把"正在访问编号 N"写进进度：页面加载慢时界面也能看出在动
                state.set_progress(
                    "ccdi",
                    message=f"[编号 {nid} · 已爬 {done}/{total or '—'}] 正在打开详情页…",
                    extra=_visit_extras(exporter, pending_url=url),
                )
                nav_started = time.time()
                try:
                    ok = _safe_goto(page, url, timeout_ms=45_000)
                    if not ok:
                        # 慢响应/超时：退一步用 commit 方式再试，内容随后再读
                        ok = _safe_goto(
                            page, url, timeout_ms=25_000, wait_until="commit"
                        )
                        if ok:
                            time.sleep(2.0)
                except BrowserConnectionClosed:
                    # 浏览器被关掉/崩溃：重启后重试同一个编号
                    if relaunch_browser(f"编号 {nid}"):
                        continue
                    aborted = True
                    break
                if not ok:
                    if not sleep_with_stop(
                        RETRY_DELAY_SECONDS, stop_event, pause_event
                    ):
                        aborted = True
                    continue
                nav_seconds = time.time() - nav_started
                if nav_seconds > 10:
                    logger.warning(
                        "编号 %s 打开较慢：%.1f 秒（%s）", nid, nav_seconds, page.url
                    )
                html = _read_list_html(page, stop_event) or ""
                if _is_error_page(page) or _is_throttle_page(page):
                    action = _throttle_backoff(
                        state,
                        stop_event,
                        pause_event,
                        wait_ranges,
                        throttle_max_rounds,
                        f"编号 {nid}",
                        visit_extra=_visit_extras(exporter),
                    )
                    if action == "stopped":
                        aborted = True
                        break
                    if action == "exhausted":
                        throttle_blocked = True
                        break
                    continue
                if _is_maintenance_page(page):
                    wait = (
                        MAINTENANCE_RETRY_DELAYS[0]
                        if attempt == 1
                        else random_delay(MAINTENANCE_COOLDOWN_SECONDS)
                    )
                    logger.warning(
                        "编号 %s 落到维护页（%s），暂停 %.0f 秒后再试",
                        nid,
                        page.url,
                        wait,
                    )
                    state.set_progress(
                        "ccdi",
                        message=f"遇到站点维护/拦截页，暂停 {wait / 60:.1f} 分钟后重试编号 {nid}…",
                        extra=_visit_extras(exporter),
                    )
                    if not sleep_with_stop(wait, stop_event, pause_event):
                        aborted = True
                        break
                    continue
                parsed = _parse_display_page(html) if html else None
                break
            if aborted or throttle_blocked:
                break

            if parsed is None:
                if _is_invalid_display_page(html):
                    # 站点明确说「输入有误」：这个编号确实没有文件
                    # （重试时遇到这种编号算「已处理」，不再留在 failed_ids 里）
                    invalid_streak += 1
                    if nid in failed_set:
                        failed_set.discard(nid)
                        failed_ids = [item for item in failed_ids if item != nid]
                        if not failed_ids:
                            failed_since = ""
                        logger.info("编号 %s 确认为无效编号，从失败列表移除", nid)
                else:
                    # 页面打开了但没读到内容（超时/空白）：记下来，下次运行再试
                    invalid_streak = 0
                    mark_failed(nid)
                note_position(nid)
                scanned += 1
                nid += direction
                if delay_range[1] > 0:
                    sleep_with_stop(
                        random_delay(delay_range), stop_event, pause_event
                    )
                since_save += 1
                if since_save >= save_every:
                    save_position()
                    since_save = 0
                if (
                    direction == 1
                    and not first_run
                    and nid > streak_base
                    and streak_stop > 0
                    and invalid_streak >= streak_stop
                ):
                    logger.info(
                        "编号 %s 之后连续 %s 个编号都没有内容，本轮探测到此结束",
                        streak_base,
                        invalid_streak,
                    )
                    break
                continue

            main_cat, sub_cat, title = parsed
            invalid_streak = 0
            if nid in failed_set:
                failed_set.discard(nid)
                failed_ids = [item for item in failed_ids if item != nid]
                if not failed_ids:
                    failed_since = ""
            # 分类缺失时的兜底：没有二级 → <主分类>/其他；一级二级都没有 →
            # 根目录下的「其他」，文件名用 [序号]（不带子分类前缀）
            cat_path = f"{main_cat}/{sub_cat}" if sub_cat else main_cat
            seq = per_cat.get(cat_path, 0) + 1
            if sub_cat:
                folder = root / main_cat / sub_cat
                prefix = f"{sub_cat}[{seq}] "
            else:
                folder = root / main_cat
                prefix = f"[{seq}] "
            # 文件名按 UTF-8 字节截断：Linux 单个文件名上限 255 字节，
            # 中文标题一长就会 OSError: File name too long（旧版本会整轮崩掉）。
            candidate = folder / fit_filename(prefix, sanitize_filename(title))
            try:
                if candidate.exists():
                    candidate = ensure_unique_path(candidate)
                exporter.save_current_page_as_pdf(candidate)
            except OSError as exc:
                logger.error(
                    "导出失败 编号 %s：%s（文件名 %s 字节，已记入待重试）",
                    nid,
                    exc,
                    len(candidate.name.encode("utf-8")),
                )
                mark_failed(nid)
                note_position(nid)
                nid += direction
                continue
            except Exception as exc:
                if is_browser_closed_error(exc):
                    # 导出时浏览器挂了：删掉半截 PDF，重启浏览器后重试这一条
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
                    if relaunch_browser(f"编号 {nid} 导出"):
                        continue
                    aborted = True
                    break
                logger.exception("导出失败 编号 %s", nid)
                mark_failed(nid)
                note_position(nid)
                nid += direction
                continue
            loaded += 1

            if detail_delay[1] > 0:
                sleep_with_stop(random_delay(detail_delay), stop_event, pause_event)

            if _is_error_page(page) or _is_throttle_page(page):
                logger.warning("编号 %s 导出的是限流页，丢弃", nid)
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
                continue

            registered = state.register_document(
                key,
                source="ccdi",
                title=title,
                url=url,
                pdf_path=str(candidate.resolve()),
                category_path=cat_path,
            )
            if registered:
                per_cat[cat_path] = seq
                known_ok[key] = candidate
                new_count += 1
                done += 1
            else:
                # 已登记但旧 PDF 不在了：这次重新导出的文件接管记录
                state.repoint_document(key, str(candidate.resolve()))
                known_ok[key] = candidate
            scanned += 1
            note_position(nid, valid=True)
            state.set_progress(
                "ccdi",
                current=done,
                message=f"[编号 {nid} · 已爬 {done}/{total}] {title}",
                extra={
                    **_visit_extras(exporter),
                    "category": cat_path,
                    "scanned": scanned,
                    "new_count": new_count,
                    "loaded": loaded,
                },
            )
            since_save += 1
            if since_save >= save_every:
                save_position()
                since_save = 0
            # 累计文件数达到库总量（6444）就停：不管当前在补扫段还是倒爬段
            # 不区分首次/重试：只要累计达到库总量就收尾（重试补齐最后一条也算）
            if registered and total and done >= total:
                reached_target = True
                hit_target = direction == -1
                if direction == -1:
                    backward_last_valid = nid
                logger.info(
                    "在 %s 号达到库总量 %s 条，首次运行到此结束（本次新增 %s 个）",
                    nid,
                    total,
                    new_count,
                )
                save_position()
                break
            nid += direction

        if backward_mode and not aborted and not throttle_blocked:
            # 倒爬阶段结束（不论是否达到总量），整个编号区间都算覆盖过
            turn_back_done = True
            last_id = max(last_id, ceiling)
            main_phase = "forward"
            logger.info(
                "双向扫描结束：正向最后一个有效编号 %s，倒爬最后一个有效编号 %s"
                "（累计已爬 %s 条%s）",
                forward_last_valid or "无",
                backward_last_valid or "无",
                done,
                "，已达到库总量" if hit_target else "",
            )
        if reached_target:
            # 达到 6444 条即视为首次运行完成，后续按日常增量走
            turn_back_done = True
            last_id = max(last_id, max_id)
            main_phase = "forward"
            logger.info("累计达到库总量 %s 条，首次运行标记可注销", total)
        save_position()
        elapsed = (time.time() - started_at) / 60.0
        logger.info(
            "按编号爬取结束：正向 %s~%s / 倒爬停在 %s，本次新增 %s，用时 %.1f 分钟",
            start,
            last_id,
            back_last_id or "—",
            new_count,
            elapsed,
        )

        # 收尾：量一遍各子分类条数，标出"已爬 < 应有"的分类
        short_categories: list[str] = []
        if not aborted and not throttle_blocked:
            for main_cat, sub_cat, code in CCDI_CATEGORY_PATHS:
                value = measure_total(code)
                if value is None:
                    continue
                measured[code] = value
                have = per_cat.get(f"{main_cat}/{sub_cat}", 0)
                state.set_category_status(
                    "ccdi",
                    code,
                    main=main_cat,
                    sub=sub_cat,
                    status="done" if have >= value else "partial",
                    total=value,
                    total_estimated=False,
                    reason="" if have >= value else "已爬少于目录条数",
                    page=1,
                )
                if have < value:
                    short_categories.append(f"{main_cat}/{sub_cat}")
                    logger.warning(
                        "子分类 %s/%s 已爬 %s 条，目录显示 %s 条，缺 %s 条",
                        main_cat,
                        sub_cat,
                        have,
                        value,
                        value - have,
                    )
            if measured:
                _save_category_totals(totals_path, measured)
            # 一级目录总数（用于界面上「其他」的减法统计）
            for main_name, main_code in (
                ("党内法规制度", "law_validity_01"),
                ("国家法律法规", "law_validity_02"),
            ):
                main_total = measure_total(main_code)
                if main_total is None:
                    continue
                state.set_category_status(
                    "ccdi",
                    main_code,
                    main=main_name,
                    sub="",
                    status="done",
                    total=main_total,
                    total_estimated=False,
                    reason="",
                    page=1,
                )

    if throttle_blocked:
        state.set_alert(THROTTLE_ALERT_KEY, THROTTLE_ALERT_TEXT, level="error")
    boundary = ""
    if backward_mode:
        boundary = (
            f"；正向最后一个有效编号 {forward_last_valid or '无'}，"
            f"倒爬最后一个有效编号 {backward_last_valid or '无'}"
        )
    if retry_mode:
        remaining = len(failed_ids)
        message = (
            f"失败编号重试完成：成功 {max(0, len(retry_queue) - remaining)} 个，"
            f"仍失败 {remaining} 个"
        )
        if remaining:
            message += "（" + "、".join(str(i) for i in failed_ids[:8]) + "…）"
    elif aborted:
        stop_at = back_last_id if direction == -1 else last_id
        message = f"已终止，本次新增 {new_count} 个（停在 {stop_at or '—'} 号）{boundary}"
    elif throttle_blocked:
        message = f"站点持续限流，本轮提前结束，新增 {new_count} 个{boundary}"
    else:
        message = (
            f"完成，本次新增 {new_count} 个（累计 {done} 条，"
            f"用时 {(time.time()-started_at)/60:.0f} 分钟）{boundary}"
        )
        if short_categories:
            message += (
                f"；仍有 {len(short_categories)} 个子分类条数不足（见子分类进度），"
                "下一轮会继续核对"
            )
        state.clear_alert(THROTTLE_ALERT_KEY)
        # 首次执行标记只有在「编号确实扫到上限」时才算跑完；否则中断重启后继续按首次运行处理。
        if last_id >= max_id or reached_target:
            state.mark_pass_done("ccdi")
        else:
            message += f"；编号还没扫到上限 {max_id}，保留「首次执行」标记"
            logger.info(
                "本轮只扫到 %s 号（上限 %s），不注销首次执行标记",
                last_id,
                max_id,
            )
    state.set_progress(
        "ccdi",
        status="idle",
        message=message,
        extra={
            "scanned": scanned,
            "new_count": new_count,
            "loaded": loaded,
            "category": "",
            "category_code": "",
        },
    )
    state.set_last_crawl("ccdi")
    return new_count


