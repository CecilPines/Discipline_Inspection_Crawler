"""爬虫共用工具：配置加载、文件名清理、Playwright PDF 导出。"""

from __future__ import annotations

import os
import random
import re
import threading
import time
import logging
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import Browser, Page, sync_playwright

# Windows 文件名不允许的字符 → 换成全角形式，尽量保留标题原样（不再粗暴替换成 _）
WIN_CHAR_MAP = {
    "<": "＜",
    ">": "＞",
    ":": "：",
    "/": "／",
    "\\": "＼",
    "|": "｜",
    "?": "？",
    "*": "＊",
}

logger = logging.getLogger(__name__)

TRUNCATION_MARKERS = ("...", "…")

BROWSER_CLOSED_MARKERS = (
    "playwright connection closed",
    "target page, context or browser has been closed",
    "connection closed while reading from the driver",
    "target closed",
    "browser has been closed",
)

# User-Agent 池（均为 Windows 桌面浏览器，和 stealth 脚本里的 navigator.platform 保持一致）
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]


def is_truncated_title(title: str) -> bool:
    """导航页链接文字是否被截断（含省略号）。"""
    return any(marker in title for marker in TRUNCATION_MARKERS)


class BrowserConnectionClosed(Exception):
    """Playwright 浏览器连接已断开（需要重启浏览器）。"""


def is_browser_closed_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in BROWSER_CLOSED_MARKERS)


def normalize_seconds_range(
    value, *, default: tuple[float, float]
) -> tuple[float, float]:
    """把配置里的时间归一化成 (最小秒, 最大秒)。

    支持：单个数字（固定值）、[min, max] 列表、以及 "1-3" / "1~3" / "1,3" 这类字符串。
    无法解析时返回 default。
    """
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        sep = re.compile(r"[~\-–—,;，、\s]+")
        parts: list = sep.split(text) if sep.search(text) else [text]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = [value]
    try:
        nums = [max(0.0, float(p)) for p in parts if str(p).strip() != ""]
    except (TypeError, ValueError):
        return default
    if not nums:
        return default
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (min(nums[0], nums[1]), max(nums[0], nums[1]))


def random_delay(seconds_range: tuple[float, float]) -> float:
    """在区间内随机取一个秒数（下限=上限时为固定值）。"""
    low, high = seconds_range
    return random.uniform(low, high) if high > low else low


def sleep_with_stop(
    seconds: float,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> bool:
    """可被暂停/终止打断的等待：暂停不计入等待时长。

    返回 False 表示等待期间收到了终止请求。
    """
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if stop_event is not None and stop_event.is_set():
            return False
        if pause_event is not None and pause_event.is_set():
            wait_while_paused(pause_event, stop_event)
            continue
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step
    return not (stop_event is not None and stop_event.is_set())


def wait_while_paused(
    pause_event: threading.Event | None,
    stop_event: threading.Event | None = None,
) -> None:
    """暂停期间阻塞于此，直到恢复或收到终止请求。用于爬取/上传循环边界。"""
    if pause_event is None:
        return
    while pause_event.is_set():
        if stop_event is not None and stop_event.is_set():
            return
        time.sleep(0.5)


# 仅 Windows 下优先使用项目内的浏览器目录（避免沙箱/临时目录失效）。
# Linux/macOS 一律使用 Playwright 默认缓存：若把 Windows 版的 .playwright-browsers 一起拷过去，
# 里面的可执行文件在 Linux 上不可用，这里直接忽略，避免启动浏览器失败。
_PLAYWRIGHT_DIR = Path(__file__).resolve().parent / ".playwright-browsers"
if (
    os.name == "nt"
    and _PLAYWRIGHT_DIR.exists()
    and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_PLAYWRIGHT_DIR)


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or Path(__file__).resolve().parent / "config.yaml"
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    ragflow = cfg.setdefault("ragflow", {})
    env_key = os.environ.get("RAGFLOW_API_KEY")
    if env_key:
        ragflow["api_key"] = env_key
    return cfg


def _resolve_range(
    low_value, high_value, *, default: tuple[float, float]
) -> tuple[float, float]:
    """两个配置项合成区间：都缺省用 default，只给一个时按固定值处理。"""
    if low_value is None and high_value is None:
        return default
    low = high_value if low_value is None else low_value
    high = low_value if high_value is None else high_value
    return normalize_seconds_range([low, high], default=default)


def resolve_delay_range(crawl_cfg: dict[str, Any]) -> tuple[float, float]:
    """中纪检每条之间的随机间隔区间（秒）。

    优先用 crawl.request_delay_min_seconds / _max_seconds；两者都未配置时，
    兼容旧配置里的单个 crawl.request_delay_seconds（按固定间隔处理）。
    """
    low = crawl_cfg.get("request_delay_min_seconds")
    high = crawl_cfg.get("request_delay_max_seconds")
    if low is None and high is None:
        legacy = crawl_cfg.get("request_delay_seconds")
        if legacy is None:
            return (1.0, 3.0)
        return normalize_seconds_range(legacy, default=(1.0, 1.0))
    return _resolve_range(low, high, default=(1.0, 3.0))


def resolve_bit_delay_range(crawl_cfg: dict[str, Any]) -> tuple[float, float]:
    """校纪检每条之间的随机间隔区间（秒），默认 0.5~1 秒（该站点无反爬）。"""
    low = crawl_cfg.get("bit_request_delay_min_seconds")
    high = crawl_cfg.get("bit_request_delay_max_seconds")
    if low is None and high is None:
        legacy = crawl_cfg.get("request_delay_seconds")
        if legacy is None:
            return (0.5, 1.0)
        return normalize_seconds_range(legacy, default=(0.5, 1.0))
    return _resolve_range(low, high, default=(0.5, 1.0))


def resolve_detail_delay_range(crawl_cfg: dict[str, Any]) -> tuple[float, float]:
    """中纪检每导出一条详情页后额外停顿的区间（秒），默认 1~2 秒。"""
    return _resolve_range(
        crawl_cfg.get("detail_page_delay_min_seconds"),
        crawl_cfg.get("detail_page_delay_max_seconds"),
        default=(1.0, 2.0),
    )


# 限流退避的默认等待区间（秒）：第 1 次 10~15 分钟，第 2 次 2~3 分钟，第 3 次 1~2 分钟。
# 第一次等久一点让站点的限流窗口过去；之后多半只是瞬时抖动，短等即可。
DEFAULT_THROTTLE_WAIT_RANGES: list[tuple[float, float]] = [
    (600.0, 900.0),
    (120.0, 180.0),
    (60.0, 120.0),
]


def resolve_throttle_ranges(crawl_cfg: dict[str, Any]) -> list[tuple[float, float]]:
    """限流退避每轮的等待区间（秒），按顺序对应第 1/2/3… 次退避。

    优先读 `crawl.throttle_wait_ranges`（单位：**分钟**，如 `[[10, 15], [2, 3], [1, 2]]`）；
    没配置时兼容旧的单个区间 `crawl.throttle_wait_min_seconds ~ _max_seconds`（所有轮次同值）；
    都没有则用默认的逐轮区间。轮数超过配置条数时沿用最后一条。
    """
    raw = crawl_cfg.get("throttle_wait_ranges")
    ranges: list[tuple[float, float]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                low = max(0.0, float(item[0])) * 60.0
                high = max(0.0, float(item[1])) * 60.0
            except (TypeError, ValueError):
                continue
            ranges.append((low, high) if low <= high else (high, low))
    if ranges:
        return ranges

    legacy_low = crawl_cfg.get("throttle_wait_min_seconds")
    legacy_high = crawl_cfg.get("throttle_wait_max_seconds")
    if legacy_low is not None or legacy_high is not None:
        return [_resolve_range(legacy_low, legacy_high, default=(240.0, 360.0))]
    return list(DEFAULT_THROTTLE_WAIT_RANGES)


def resolve_throttle_rounds(crawl_cfg: dict[str, Any], default: int = 3) -> int:
    """连续限流退避次数上限（0=不限）。未显式配置时等于区间条数。"""
    value = crawl_cfg.get("throttle_max_rounds")
    if value is None:
        return max(0, int(default))
    try:
        return max(0, int(value) or 0)
    except (TypeError, ValueError):
        return max(0, int(default))


def truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，不切断多字节字符（Linux 单个文件名上限 255 字节）。"""
    raw = str(text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(text or "")
    out = raw[: max(0, max_bytes)]
    while out:
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            out = out[:-1]
    return ""


def fit_filename(
    prefix: str, title: str, suffix: str = ".pdf", max_bytes: int = 200
) -> str:
    """拼出不超过文件系统上限的文件名：标题超长时按字节截断，保留前后缀。"""
    budget = max_bytes - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    return f"{prefix}{truncate_utf8(title, max(16, budget))}{suffix}"


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """清成合法文件名：非法字符换全角、英文引号按开合换成中文引号、空白压缩。"""
    text = re.sub(r"[\x00-\x1f]+", " ", str(name or "")).strip()
    out: list[str] = []
    opening = True
    for ch in text:
        if ch == '"':
            # 英文双引号在中文标题里通常成对出现：按先后换成 “ ”
            out.append("“" if opening else "”")
            opening = not opening
            continue
        out.append(WIN_CHAR_MAP.get(ch, ch))
    text = re.sub(r"\s+", " ", "".join(out)).strip()
    if len(text) > max_len:
        text = text[:max_len].strip()
    return text or "untitled"


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成唯一文件名: {path}")


def path_exists(path: Path | None) -> bool:
    """exists() 在路径过长（Errno 36）时会抛 OSError，这里统一当成「不存在」。"""
    if path is None:
        return False
    try:
        return Path(path).exists()
    except OSError:
        return False


def _clamp_scale(value: float) -> float:
    """PDF 渲染缩放限制在 Playwright 允许的 0.1~2.0 区间。"""
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(2.0, max(0.1, scale))


class PlaywrightPdfExporter:
    """复用单个 Chromium 实例批量导出 PDF。"""

    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    _LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    # 隐藏常见的自动化特征，降低被人机验证拦截的概率
    _STEALTH_SCRIPT = (
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "window.chrome = window.chrome || {runtime: {}};"
        "Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});"
        "Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});"
    )

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 120_000,
        pdf_scale: float = 1.0,
        browser_channel: str | None = None,
        stealth: bool = True,
        user_agents: list[str] | None = None,
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.pdf_scale = _clamp_scale(pdf_scale)
        self.browser_channel = (browser_channel or "").strip() or None
        self.stealth = stealth
        self.user_agents = [
            ua.strip() for ua in (user_agents or []) if isinstance(ua, str) and ua.strip()
        ]
        self._pw = None
        self._browser: Browser | None = None
        self._context = None
        self._page: Page | None = None
        # 最近一次「文档级」响应的 URL 与状态码（供界面实时显示正在访问什么）
        self.last_url = ""
        self.last_status = 0
        self.prev_url = ""
        self.prev_status = 0

    def _watch_responses(self, page: Page) -> None:
        """记录每个页面最近一次主文档响应的 URL 与 HTTP 状态码。"""

        def _on_response(response) -> None:
            try:
                if response.request.resource_type == "document":
                    # 保留上一条不同 URL，界面可以同时显示"拦截页"和"上一个页面"
                    if response.url != self.last_url:
                        self.prev_url, self.prev_status = self.last_url, self.last_status
                    self.last_url = response.url
                    self.last_status = response.status
            except Exception:
                pass

        try:
            page.on("response", _on_response)
        except Exception:
            pass
        # 最近一次主框架导航的 URL 与 HTTP 状态（供界面实时显示）
        self.last_navigation: dict[str, Any] = {}

    def _start_browser(self, channel: str | None) -> None:
        launch_kwargs = {
            "headless": self.headless,
            "args": list(self._LAUNCH_ARGS),
        }
        if channel:
            launch_kwargs["channel"] = channel
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            user_agent=self._USER_AGENT,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )

    def _launch(self) -> None:
        self._pw = sync_playwright().start()
        last_error: Exception | None = None
        # 优先用配置指定的真实浏览器内核（如 chrome），失败则回退到自带 Chromium
        for channel in ([self.browser_channel] if self.browser_channel else []) + [None]:
            try:
                self._start_browser(channel)
                if channel:
                    logger.info("已使用浏览器内核: %s", channel)
                break
            except Exception as exc:
                last_error = exc
                if channel:
                    logger.warning(
                        "使用 %s 内核启动失败，回退到自带 Chromium: %s", channel, exc
                    )
        else:
            raise RuntimeError(f"浏览器启动失败: {last_error}")
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        self._watch_responses(self._page)
        try:
            self._page.on("response", self._on_response)
        except Exception:
            pass
        if self.stealth:
            try:
                self._context.add_init_script(self._STEALTH_SCRIPT)
            except Exception:
                pass

    def _on_response(self, response) -> None:
        """记录主框架导航请求的 URL 与状态码（用来看现在在访问什么、站点返回什么）。"""
        try:
            if not response.request.is_navigation_request():
                return
            page = self._page
            if page is None or response.frame != page.main_frame:
                return
            self.last_navigation = {
                "url": response.url,
                "status": int(response.status),
            }
        except Exception:
            pass

    def __enter__(self) -> PlaywrightPdfExporter:
        self._launch()
        return self

    def close(self) -> None:
        """关闭浏览器与 Playwright，过程容错（浏览器可能已被站点或系统关闭）。"""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._pw = None
        self._page = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def relaunch(self) -> None:
        """连接断开后重启浏览器。"""
        self.close()
        self._launch()

    @property
    def page(self) -> Page:
        assert self._page is not None
        return self._page

    def new_page(self) -> Page:
        """新建一个标签页（例如用专页导出 PDF，避免主页面离开列表页）。"""
        assert self._context is not None
        page = self._context.new_page()
        page.set_default_timeout(self.timeout_ms)
        self._watch_responses(page)
        return page

    def rotate_user_agent(self, page: Page | None = None) -> str | None:
        """随机换一个 User-Agent（只改请求头，不重建浏览器）；池为空时不动。"""
        if not self.user_agents:
            return None
        target = page if page is not None else self.page
        ua = random.choice(self.user_agents)
        try:
            target.set_extra_http_headers({"User-Agent": ua})
            logger.debug("User-Agent → %s", ua[:60])
        except Exception as exc:
            logger.warning("切换 User-Agent 失败：%s", exc)
            return None
        return ua

    def fetch_page_title(
        self,
        url: str,
        *,
        wait_after_load: float = 1.0,
        wait_until: str = "domcontentloaded",
    ) -> str:
        """打开目标页面并返回其 <title> 文本（用于截断标题回退）。"""
        self.page.goto(url, wait_until=wait_until)
        if wait_after_load > 0:
            time.sleep(wait_after_load)
        return (self.page.title() or "").strip()

    def save_url_as_pdf(
        self,
        url: str,
        pdf_path: Path,
        *,
        wait_after_load: float = 1.0,
        wait_until: str = "domcontentloaded",
        scale: float | None = None,
        page: Page | None = None,
    ) -> Page:
        """把 url 导出为 PDF，返回实际使用的标签页（便于调用方检查是否被限流）。"""
        target = page if page is not None else self.page
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        self.rotate_user_agent(target)  # 每条随机换 UA
        target.goto(url, wait_until=wait_until)
        if wait_after_load > 0:
            time.sleep(wait_after_load)
        target.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            scale=_clamp_scale(self.pdf_scale if scale is None else scale),
        )
        return target

    def save_current_page_as_pdf(self, pdf_path: Path, *, scale: float | None = None) -> Page:
        """把「当前已经打开的页面」直接导出为 PDF（不再重新导航，省一次请求）。"""
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        target = self.page
        target.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            scale=_clamp_scale(self.pdf_scale if scale is None else scale),
        )
        return target
