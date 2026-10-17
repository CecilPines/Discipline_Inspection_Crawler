#!/usr/bin/env python3
"""
纪律法规爬虫主程序：定时爬取、延迟上传、监控 UI 与任务控制。

爬取与上传是两个独立环节，按「先爬取、爬取结束后再上传」的顺序执行：
- 首次运行（状态中无已登记文档）且未加 --no-auto-start 时，先全量爬取，
  爬取结束后立即上传全部文件；
- 之后由每日定时任务增量爬取，并按保留天数（默认 3 天）上传新文件；
- 监控 UI 提供「运行 / 测试」两种模式，可手动触发爬取或上传，测试模式可
  限制每个来源的爬取条数，并选择爬取结束后自动上传或手动上传。
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from crawl_bit_jwjc_regulations import crawl_bit_jwjc_regulations
from crawl_ccdi_regulations import crawl_ccdi_by_id, crawl_ccdi_regulations
from crawl_common import (
    DEFAULT_USER_AGENTS,
    load_config,
    resolve_bit_delay_range,
    resolve_detail_delay_range,
    resolve_delay_range,
    resolve_throttle_rounds,
    resolve_throttle_ranges,
)
from crawler_state import CrawlerState
from monitor_web_ui import run_monitor_server
from ragflow_kb_uploader import get_agent_status, run_scheduled_uploads

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("discipline_crawler")

# 中纪检被跳过的子分类的重试任务 id（由调度器执行）
SKIPPED_RETRY_JOB_ID = "skipped_category_retry"


def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着（跨平台）。"""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _clear_pid_file(path: Path) -> None:
    """退出时删掉自己的 PID 文件（别误删别人的）。"""
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _update_config_section_in_file(path: Path, section: str, updates: dict) -> None:
    """仅更新 config.yaml 中指定顶层段内的键，保留其余内容与注释。"""
    if not path or not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    in_section = False
    done: set[str] = set()
    key_re = re.compile(r"^(\s*)([\w]+):")
    section_re = re.compile(rf"^{re.escape(section)}:\s*$")

    for line in lines:
        if not in_section:
            out.append(line)
            if section_re.match(line):
                in_section = True
            continue

        # 到达下一个顶层键（无缩进、非注释）
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            for key, value in updates.items():
                if key not in done:
                    out.append(f"  {key}: {_yaml_scalar(value)}\n")
                    done.add(key)
            out.append(line)
            in_section = False
            continue

        m = key_re.match(line)
        if m:
            key = m.group(2)
            if key in updates:
                out.append(f"{m.group(1)}{key}: {_yaml_scalar(updates[key])}\n")
                done.add(key)
                continue
        out.append(line)

    if in_section:
        for key, value in updates.items():
            if key not in done:
                out.append(f"  {key}: {_yaml_scalar(value)}\n")
                done.add(key)

    path.write_text("".join(out), encoding="utf-8")


def _update_ragflow_in_config(path: Path, updates: dict) -> None:
    _update_config_section_in_file(path, "ragflow", updates)


def run_crawl_jobs(
    state: CrawlerState,
    config: dict,
    source: str = "all",
    *,
    max_items: int | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    parallel: bool = False,
    categories: list[str] | None = None,
) -> None:
    """执行爬取环节。max_items 用于测试模式：每个来源最多爬取该数量。
    parallel=True 且同时爬取两个来源时，中纪检与校纪检并行执行。
    categories 仅用于中纪检：只爬取这些导航码（重试被跳过的子分类）。
    """
    crawl_cfg = config.get("crawl", {})
    output_dir = Path(config["output_dir"])
    headless = bool(crawl_cfg.get("playwright_headless", True))
    timeout_ms = int(crawl_cfg.get("page_load_timeout_ms", 120_000))
    # 每条之间的随机间隔区间（秒）：两个站点各自独立配置
    delay_range = resolve_delay_range(crawl_cfg)
    bit_delay_range = resolve_bit_delay_range(crawl_cfg)
    detail_delay_range = resolve_detail_delay_range(crawl_cfg)
    # 中纪检遇到站点限流页时逐轮的随机等待区间（秒）：默认 10~15 / 2~3 / 1~2 分钟
    throttle_ranges = resolve_throttle_ranges(crawl_cfg)
    throttle_rounds = resolve_throttle_rounds(crawl_cfg, default=len(throttle_ranges))
    # 中断后重启可续爬的时间窗（小时）；0 = 每次都从第一个分类全量扫描
    try:
        resume_max_age_hours = float(crawl_cfg.get("resume_max_age_hours", 12) or 0)
    except (TypeError, ValueError):
        resume_max_age_hours = 12.0
    verification_wait = int(crawl_cfg.get("human_verification_wait_seconds", 300))
    scale_ccdi = float(crawl_cfg.get("pdf_scale_ccdi", 1.0))
    scale_bit = float(crawl_cfg.get("pdf_scale_bit", 0.9))
    browser_channel = crawl_cfg.get("browser_channel") or None
    stealth = bool(crawl_cfg.get("stealth", True))
    # User-Agent 池：默认开启，每条详情页 / 每个分类随机换一次
    rotate_ua = bool(crawl_cfg.get("rotate_user_agent", True))
    configured_uas = crawl_cfg.get("user_agents")
    user_agents = (
        [ua for ua in configured_uas if isinstance(ua, str) and ua.strip()]
        if isinstance(configured_uas, list) and configured_uas
        else list(DEFAULT_USER_AGENTS)
    )
    if not rotate_ua:
        user_agents = []

    def run_ccdi() -> None:
        logger.info("开始中纪检爬取")
        mode = str(crawl_cfg.get("crawl_mode") or "id_walk").strip().lower()
        first_run_now = state.is_first_run()
        try:
            ccdi_new = _run_ccdi_impl(mode, first_run_now)
        except Exception as exc:
            # 异常退出时把状态改回空闲，否则界面会一直显示"运行中"而控制按钮已经失效
            logger.exception("中纪检爬取异常")
            reason = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
            state.set_progress(
                "ccdi",
                status="error",
                message=f"异常中断：{reason}",
            )
            state.set_alert(
                "crawl_error_ccdi",
                f"中纪检爬取异常中断：{reason}",
                level="error",
            )
            raise
        logger.info("中纪检完成，新增 %s 个", ccdi_new)

    def _run_ccdi_impl(mode: str, first_run_now: bool) -> int:
        if mode == "id_walk" and not categories and not max_items:
            # 按详情页编号枚举：不依赖类目翻页，规避翻页触发的拦截
            try:
                id_walk_max = int(crawl_cfg.get("id_walk_max", 71069) or 71069)
            except (TypeError, ValueError):
                id_walk_max = 71069
            try:
                daily_span = int(crawl_cfg.get("id_walk_daily_span", 400) or 400)
            except (TypeError, ValueError):
                daily_span = 400
            try:
                streak_stop = int(
                    crawl_cfg.get("id_walk_invalid_streak_stop", 0) or 0
                )
            except (TypeError, ValueError):
                streak_stop = 0
            return crawl_ccdi_by_id(
                output_dir,
                state,
                headless=headless,
                timeout_ms=timeout_ms,
                request_delay_seconds=delay_range,
                detail_page_delay_seconds=detail_delay_range,
                max_id=id_walk_max,
                daily_span=daily_span,
                invalid_streak_stop=streak_stop,
                stop_event=stop_event,
                pause_event=pause_event,
                pdf_scale=scale_ccdi,
                browser_channel=browser_channel,
                stealth=stealth,
                user_agents=user_agents,
                first_run=first_run_now,
                throttle_wait_ranges=throttle_ranges,
                throttle_max_rounds=throttle_rounds,
            )
        # 类目翻页方式（测试模式的限量爬取、跳过分类的重试都走这里）
        return crawl_ccdi_regulations(
            output_dir,
            state,
            headless=headless,
            timeout_ms=timeout_ms,
            request_delay_seconds=delay_range,
            max_items=max_items,
            stop_event=stop_event,
            pause_event=pause_event,
            verification_wait_seconds=verification_wait,
            pdf_scale=scale_ccdi,
            browser_channel=browser_channel,
            stealth=stealth,
            categories=categories,
            throttle_wait_ranges=throttle_ranges,
            throttle_max_rounds=throttle_rounds,
            resume_max_age_hours=resume_max_age_hours,
            user_agents=user_agents,
            detail_page_delay_seconds=detail_delay_range,
            # 首次运行（含中断后继续）：跳过已完成的子分类，并记录各分类条数基线
            first_run=first_run_now,
        )

    def run_bit() -> None:
        logger.info("开始校纪检爬取")
        try:
            bit_new = crawl_bit_jwjc_regulations(
                output_dir,
                state,
                headless=headless,
                timeout_ms=timeout_ms,
                request_delay_seconds=bit_delay_range,
                max_items=max_items,
                stop_event=stop_event,
                pause_event=pause_event,
                pdf_scale=scale_bit,
                browser_channel=browser_channel,
                stealth=stealth,
            )
        except Exception as exc:
            # 与中纪检一致：异常退出时把卡片状态改回空闲，避免"卡片在转、按钮已失效"
            logger.exception("校纪检爬取异常")
            reason = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
            state.set_progress(
                "bit",
                status="error",
                message=f"异常中断：{reason}",
            )
            state.set_alert(
                "crawl_error_bit",
                f"校纪检爬取异常中断：{reason}",
                level="error",
            )
            raise
        logger.info("校纪检完成，新增 %s 个", bit_new)

    want_ccdi = source in ("all", "ccdi")
    want_bit = source in ("all", "bit")

    if parallel and want_ccdi and want_bit:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                "中纪检": pool.submit(run_ccdi),
                "校纪检": pool.submit(run_bit),
            }
            for name, future in futures.items():
                try:
                    future.result()
                except Exception:
                    logger.exception("%s 爬取失败", name)
        return

    if want_ccdi:
        run_ccdi()
    if want_bit:
        run_bit()


def run_upload_jobs(
    state: CrawlerState,
    config: dict,
    *,
    immediate: bool = False,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> None:
    """执行上传环节。immediate=True 时无视保留天数，上传全部待上传文件。"""
    logger.info("开始上传待处理 PDF")
    result = run_scheduled_uploads(
        state,
        config,
        immediate=immediate,
        stop_event=stop_event,
        pause_event=pause_event,
    )
    logger.info("上传结果: %s", result)


class JobController:
    """爬取/上传任务编排：两个环节分开、顺序执行，供 UI 与定时任务共用。"""

    def __init__(self, state: CrawlerState, config: dict):
        self.state = state
        self.config = config
        self.config_path: Path | None = None
        self.output_dir = Path(config["output_dir"]).resolve()
        self._crawl_running = False
        self._upload_running = False
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self.mode = "run"  # run | test
        self.test_count = 5
        self.test_source = "all"
        self.full_test = False
        self.test_schedule_mode = "interval"  # interval | daily
        # 上传方式：auto=爬取结束后按策略自动上传（默认，可用 config.yaml 的 upload.mode 调整）
        self.upload_mode = self._normalize_upload_mode(
            (config.get("upload", {}) or {}).get("mode", "auto")
        )
        self.parallel = bool(config.get("crawl", {}).get("parallel_sources", True))
        # 测试模式的间隔调度（分钟，0=关闭）
        self.crawl_interval_minutes = 0
        self.upload_interval_minutes = 0
        self._next_crawl_at: datetime | None = None
        self._next_upload_at: datetime | None = None
        self._interval_stop = threading.Event()
        self._interval_thread: threading.Thread | None = None
        self._scheduler: BackgroundScheduler | None = None
        # 中纪检跳过分类的自动重试：已重试轮数（每次完整爬取后归零）
        self._retry_round = 0

    def get_settings(self) -> dict:
        crawl_cfg = self.config.get("crawl", {}) or {}
        return {
            "mode": self.mode,
            "test_count": self.test_count,
            "test_source": self.test_source,
            "full_test": self.full_test,
            "test_schedule_mode": self.test_schedule_mode,
            "first_run_pending": self.state.is_first_run(),
            "upload_mode": self.upload_mode,
            "parallel": self.parallel,
            "crawl_interval_minutes": self.crawl_interval_minutes,
            "upload_interval_minutes": self.upload_interval_minutes,
            "scheduler_enabled": self._scheduler is not None,
            "next_crawl_at": self._next_crawl_at.isoformat() if self._next_crawl_at else None,
            "next_upload_at": self._next_upload_at.isoformat() if self._next_upload_at else None,
            "output_dir": str(self.output_dir),
            "pdf_scale_bit": float(crawl_cfg.get("pdf_scale_bit", 0.9)),
            "config_info": self._config_info(),
            "crawl_time": (self.config.get("schedule", {}) or {}).get("crawl_time", ""),
            "upload_time": (self.config.get("schedule", {}) or {}).get("upload_time", ""),
            "next_daily_crawl_at": self._next_run_at("daily_crawl"),
            "next_daily_upload_at": self._next_run_at("daily_upload"),
            "playwright_headless": bool(crawl_cfg.get("playwright_headless", True)),
            "crawl_running": self._crawl_running,
            "upload_running": self._upload_running,
            "paused": self._pause_event.is_set(),
        }

    def set_settings(
        self,
        *,
        mode: str | None = None,
        test_count: int | None = None,
        test_source: str | None = None,
        full_test: bool | None = None,
        test_schedule_mode: str | None = None,
        upload_mode: str | None = None,
        parallel: bool | None = None,
        crawl_interval_minutes: int | None = None,
        upload_interval_minutes: int | None = None,
        crawl_time: str | None = None,
        upload_time: str | None = None,
        pdf_scale_bit: float | None = None,
    ) -> dict:
        if mode is not None:
            if mode not in ("run", "test"):
                raise ValueError("mode 必须为 run 或 test")
            self.mode = mode
        if upload_mode is not None:
            if upload_mode not in ("auto", "manual"):
                raise ValueError("upload_mode 必须为 auto 或 manual")
            self.upload_mode = upload_mode
            self._update_upload_config({"mode": upload_mode})
        if test_count is not None:
            try:
                n = int(test_count)
            except (TypeError, ValueError):
                raise ValueError("测试条数必须为整数")
            if n < 1:
                raise ValueError("测试条数必须大于 0")
            self.test_count = n
        if parallel is not None:
            self.parallel = bool(parallel)
        if full_test is not None:
            self.full_test = bool(full_test)
        if test_schedule_mode is not None:
            if test_schedule_mode not in ("interval", "daily"):
                raise ValueError("test_schedule_mode 必须为 interval 或 daily")
            self.test_schedule_mode = test_schedule_mode
        if crawl_time is not None or upload_time is not None:
            self._update_schedule_times(crawl_time=crawl_time, upload_time=upload_time)
        if test_source is not None:
            if test_source not in ("all", "ccdi", "bit"):
                raise ValueError("test_source 必须为 all/ccdi/bit")
            self.test_source = test_source
        if crawl_interval_minutes is not None:
            self.crawl_interval_minutes = self._parse_interval(crawl_interval_minutes)
            self._next_crawl_at = self._compute_next(self.crawl_interval_minutes)
        if upload_interval_minutes is not None:
            self.upload_interval_minutes = self._parse_interval(upload_interval_minutes)
            self._next_upload_at = self._compute_next(self.upload_interval_minutes)
        if pdf_scale_bit is not None:
            try:
                scale = float(pdf_scale_bit)
            except (TypeError, ValueError):
                raise ValueError("缩放必须是数字")
            if not (0.1 <= scale <= 2.0):
                raise ValueError("缩放需在 0.1 ~ 2.0 之间")
            self._update_crawl_config({"pdf_scale_bit": round(scale, 2)})
        return self.get_settings()

    def _update_crawl_config(self, updates: dict) -> None:
        """修改 crawl 段配置并写回 config.yaml。"""
        crawl_cfg = self.config.setdefault("crawl", {})
        crawl_cfg.update(updates)
        if self.config_path and updates:
            _update_config_section_in_file(self.config_path, "crawl", updates)

    def _update_upload_config(self, updates: dict) -> None:
        """修改 upload 段配置并写回 config.yaml。"""
        up_cfg = self.config.setdefault("upload", {})
        up_cfg.update(updates)
        if self.config_path and updates:
            _update_config_section_in_file(self.config_path, "upload", updates)

    @staticmethod
    def _normalize_upload_mode(value) -> str:
        """upload.mode 取值归一化：manual=手动，其余（含缺省）=auto。"""
        return "manual" if str(value or "").strip().lower() == "manual" else "auto"

    def _config_info(self) -> dict:
        """供 UI 只读展示的关键配置项（不含密钥）。"""
        crawl_cfg = self.config.get("crawl", {}) or {}
        rag = self.config.get("ragflow", {}) or {}
        sched = self.config.get("schedule", {}) or {}
        up = self.config.get("upload", {}) or {}
        return {
            "output_dir": str(self.output_dir),
            "crawl.playwright_headless": crawl_cfg.get("playwright_headless", True),
            "crawl.browser_channel": crawl_cfg.get("browser_channel") or "（自带 Chromium）",
            "crawl.stealth": crawl_cfg.get("stealth", True),
            "crawl.rotate_user_agent": bool(crawl_cfg.get("rotate_user_agent", True)),
            "crawl.parallel_sources": crawl_cfg.get("parallel_sources", True),
            "crawl.crawl_mode": crawl_cfg.get("crawl_mode", "id_walk"),
            "crawl.id_walk_max": crawl_cfg.get("id_walk_max", 71069),
            "crawl.id_walk_daily_span": crawl_cfg.get("id_walk_daily_span", 400),
            "crawl.id_walk_invalid_streak_stop": crawl_cfg.get(
                "id_walk_invalid_streak_stop", 0
            ),
            "crawl.pdf_scale_ccdi": crawl_cfg.get("pdf_scale_ccdi", 1.0),
            "crawl.pdf_scale_bit": crawl_cfg.get("pdf_scale_bit", 0.9),
            "crawl.request_delay": self._delay_text(resolve_delay_range(crawl_cfg)),
            "crawl.detail_page_delay": self._delay_text(
                resolve_detail_delay_range(crawl_cfg)
            ),
            "crawl.bit_request_delay": self._delay_text(
                resolve_bit_delay_range(crawl_cfg)
            ),
            "crawl.throttle_wait": self._throttle_text(
                resolve_throttle_ranges(crawl_cfg)
            ),
            "crawl.throttle_max_rounds": resolve_throttle_rounds(
                crawl_cfg, default=len(resolve_throttle_ranges(crawl_cfg))
            ),
            "crawl.resume_max_age_hours": crawl_cfg.get("resume_max_age_hours", 12),
            "crawl.page_load_timeout_ms": crawl_cfg.get("page_load_timeout_ms", 120_000),
            "crawl.human_verification_wait_seconds": crawl_cfg.get(
                "human_verification_wait_seconds", 300
            ),
            "crawl.skipped_category_retry_minutes": crawl_cfg.get(
                "skipped_category_retry_minutes", 30
            ),
            "crawl.skipped_category_retry_max_rounds": crawl_cfg.get(
                "skipped_category_retry_max_rounds", 3
            ),
            "schedule.timezone": sched.get("timezone") or "（服务器本地时区）",
            "upload.mode": up.get("mode", "auto"),
            "upload.min_age_days": up.get("min_age_days", 3),
            "ragflow.base_url": rag.get("base_url", ""),
            "ragflow.chunk_method": rag.get("chunk_method", "laws"),
            "ragflow.dataset_name_ccdi": rag.get("dataset_name_ccdi", "中纪检"),
            "ragflow.dataset_name_bit": rag.get("dataset_name_bit", "校纪检"),
        }

    @staticmethod
    def _delay_text(seconds_range: tuple[float, float]) -> str:
        """把随机区间显示成「1~3 秒」这样的文本（区间相同则显示固定值）。"""
        low, high = seconds_range
        if high >= 60:
            if high <= low:
                return f"{low / 60:.1f} 分钟"
            return f"{low / 60:.1f}~{high / 60:.1f} 分钟（随机）"
        if high <= low:
            return f"{low:g} 秒"
        return f"{low:g}~{high:g} 秒（随机）"

    @classmethod
    def _throttle_text(cls, ranges: list[tuple[float, float]]) -> str:
        """限流退避的逐轮区间显示，如「第1次 10~15 分钟 / 第2次 2~3 分钟」。"""
        parts = []
        for index, rng in enumerate(ranges, start=1):
            low, high = rng
            if high <= low:
                parts.append(f"第{index}次 {low / 60:.0f} 分钟")
            else:
                parts.append(f"第{index}次 {low / 60:.0f}~{high / 60:.0f} 分钟")
        return " / ".join(parts)

    @staticmethod
    def _parse_interval(value) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise ValueError("间隔必须是整数分钟")
        if minutes < 0:
            raise ValueError("间隔不能为负数")
        return minutes

    @staticmethod
    def _compute_next(minutes: int) -> datetime | None:
        if minutes <= 0:
            return None
        return datetime.now() + timedelta(minutes=minutes)

    @staticmethod
    def _normalize_hhmm(value: str) -> str:
        """校验 HH:MM；空字符串表示不启用该定时任务。"""
        text = (value or "").strip()
        if not text:
            return ""
        try:
            hour, minute = _parse_hhmm(text)
        except Exception:
            raise ValueError("时间格式必须是 HH:MM")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("时间必须在 00:00 ~ 23:59 之间")
        return f"{hour:02d}:{minute:02d}"

    def _update_schedule_times(
        self, *, crawl_time: str | None = None, upload_time: str | None = None
    ) -> None:
        sched_cfg = self.config.setdefault("schedule", {})
        changed: dict[str, str] = {}
        if crawl_time is not None:
            sched_cfg["crawl_time"] = self._normalize_hhmm(crawl_time)
            changed["crawl_time"] = sched_cfg["crawl_time"]
        if upload_time is not None:
            sched_cfg["upload_time"] = self._normalize_hhmm(upload_time)
            changed["upload_time"] = sched_cfg["upload_time"]
        if changed and self.config_path:
            _update_config_section_in_file(self.config_path, "schedule", changed)
        self.apply_schedule()

    def _max_items_for_mode(self) -> int | None:
        """测试模式取设置的条数（全量测试时不限）；运行模式不限。"""
        if self.mode == "test" and not self.full_test:
            return self.test_count
        return None

    def _upload_schedule_configured(self) -> bool:
        return bool((self.config.get("schedule", {}) or {}).get("upload_time"))

    def _upload_time_passed_today(self) -> bool:
        """当天设定的上传时间点是否已经过去（按 schedule.timezone 判断）。"""
        sched_cfg = self.config.get("schedule", {}) or {}
        value = (sched_cfg.get("upload_time") or "").strip()
        if not value:
            return True
        try:
            hour, minute = _parse_hhmm(value)
        except Exception:
            return True
        now = datetime.now()
        tz_name = sched_cfg.get("timezone")
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                now = datetime.now(ZoneInfo(str(tz_name)))
            except Exception:
                pass
        return (now.hour, now.minute) >= (hour, minute)

    def mark_first_run(self, value: bool = True) -> None:
        """标记/取消“首次运行”，界面会据此显示提示。"""
        self.state.set_first_run(value)

    def recover_stale_progress(self) -> None:
        """进程启动时：把上次被强杀（Ctrl+C / kill）遗留的「运行中」状态改回空闲。

        否则界面会一直停在中断那一刻，看起来像"还在爬"。
        """
        snap = self.state.snapshot()
        names: list[str] = []
        for source, label in (("ccdi", "中纪检"), ("bit", "校纪检"), ("upload", "上传")):
            prog = (snap.get("progress", {}) or {}).get(source) or {}
            if prog.get("status") == "running":
                self.state.set_progress(
                    source,
                    status="idle",
                    message=f"{label}上次运行被中断（进程已退出），可点「开始」继续",
                )
                names.append(label)
        cats = self.state.get_category_status("ccdi")
        updates = {
            code: {"status": "interrupted", "reason": ""}
            for code, entry in cats.items()
            if entry.get("status") == "running"
        }
        if updates:
            self.state.update_category_statuses("ccdi", updates)
        if names or updates:
            logger.info(
                "已把上次中断遗留的运行状态改回空闲：%s%s",
                "、".join(names) or "—",
                f"，并把 {len(updates)} 个子分类标为已中断" if updates else "",
            )

    def _upload_pass(self, stop_event: threading.Event | None = None) -> None:
        """执行一次“立即上传”（跳过已上传的），供首次运行的批量上传循环调用。"""
        if self._upload_running:
            return
        self._upload_running = True
        try:
            self._upload_blocking(True, stop_event, None)
        finally:
            self._upload_running = False

    def start_first_run_uploader(self, interval_seconds: float = 15.0) -> threading.Event:
        """首次采集时并行批量上传：爬取期间定期把已落盘的新文件推走，爬取结束后收尾。

        返回一个事件，调用方在爬取结束时 set 它即可让该线程收尾退出。
        """
        done = threading.Event()

        def worker() -> None:
            while not done.wait(interval_seconds):
                try:
                    self._upload_pass(self._stop_event)
                except Exception:
                    logger.exception("首次运行期间的批量上传失败，稍后重试")
            try:
                self._upload_pass(self._stop_event)
            except Exception:
                logger.exception("首次运行的收尾上传失败")
            logger.info("首次运行的并行批量上传已结束")

        threading.Thread(target=worker, daemon=True, name="first-run-upload").start()
        logger.info("首次运行：已启动并行批量上传（每 %s 秒一批）", interval_seconds)
        return done

    def scheduled_crawl(self) -> None:
        """定时任务触发的爬取：按当前模式取参数，并取消“首次运行”标记。

        若此刻已有任务在跑（例如首次全量爬取还没结束），本次时间点**直接跳过、
        不排队也不补跑**，等下一个定时点；「首次执行」标记也保持不变。
        """
        if self._crawl_running or self._upload_running:
            logger.warning("定时爬取跳过：已有任务运行中（这个时间点不再补跑，等下一天）")
            return
        source = self.test_source if self.mode == "test" else "all"
        self.crawl_sync(source=source, max_items=self._max_items_for_mode())

    def scheduled_upload(self) -> None:
        """定时任务触发的上传：测试模式立即推送全部待上传，运行模式按保留天数。

        同样地，已有任务在跑时本次时间点直接跳过，不排队也不补跑。
        """
        if self._crawl_running or self._upload_running:
            logger.warning("定时上传跳过：已有任务运行中（这个时间点不再补跑）")
            return
        if self.mode == "test":
            self.upload_now_sync()
        else:
            self.upload_sync()

    def _next_run_at(self, job_id: str) -> str | None:
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job(job_id)
        nxt = getattr(job, "next_run_time", None) if job else None
        return nxt.isoformat() if nxt else None

    def attach_scheduler(self, scheduler: BackgroundScheduler) -> None:
        self._scheduler = scheduler
        self.apply_schedule()
        # 首次全量爬取是在定时器启动之前跑的，那时没法安排重试；
        # 这里补上，保证那批被跳过的分类也能按间隔自动重试
        self.schedule_skipped_retry()

    def apply_schedule(self) -> None:
        """按当前配置重建每日定时任务；时间为空表示停用该任务。"""
        if self._scheduler is None:
            return
        sched_cfg = self.config.setdefault("schedule", {})
        for job_id, key, func in (
            ("daily_crawl", "crawl_time", self.scheduled_crawl),
            ("daily_upload", "upload_time", self.scheduled_upload),
        ):
            value = (sched_cfg.get(key) or "").strip()
            if not value:
                if self._scheduler.get_job(job_id):
                    self._scheduler.remove_job(job_id)
                    logger.info("已停用定时任务 %s（%s 为空）", job_id, key)
                continue
            try:
                hour, minute = _parse_hhmm(value)
            except Exception:
                logger.warning("定时时间格式不正确：%s=%s", key, value)
                continue
            self._scheduler.add_job(
                func,
                CronTrigger(hour=hour, minute=minute),
                id=job_id,
                replace_existing=True,
            )
            logger.info("定时任务 %s 已设为每天 %s", job_id, value)

    def set_output_dir(self, value: str) -> dict:
        """切换 PDF 输出目录，并加载新目录下的状态文件。"""
        if self._crawl_running or self._upload_running:
            raise RuntimeError("已有任务运行中，无法修改保存位置")
        value = (value or "").strip()
        if not value:
            raise ValueError("保存位置不能为空")
        new_dir = Path(value).expanduser().resolve()
        new_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = new_dir
        self.config["output_dir"] = str(new_dir)
        self.state = CrawlerState(new_dir / "crawler_state.json")
        return self.get_settings()

    def _crawl_blocking(
        self,
        source: str,
        max_items: int | None,
        stop_event: threading.Event | None,
        pause_event: threading.Event | None = None,
        parallel: bool = False,
        categories: list[str] | None = None,
    ) -> None:
        run_crawl_jobs(
            self.state,
            self.config,
            source=source,
            max_items=max_items,
            stop_event=stop_event,
            pause_event=pause_event,
            parallel=parallel,
            categories=categories,
        )

    def _upload_blocking(
        self,
        immediate: bool,
        stop_event: threading.Event | None,
        pause_event: threading.Event | None = None,
    ) -> None:
        run_upload_jobs(
            self.state,
            self.config,
            immediate=immediate,
            stop_event=stop_event,
            pause_event=pause_event,
        )

    def crawl_sync(self, source: str = "all", max_items: int | None = None) -> None:
        """同步执行爬取（定时任务 / 首次运行 / 命令行用）。"""
        if self._crawl_running or self._upload_running:
            logger.warning("爬取跳过：已有任务运行中")
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self._crawl_running = True
        try:
            self._crawl_blocking(
                source, max_items, self._stop_event, self._pause_event, self.parallel
            )
        finally:
            self._crawl_running = False
        self._after_crawl(source)

    def _after_crawl(self, source: str) -> None:
        """一轮爬取结束后的收尾：安排跳过分类重试、判断首次运行是否完成。"""
        self._retry_round = 0
        self.schedule_skipped_retry()
        if not self.state.is_first_run():
            return
        need = {"ccdi", "bit"} if source == "all" else {source}
        if not all(self.state.is_pass_done(s) for s in need):
            return
        # 中纪检按编号方式：必须真的扫到编号上限，才算首次全量跑完。
        # （pass_done 可能是旧版本留下的，不能单独作为依据。）
        if self._ccdi_id_walk_pending():
            logger.info(
                "首次运行还没结束：中纪检编号尚未扫到上限，保留「首次执行」标记"
            )
            return
        self.state.set_first_run(False)
        self.state.reset_pass_done()
        logger.info("首次运行已完整结束（%s 都跑完了），已清除「首次执行」标记", "、".join(sorted(need)))

    def _ccdi_id_walk_pending(self) -> bool:
        """按编号爬取时，编号还没扫到上限就返回 True。"""
        cfg = self.config.get("crawl", {}) or {}
        if str(cfg.get("crawl_mode") or "id_walk").strip().lower() != "id_walk":
            return False
        try:
            ceiling = int(cfg.get("id_walk_max", 71069) or 71069)
        except (TypeError, ValueError):
            ceiling = 71069
        try:
            last_id = int(
                (self.state.get_id_walk_position("ccdi") or {}).get("last_id") or 0
            )
        except (TypeError, ValueError):
            last_id = 0
        return last_id < ceiling

    def upload_sync(self) -> None:
        """同步执行定时上传（按保留天数）。"""
        if self._upload_running or self._crawl_running:
            logger.warning("上传跳过：已有任务运行中")
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self._upload_running = True
        try:
            self._upload_blocking(False, self._stop_event, self._pause_event)
        finally:
            self._upload_running = False

    def upload_now_sync(self) -> None:
        """同步执行立即上传（首次运行 / 命令行用）。"""
        if self._upload_running or self._crawl_running:
            logger.warning("立即上传跳过：已有任务运行中")
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self._upload_running = True
        try:
            self._upload_blocking(True, self._stop_event, self._pause_event)
        finally:
            self._upload_running = False

    def _run_crawl_with_upload(self, source: str, *, async_upload: bool) -> None:
        """执行一次爬取，并按上传策略处理后续上传（UI 与命令行 --crawl-now 共用）。"""
        max_items = self._max_items_for_mode()
        # 点下「开始」立刻给卡片一个反馈：浏览器启动要几秒，不能看起来像没反应
        starting = []
        if source in ("all", "ccdi"):
            starting.append("ccdi")
        if source in ("all", "bit") and (source == "bit" or self.parallel):
            starting.append("bit")
        for item in starting:
            prog = (self.state.snapshot().get("progress") or {}).get(item) or {}
            # 新一轮开始：收起上一次的「异常中断」横幅（卡片会变成运行中）
            self.state.clear_alert(f"crawl_error_{item}")
            if prog.get("status") != "running":
                self.state.set_progress(
                    item,
                    status="running",
                    message="已启动，正在初始化浏览器…",
                    extra={"pending": False},
                )
        # 首次运行判定：以「首次执行」标记为准（中途重启后标记仍在），
        # 不能只看目录里有没有文件——否则续跑时会退回定时上传，边爬边传就失效了。
        first_time = self.state.is_first_run() or not bool(
            self.state.snapshot().get("documents")
        )
        self._stop_event.clear()
        self._pause_event.clear()
        self._crawl_running = True
        # 首次采集：一边爬一边批量上传，避免上传环节空等
        batch_done = None
        if first_time and self.upload_mode == "auto":
            batch_done = self.start_first_run_uploader()
        try:
            self._crawl_blocking(
                source, max_items, self._stop_event, self._pause_event, self.parallel
            )
        except Exception:
            logger.exception("爬取失败")
        finally:
            self._crawl_running = False
            if batch_done is not None:
                batch_done.set()
        # 本次爬取若跳过了中纪检子分类，按配置的间隔安排自动重试
        self._after_crawl(source)
        if batch_done is not None:
            logger.info("首次采集：并行批量上传已接管，爬取结束后由上传线程收尾")
            return
        if self._stop_event.is_set():
            logger.info("爬取已终止，跳过自动上传")
            return
        if self.upload_mode != "auto":
            return
        if not first_time and self._upload_schedule_configured():
            upload_at = self.config.get("schedule", {}).get("upload_time")
            if not self._upload_time_passed_today():
                logger.info(
                    "自动上传：本次等待设定的上传时间点（%s）由定时任务执行", upload_at
                )
                return
            logger.info(
                "自动上传：今天的上传时间点（%s）已过，本次爬取结束后立即上传", upload_at
            )
        if async_upload:
            try:
                self.start_upload()
            except RuntimeError:
                logger.warning("自动上传未启动：已有任务运行中")
        else:
            self.upload_now_sync()

    def crawl_now_sync(self, source: str = "all") -> None:
        """命令行 --crawl-now：同步跑一次，行为与界面点「开始」一致。"""
        if self._crawl_running or self._upload_running:
            logger.warning("爬取跳过：已有任务运行中")
            return
        self._run_crawl_with_upload(source, async_upload=False)

    def start_crawl(self, source: str = "all") -> dict:
        """UI 触发：异步执行爬取；结束后按设置决定是否自动上传。"""
        if self._crawl_running or self._upload_running:
            raise RuntimeError("已有任务运行中")
        max_items = self._max_items_for_mode()
        # 线程真正开始前就把标志置上：否则界面在启动的瞬间仍认为空闲，
        # 连续点两下会起两个任务，停止按钮也会短暂不可用。
        self._stop_event.clear()
        self._pause_event.clear()
        self._crawl_running = True

        def worker() -> None:
            try:
                self._run_crawl_with_upload(source, async_upload=True)
            finally:
                self._crawl_running = False

        threading.Thread(target=worker, daemon=True, name="ui-crawl").start()
        return {
            "started": True,
            "mode": self.mode,
            "source": source,
            "max_items": max_items,
        }

    def start_upload(self) -> dict:
        """UI 触发：异步立即上传全部待上传文件。"""
        if self._upload_running or self._crawl_running:
            raise RuntimeError("已有任务运行中")
        self._stop_event.clear()
        self._pause_event.clear()
        self._upload_running = True

        def worker() -> None:
            try:
                self._upload_blocking(True, self._stop_event, self._pause_event)
            except Exception:
                logger.exception("UI 触发的上传失败")
            finally:
                self._upload_running = False

        threading.Thread(target=worker, daemon=True, name="ui-upload").start()
        return {"started": True}

    def stop_all(self) -> dict:
        """终止所有任务（爬取 / 上传，含自动上传），并取消待执行的中纪检跳过分类重试。"""
        self.cancel_skipped_retry(reason="已终止所有任务")
        if not (self._crawl_running or self._upload_running):
            self._pause_event.clear()
            # 状态文件仍显示运行中，说明任务在别的进程里（例如终端单次任务），
            # 本界面改不了它的停止标志，给出明确提示而不是一句"没有任务"。
            self.state.reload()
            snap = self.state.snapshot()
            external = [
                label
                for src, label in (("ccdi", "中纪检"), ("bit", "校纪检"), ("upload", "上传"))
                if ((snap.get("progress") or {}).get(src) or {}).get("status") == "running"
            ]
            if external:
                return {
                    "stopped": False,
                    "message": "、".join(external)
                    + "在状态文件里仍是「运行中」，但本界面没有对应的任务"
                    "（可能由终端/其它进程启动，或上次异常退出残留）；"
                    "如在终端运行请按 Ctrl+C，否则重新启动本程序即可复位",
                }
            return {"stopped": False, "message": "当前没有运行中的任务"}
        self._pause_event.clear()  # 先解除暂停，让循环尽快执行终止检查
        self._stop_event.set()
        return {"stopped": True, "message": "已终止所有任务"}

    def pause(self) -> dict:
        """暂停当前运行中的任务。"""
        if not (self._crawl_running or self._upload_running):
            return {"paused": False, "message": "当前没有运行中的任务"}
        self._pause_event.set()
        return {"paused": True, "message": "任务已暂停"}

    def resume(self) -> dict:
        """继续已暂停的任务。"""
        if not (self._crawl_running or self._upload_running):
            return {"paused": False, "message": "当前没有运行中的任务"}
        self._pause_event.clear()
        return {"paused": False, "message": "任务已继续"}

    # ------------------------------------------------------------------
    # 中纪检被跳过的子分类：界面标识 + 间隔重试
    # ------------------------------------------------------------------
    def _skipped_retry_minutes(self) -> int:
        cfg = self.config.get("crawl", {}) or {}
        try:
            return max(0, int(cfg.get("skipped_category_retry_minutes", 30) or 0))
        except (TypeError, ValueError):
            return 30

    def _skipped_retry_max_rounds(self) -> int:
        """同一批跳过分类最多自动重试几轮（0=不限轮数）。"""
        cfg = self.config.get("crawl", {}) or {}
        try:
            return max(0, int(cfg.get("skipped_category_retry_max_rounds", 3) or 0))
        except (TypeError, ValueError):
            return 3

    def skipped_categories(self) -> list[dict]:
        """当前被跳过的中纪检子分类（供界面展示）。"""
        return self.state.get_skipped_categories("ccdi")

    def next_skipped_retry_at(self) -> str | None:
        """下一次自动重试的时间（未安排则为 None）。"""
        return self._next_run_at(SKIPPED_RETRY_JOB_ID)

    def cancel_skipped_retry(self, *, reason: str = "") -> None:
        """取消待执行的跳过分类重试。"""
        if self._scheduler is None:
            return
        if self._scheduler.get_job(SKIPPED_RETRY_JOB_ID):
            self._scheduler.remove_job(SKIPPED_RETRY_JOB_ID)
            self._retry_round = 0
            logger.info("已取消待执行的中纪检跳过分类重试%s", f"（{reason}）" if reason else "")

    def schedule_skipped_retry(self) -> None:
        """按配置间隔安排一次“只重试被跳过的中纪检子分类”的任务。"""
        pending = self.state.get_skipped_categories("ccdi", retryable_only=True)
        if not pending:
            self.cancel_skipped_retry()
            return
        if self._scheduler is None:
            logger.info("未启用定时器，跳过的 %s 个分类不会自动重试", len(pending))
            return
        minutes = self._skipped_retry_minutes()
        if minutes <= 0:
            logger.info(
                "已有 %s 个中纪检子分类被跳过；自动重试已关闭"
                "（crawl.skipped_category_retry_minutes=0），将在下次爬取时重试",
                len(pending),
            )
            self.cancel_skipped_retry()
            return
        max_rounds = self._skipped_retry_max_rounds()
        if max_rounds and self._retry_round >= max_rounds:
            logger.warning(
                "跳过分类已自动重试 %s 轮仍未完成，停止重试（下次爬取会再次尝试）",
                self._retry_round,
            )
            self.cancel_skipped_retry()
            return
        run_at = datetime.now() + timedelta(minutes=minutes)
        self._scheduler.add_job(
            self.retry_skipped_categories,
            DateTrigger(run_date=run_at),
            id=SKIPPED_RETRY_JOB_ID,
            replace_existing=True,
        )
        logger.info(
            "已安排中纪检跳过分类重试：%s（%s 个：%s）",
            run_at.strftime("%Y-%m-%d %H:%M:%S"),
            len(pending),
            "、".join(i.get("category", "") for i in pending),
        )

    def retry_skipped_categories(self) -> None:
        """定时触发：只重爬被跳过的中纪检子分类。"""
        pending = self.state.get_skipped_categories("ccdi", retryable_only=True)
        codes = [str(i.get("code", "")) for i in pending if i.get("code")]
        if not codes:
            self.cancel_skipped_retry()
            return
        if self._crawl_running or self._upload_running:
            logger.info("跳过分类重试延后：当前有任务运行中，%s 分钟后再试", self._skipped_retry_minutes())
            self.schedule_skipped_retry()
            return
        self._retry_round += 1
        logger.info(
            "开始重试中纪检跳过的 %s 个分类（第 %s 轮）：%s",
            len(codes),
            self._retry_round,
            "、".join(i.get("category", "") for i in pending),
        )
        self._stop_event.clear()
        self._pause_event.clear()
        self._crawl_running = True
        try:
            self._crawl_blocking(
                "ccdi",
                self._max_items_for_mode(),
                self._stop_event,
                self._pause_event,
                False,
                categories=codes,
            )
        except Exception:
            logger.exception("中纪检跳过分类重试失败")
        finally:
            self._crawl_running = False
        self.schedule_skipped_retry()

    def start_interval_scheduler(self) -> None:
        """启动测试模式的间隔调度线程（仅在 mode=test 时实际触发）。"""
        if self._interval_thread and self._interval_thread.is_alive():
            return
        self._interval_stop.clear()
        self._interval_thread = threading.Thread(
            target=self._interval_loop, daemon=True, name="interval-scheduler"
        )
        self._interval_thread.start()

    def stop_interval_scheduler(self) -> None:
        self._interval_stop.set()

    def _interval_loop(self) -> None:
        while not self._interval_stop.wait(5):
            if self.mode != "test":
                continue
            if self.test_schedule_mode != "interval":
                # 选的是「按每日时间点」，间隔任务整体不参与（爬取与上传都交给每日定时任务）
                continue
            now = datetime.now()
            if self._next_crawl_at and now >= self._next_crawl_at:
                self._next_crawl_at = self._compute_next(self.crawl_interval_minutes)
                if not (self._crawl_running or self._upload_running):
                    logger.info("测试模式间隔触发：开始爬取（%s）", self.test_source)
                    self.crawl_sync(
                        source=self.test_source, max_items=self._max_items_for_mode()
                    )
            if self._next_upload_at and now >= self._next_upload_at:
                self._next_upload_at = self._compute_next(self.upload_interval_minutes)
                if not (self._crawl_running or self._upload_running):
                    logger.info("测试模式间隔触发：开始上传")
                    self.upload_now_sync()

    def agent_status(self) -> dict:
        """查询学科智能体连接状态与两个知识库的文档状态统计。"""
        return get_agent_status(self.config)

    def set_agent_config(
        self,
        *,
        base_url: str,
        api_key: str,
        dataset_name_ccdi: str,
        dataset_name_bit: str,
    ) -> dict:
        """修改学科智能体地址、API Key 与两个库名称，并写回配置文件。"""
        if self._crawl_running or self._upload_running:
            raise RuntimeError("已有任务运行中，无法修改学科智能体配置")
        rag = self.config.setdefault("ragflow", {})
        rag["base_url"] = (base_url or "").strip().rstrip("/")
        rag["api_key"] = (api_key or "").strip()
        rag["dataset_name_ccdi"] = (dataset_name_ccdi or "").strip() or "中纪检"
        rag["dataset_name_bit"] = (dataset_name_bit or "").strip() or "校纪检"
        if self.config_path:
            _update_ragflow_in_config(
                self.config_path,
                {
                    "base_url": rag["base_url"],
                    "api_key": rag["api_key"],
                    "dataset_name_ccdi": rag["dataset_name_ccdi"],
                    "dataset_name_bit": rag["dataset_name_bit"],
                },
            )
        return {"saved": True}


def start_scheduler(
    state: CrawlerState, config: dict, controller: JobController
) -> BackgroundScheduler:
    sched_cfg = config.get("schedule", {})
    # 空值表示使用服务器本地时区；配置具体时区（如 Asia/Shanghai）则按该时区执行。
    tz = sched_cfg.get("timezone") or None
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.start()
    # 由控制器按配置（可在 UI 中修改）注册/停用每日定时任务
    controller.attach_scheduler(scheduler)
    logger.info(
        "定时任务已启动：爬取 %s，上传 %s",
        sched_cfg.get("crawl_time") or "未设置",
        sched_cfg.get("upload_time") or "未设置",
    )
    return scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="纪律法规爬虫（中纪检 + 校纪检）")
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径")
    parser.add_argument("--output-dir", type=Path, default=None, help="覆盖 config 中的输出目录")
    parser.add_argument(
        "--crawl-now",
        action="store_true",
        help="立即执行一次爬取（等价于在界面点「开始」，结束后按上传策略处理上传）",
    )
    parser.add_argument(
        "--source",
        choices=["all", "ccdi", "bit"],
        default="all",
        help="立即爬取时的来源（默认全部）",
    )
    parser.add_argument("--upload-now", action="store_true", help="立即执行一次上传")
    parser.add_argument("--no-ui", action="store_true", help="不启动监控 Web UI")
    parser.add_argument(
        "--no-schedule",
        action="store_true",
        help="不注册定时任务（仅在同时加 --no-ui 时生效；开着界面会照常启动定时任务）",
    )
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="首次运行时跳过自动全量爬取与上传，由 UI 或命令行手动触发",
    )
    args = parser.parse_args()

    config_path = args.config or (Path(__file__).resolve().parent / "config.yaml")
    config = load_config(config_path)
    if args.output_dir:
        config["output_dir"] = str(args.output_dir)

    output_dir = Path(config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "crawler_state.json"
    state = CrawlerState(state_path)
    controller = JobController(state, config)
    controller.config_path = config_path
    controller.start_interval_scheduler()

    # 实例自检：同一输出目录若还有旧进程活着，界面很可能会端口冲突、看到旧状态
    pid_file = output_dir / ".crawler.pid"
    try:
        other_pid = int((pid_file.read_text(encoding="utf-8").strip() or "0"))
    except (OSError, ValueError):
        other_pid = 0
    if other_pid and other_pid != os.getpid() and _pid_alive(other_pid):
        logger.warning(
            "检测到另一个实例可能还在运行（PID %s，见 %s）。"
            "如果它的监控界面占着端口，本实例的界面会起不来、浏览器里看到的仍是旧界面；"
            "请先结束旧进程（kill %s）再启动。",
            other_pid,
            pid_file,
            other_pid,
        )
    else:
        # 只有确认没有别的实例时，才把上次遗留的「运行中」改回空闲
        controller.recover_stale_progress()
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(_clear_pid_file, pid_file)
    except OSError as exc:
        logger.warning("写入 PID 文件失败（不影响运行）：%s", exc)

    # 首次运行标记：一旦开始就持久化，只有整轮爬完（两个来源都跑完）才清除。
    # 所以首次运行中途被中断，重启后仍然按"首次运行"继续。
    first_run = state.is_first_run() or not bool(
        state.snapshot().get("documents")
    )
    # 按编号方式：只要还没扫到编号上限，就仍算「首次执行」——
    # 中途重启后继续边爬边传、界面也保留首次运行标识。
    if not first_run and controller._ccdi_id_walk_pending():
        logger.info("中纪检编号尚未扫到上限，仍按首次运行处理")
        first_run = True
    if first_run:
        controller.mark_first_run(True)
    logger.info(
        "首次运行标记：%s（persisted=%s）",
        "是" if first_run else "否",
        state.is_first_run(),
    )

    monitor_cfg = config.get("monitor", {})
    logger.info(
        "进程启动：PID %s · 输出目录 %s · 监控 UI http://%s:%s",
        os.getpid(),
        output_dir,
        monitor_cfg.get("host", "0.0.0.0"),
        int(monitor_cfg.get("port", 8765)),
    )
    if not args.no_ui:
        ui_thread = threading.Thread(
            target=run_monitor_server,
            args=(
                controller,
                monitor_cfg.get("host", "0.0.0.0"),
                int(monitor_cfg.get("port", 8765)),
            ),
            daemon=True,
        )
        ui_thread.start()
        logger.info(
            "监控 UI: http://%s:%s",
            monitor_cfg.get("host", "0.0.0.0"),
            monitor_cfg.get("port", 8765),
        )

    ran_work = False
    if first_run and not args.no_auto_start:
        logger.info("首次运行：全量爬取（边爬边批量上传），结束后收尾上传")
        controller.mark_first_run(True)
        batch_done = controller.start_first_run_uploader()
        try:
            controller.crawl_sync()
        except Exception:
            logger.exception("首次全量爬取失败")
        finally:
            batch_done.set()
        ran_work = True
    else:
        if args.crawl_now:
            controller.crawl_now_sync(source=args.source)
            ran_work = True
        if args.upload_now:
            controller.upload_now_sync()
            ran_work = True

    scheduler = None
    # 只要开着监控界面就启动定时任务：界面里可以配置每日时间点，
    # 若这里悄悄跳过，就会出现“设了时间却不执行”的困惑。
    # 只有 --no-schedule 且 --no-ui（纯单次执行）时才不注册定时任务。
    if not args.no_schedule or not args.no_ui:
        scheduler = start_scheduler(state, config, controller)
    else:
        logger.info("已跳过定时任务（--no-schedule --no-ui：单次执行模式）")

    if args.no_schedule and args.no_ui:
        if ran_work:
            logger.info("单次任务已完成")
        else:
            parser.print_help()
        return

    logger.info("服务运行中，按 Ctrl+C 退出")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("收到退出信号")
        if scheduler:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
