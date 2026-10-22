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
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from crawl_bit_jwjc_regulations import (
    BIT_INDEX,
    _fetch_html,
    _parse_declared_total,
    backfill_bit_publish_dates,
    crawl_bit_jwjc_regulations,
)
from crawl_ccdi_regulations import (
    THROTTLE_ALERT_KEY,
    THROTTLE_ALERT_TEXT,
    _estimate_all_data_total,
    audit_ccdi_match_dates,
    backfill_ccdi_publish_years,
    crawl_ccdi_by_id,
    crawl_ccdi_regulations,
)
from crawl_common import (
    DEFAULT_USER_AGENTS,
    PlaywrightPdfExporter,
    load_config,
    resolve_bit_delay_range,
    resolve_detail_delay_range,
    resolve_delay_range,
    resolve_throttle_rounds,
    resolve_throttle_ranges,
)
from crawler_state import CrawlerState
from daily_run_log import DailyRunLog
from monitor_web_ui import run_monitor_server
from ragflow_kb_uploader import (
    RagflowKbUploader,
    get_agent_status,
    migrate_dataset_libraries,
    run_scheduled_uploads,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("discipline_crawler")


def _setup_logging(config: dict) -> None:
    """按 config.yaml 的 logging 段配置日志级别与文件轮转。

    level：DEBUG/INFO/WARNING/ERROR（只看告警和错误就填 WARNING）；
    http_access：是否记录每个 HTTP 请求（UI 每秒轮询，默认关闭）；
    file：日志文件路径（相对路径以项目目录为基准），留空则只输出到控制台；
    max_bytes / backup_count：按大小轮转，例如 10MB × 5 份。
    """
    log_cfg = config.get("logging", {}) or {}
    level_name = str(log_cfg.get("level", "INFO") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    stream = logging.StreamHandler()
    handlers: list[logging.Handler] = [stream]
    file_path = str(log_cfg.get("file") or "").strip()
    run_log_cfg = log_cfg.get("run_log", {}) or {}
    run_log_enabled = bool(run_log_cfg.get("enabled", True))
    if file_path:
        try:
            from logging.handlers import RotatingFileHandler

            path = Path(file_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent / path
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    path,
                    maxBytes=max(
                        1024 * 1024, int(log_cfg.get("max_bytes", 10 * 1024 * 1024))
                    ),
                    backupCount=max(1, int(log_cfg.get("backup_count", 5))),
                    encoding="utf-8",
                )
            )
        except (OSError, ValueError) as exc:
            print(f"日志文件初始化失败，仅输出到控制台：{exc}")
    if file_path or run_log_enabled:
        # 已经写了独立日志文件，控制台只保留告警/错误，免得 nohup 的重定向文件也跟着涨
        stream.setLevel(logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    # werkzeug 默认把每个请求按 INFO 记一行；UI 每秒轮询两次，不关会刷屏
    http_level = (
        logging.INFO if bool(log_cfg.get("http_access", False)) else logging.WARNING
    )
    for name in ("werkzeug", "flask.app"):
        logging.getLogger(name).setLevel(http_level)
    logger.info(
        "日志配置：级别 %s · 文件 %s · HTTP 访问日志 %s",
        level_name,
        file_path or "（仅控制台）",
        "开" if http_level == logging.INFO else "关",
    )

# 中纪检被跳过的子分类的重试任务 id（由调度器执行）
SKIPPED_RETRY_JOB_ID = "skipped_category_retry"
THROTTLE_RETRY_JOB_ID = "ccdi_throttle_retry"
FAILED_RETRY_JOB_ID = "ccdi_failed_retry"
FAILED_ALERT_KEY = "ccdi_failed_ids"
FAILED_RETRY_MINUTES = 30
FAILED_RETRY_MAX_ROUNDS = 3
RUN_LOG_ALERT_KEY = "daily_log_error"


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


def _install_signal_handlers(controller: "JobController") -> None:
    """systemd stop / 关机收到 SIGTERM 时优雅结束：置停止位、让日志改名、状态落盘。"""

    def _handle(signum, _frame):
        logger.info("收到停止信号 %s，正在结束当前任务…", signum)
        controller.request_stop()
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle)
        except (OSError, ValueError):
            continue


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
    retry_ids: list[int] | None = None,
    retry_probe=None,
) -> dict[str, int]:
    """执行爬取环节。max_items 用于测试模式：每个来源最多爬取该数量。
    parallel=True 且同时爬取两个来源时，中纪检与校纪检并行执行。
    categories 仅用于中纪检：只爬取这些导航码（重试被跳过的子分类）。
    retry_ids 仅用于中纪检：只重试这些失败编号，不动日常/首次运行的游标。
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

    def run_ccdi() -> int:
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
                # 清掉上一轮的活动量，避免卡片把旧数字和错误混在一起
                extra={"scanned": 0, "new_count": 0, "loaded": 0, "pending": False},
            )
            state.set_alert(
                "crawl_error_ccdi",
                f"中纪检爬取异常中断：{reason}",
                level="error",
            )
            raise
        logger.info("中纪检完成，新增 %s 个", ccdi_new)
        state.set_progress(
            "ccdi", extra={"last_crawl_new_count": int(ccdi_new)}
        )
        return int(ccdi_new)

    def _run_ccdi_impl(mode: str, first_run_now: bool) -> int:
        if retry_ids:
            try:
                id_walk_max = int(crawl_cfg.get("id_walk_max", 71069) or 71069)
            except (TypeError, ValueError):
                id_walk_max = 71069
            return crawl_ccdi_by_id(
                output_dir,
                state,
                headless=headless,
                timeout_ms=timeout_ms,
                request_delay_seconds=delay_range,
                detail_page_delay_seconds=detail_delay_range,
                max_id=id_walk_max,
                turn_back_at=0,
                stop_event=stop_event,
                pause_event=pause_event,
                pdf_scale=scale_ccdi,
                browser_channel=browser_channel,
                stealth=stealth,
                user_agents=user_agents,
                first_run=False,
                retry_ids=list(retry_ids),
                retry_probe=retry_probe,
                throttle_wait_ranges=throttle_ranges,
                throttle_max_rounds=throttle_rounds,
            )
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
                turn_back_at = int(
                    crawl_cfg.get("id_walk_turn_back_at", 0) or 0
                )
            except (TypeError, ValueError):
                turn_back_at = 0
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
                turn_back_at=turn_back_at,
                daily_span=daily_span,
                invalid_streak_stop=streak_stop,
                stop_event=stop_event,
                pause_event=pause_event,
                pdf_scale=scale_ccdi,
                browser_channel=browser_channel,
                stealth=stealth,
                user_agents=user_agents,
                first_run=first_run_now,
                retry_probe=retry_probe,
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

    def run_bit() -> int:
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
                extra={"scanned": 0, "new_count": 0, "loaded": 0, "pending": False},
            )
            state.set_alert(
                "crawl_error_bit",
                f"校纪检爬取异常中断：{reason}",
                level="error",
            )
            raise
        logger.info("校纪检完成，新增 %s 个", bit_new)
        state.set_progress(
            "bit", extra={"last_crawl_new_count": int(bit_new)}
        )
        return int(bit_new)

    want_ccdi = source in ("all", "ccdi")
    want_bit = source in ("all", "bit")

    results = {"ccdi": 0, "bit": 0}
    if parallel and want_ccdi and want_bit:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                "ccdi": pool.submit(run_ccdi),
                "bit": pool.submit(run_bit),
            }
            for source_key, future in futures.items():
                try:
                    results[source_key] = int(future.result())
                except Exception:
                    logger.exception("%s 爬取失败", source_key)
        return results

    if want_ccdi:
        results["ccdi"] = run_ccdi()
    if want_bit:
        results["bit"] = run_bit()
    return results


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
        self._migration_running = False
        self._last_crawl_results = {"ccdi": 0, "bit": 0}
        self._index_building = False
        index_meta = state.get_version_index_status()
        self._index_ready = bool(index_meta.get("ready"))
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
        # 失败编号（failed_ids）批量重试：已重试轮数
        self._failed_round = 0
        # 抢占式重试：爬取过程中收到重试请求时，主循环会在下一个编号前插队处理
        self._retry_interrupt = threading.Event()
        # 每次爬取一个独立日志文件（logs/runs/<开始>至<结束>.log）
        run_log_cfg = ((config.get("logging", {}) or {}).get("run_log", {}) or {})
        run_log_dir = Path(str(run_log_cfg.get("dir") or "logs/runs"))
        if not run_log_dir.is_absolute():
            run_log_dir = Path(__file__).resolve().parent / run_log_dir
        self.run_log = DailyRunLog(
            run_log_dir,
            keep_days=int(run_log_cfg.get("keep_days", 7) or 7),
            enabled=bool(run_log_cfg.get("enabled", True)),
        )

    def get_settings(self) -> dict:
        crawl_cfg = self.config.get("crawl", {}) or {}
        index_status = self.state.get_version_index_status()
        maintenance_flags = self.state.get_maintenance_flags()
        self._index_ready = all(
            bool(maintenance_flags.get(source))
            for source in ("ccdi", "bit")
        )
        return {
            "mode": self.mode,
            "test_count": self.test_count,
            "test_source": self.test_source,
            "full_test": self.full_test,
            "test_schedule_mode": self.test_schedule_mode,
            # 编号已经扫到上限时不再显示「首次执行」（避免旧标记一直挂着）
            "first_run_pending": (
                self.state.is_first_run() and self._ccdi_id_walk_pending()
            ),
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
            "next_daily_upload_at": self.next_effective_upload_at(),
            "upload_strategy": self._upload_strategy(),
            "upload_interval_days": self._upload_interval_days(),
            "next_throttle_retry_at": self.next_throttle_retry_at(),
            "failed_ids": self._failed_ids(),
            "failed_retry_round": self._failed_round,
            "next_failed_retry_at": self.next_failed_retry_at(),
            "playwright_headless": bool(crawl_cfg.get("playwright_headless", True)),
            "crawl_running": self._crawl_running,
            "upload_running": self._upload_running,
            "migration_running": self._migration_running,
            "index_building": self._index_building,
            "index_ready": self._index_ready,
            "version_index_status": index_status,
            "maintenance_flags": maintenance_flags,
            "parse_settings": {
                "max_parsing_documents": int(
                    ((self.config.get("upload", {}) or {}).get(
                        "max_parsing_documents", 500
                    ))
                    or 500
                ),
                "parse_timeout_minutes": float(
                    ((self.config.get("upload", {}) or {}).get(
                        "parse_timeout_minutes", 30
                    ))
                    or 30
                ),
                "parse_retry_rounds": int(
                    ((self.config.get("upload", {}) or {}).get(
                        "parse_retry_rounds", 1
                    ))
                    or 0
                ),
            },
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
        upload_interval_days: int | None = None,
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
        if upload_interval_days is not None:
            try:
                days = int(upload_interval_days)
            except (TypeError, ValueError):
                raise ValueError("上传间隔天数必须是整数")
            if days < 0 or days > 365:
                raise ValueError("上传间隔天数需在 0 ~ 365 之间（0=随爬取立即上传）")
            self._update_upload_config(
                {"strategy": "interval", "interval_days": days}
            )
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
        log_cfg = self.config.get("logging", {}) or {}
        return {
            "output_dir": str(self.output_dir),
            "crawl.playwright_headless": crawl_cfg.get("playwright_headless", True),
            "crawl.browser_channel": crawl_cfg.get("browser_channel") or "（自带 Chromium）",
            "crawl.stealth": crawl_cfg.get("stealth", True),
            "crawl.rotate_user_agent": bool(crawl_cfg.get("rotate_user_agent", True)),
            "crawl.parallel_sources": crawl_cfg.get("parallel_sources", True),
            "crawl.crawl_mode": crawl_cfg.get("crawl_mode", "id_walk"),
            "crawl.id_walk_max": crawl_cfg.get("id_walk_max", 71069),
            "crawl.id_walk_turn_back_at": crawl_cfg.get("id_walk_turn_back_at", 0),
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
            "crawl.throttle_retry_minutes": crawl_cfg.get(
                "throttle_retry_minutes", 30
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
            "upload.strategy": up.get("strategy", "interval"),
            "upload.interval_days": up.get("interval_days", 3),
            "upload.min_age_days": up.get("min_age_days", 3),
            "upload.max_parsing_documents": up.get("max_parsing_documents", 500),
            "upload.parse_timeout_minutes": up.get("parse_timeout_minutes", 30),
            "upload.parse_poll_seconds": up.get("parse_poll_seconds", 10),
            "upload.parse_retry_rounds": up.get("parse_retry_rounds", 1),
            "ragflow.base_url": rag.get("base_url", ""),
            "ragflow.chunk_method": rag.get("chunk_method", "laws"),
            "ragflow.dataset_name_ccdi": rag.get("dataset_name_ccdi", "中纪检"),
            "ragflow.dataset_name_bit": rag.get("dataset_name_bit", "校纪检"),
            "logging.run_log.enabled": (log_cfg.get("run_log", {}) or {}).get(
                "enabled", True
            ),
            "logging.run_log.dir": (log_cfg.get("run_log", {}) or {}).get(
                "dir", "logs/runs"
            ),
            "logging.run_log.keep_days": (log_cfg.get("run_log", {}) or {}).get(
                "keep_days", 7
            ),
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

    # ---- 运行模式上传策略：interval（每 N 天全量传未上传） / age（旧：按保存天数） ----
    def _upload_strategy(self) -> str:
        cfg = self.config.get("upload", {}) or {}
        value = str(cfg.get("strategy") or "interval").strip().lower()
        return value if value in ("interval", "age") else "interval"

    def _upload_interval_days(self) -> int:
        cfg = self.config.get("upload", {}) or {}
        raw = cfg.get("interval_days", 3)
        try:
            # 0 = 随爬取立即上传（和首次运行一样），不再按天数攒
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 3

    def _last_upload_datetime(self) -> datetime | None:
        """两个来源里最近一次上传的时间（interval 策略的计时起点）。"""
        values: list[datetime] = []
        for raw in (self.state.snapshot().get("last_upload_time") or {}).values():
            try:
                values.append(datetime.fromisoformat(str(raw)))
            except (TypeError, ValueError):
                continue
        return max(values) if values else None

    def _upload_due(self) -> tuple[bool, datetime | None]:
        """interval 策略：距上次上传是否已满 interval_days 天。"""
        days = self._upload_interval_days()
        if days <= 0:
            return True, None
        anchor = self._last_upload_datetime()
        if anchor is None:
            return True, None
        due_at = anchor + timedelta(days=days)
        # 留 60 秒容差：定时点在整分触发，避免差几毫秒被判成"还没到期"
        return datetime.now() >= due_at - timedelta(seconds=60), due_at

    def _next_upload_checkpoint(self, base: datetime) -> datetime | None:
        """不早于 base 的下一个「上传时间点」（每天 HH:MM 的那个检查点）。"""
        value = ((self.config.get("schedule", {}) or {}).get("upload_time") or "").strip()
        if not value:
            return None
        try:
            hour, minute = _parse_hhmm(value)
        except Exception:
            return None
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < base:
            candidate += timedelta(days=1)
        return candidate

    def next_effective_upload_at(self) -> str | None:
        """界面上的「下一次上传」：interval 策略要算到真正会执行的那一天。"""
        if self._upload_strategy() != "interval":
            return self._next_run_at("daily_upload")
        if self._upload_interval_days() <= 0:
            return None  # 随爬取立即上传，没有固定时间点
        _, due_at = self._upload_due()
        now = datetime.now()
        base = max(now, due_at) if due_at else now
        nxt = self._next_upload_checkpoint(base)
        return nxt.isoformat() if nxt else None

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

    def _version_index_is_ready(self) -> bool:
        """上传门禁以两来源的日期/索引时效标志为准。"""
        flags = self.state.get_maintenance_flags()
        self._index_ready = all(
            bool(flags.get(source)) for source in ("ccdi", "bit")
        )
        return self._index_ready

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
        migration = snap.get("dataset_migration") or {}
        if migration.get("status") == "running":
            self.state.set_dataset_migration(
                status="interrupted",
                error="迁库进程上次被中断；请重新点击「迁库」完成重建",
            )
        elif migration.get("status") == "uploading":
            self.state.set_dataset_migration(
                status="interrupted",
                error="新库已创建，但重新上传被中断；点击「立即上传」继续",
            )
        queue = snap.get("parse_queue") or {}
        active = [dict(row) for row in queue.get("active", [])]
        if active:
            retry = [dict(row) for row in queue.get("retry", [])]
            seen = {
                (row.get("dataset_id"), row.get("document_id")) for row in retry
            }
            for row in active:
                key = (row.get("dataset_id"), row.get("document_id"))
                if key not in seen:
                    row["reason"] = "程序重启前的解析未完成"
                    retry.append(row)
            self.state.set_parse_queue(
                {
                    "active": [],
                    "retry": retry,
                    "wave": int(queue.get("wave") or 0),
                    "message": f"已恢复 {len(active)} 个中断的解析文件，等待重试",
                }
            )

    def _missing_date_targets(self) -> tuple[list[dict], list[dict]]:
        """返回可联网补录日期的中纪检、校纪检缺失记录。"""
        docs = self.state.snapshot().get("documents") or {}
        missing_ccdi: list[dict] = []
        missing_bit: list[dict] = []
        for doc_key, meta in docs.items():
            if not isinstance(meta, dict) or meta.get("publish_date"):
                continue
            key = str(doc_key)
            if meta.get("source") == "ccdi" and re.fullmatch(r"ccdi:\d+", key):
                missing_ccdi.append(dict(meta))
            elif meta.get("source") == "bit" and key.startswith("bit:"):
                missing_bit.append(dict(meta))
        return missing_ccdi, missing_bit

    def date_source_stats(self, snapshot: dict | None = None) -> dict:
        """按来源统计可联网补录文档的日期完成度。"""
        docs = (snapshot or self.state.snapshot()).get("documents") or {}
        result = {
            "ccdi": {
                "total": 0,
                "dated": 0,
                "missing": 0,
                "filename_dated": 0,
            },
            "bit": {
                "total": 0,
                "dated": 0,
                "missing": 0,
                "filename_dated": 0,
            },
        }
        for doc_key, meta in docs.items():
            if not isinstance(meta, dict):
                continue
            source = str(meta.get("source") or "")
            if source not in result:
                continue
            result[source]["total"] += 1
            if meta.get("publish_date"):
                result[source]["dated"] += 1
                if meta.get("date_source") == "filename":
                    result[source]["filename_dated"] += 1
            else:
                result[source]["missing"] += 1
        return result

    def prepare_version_index_on_startup(
        self, *, force_full_dates: bool | None = None
    ) -> dict:
        """日期补录完成后再生成最终索引；日期完整时仅做本地核对。"""
        if self._index_building:
            return {"ready": False, "building": True, "message": "索引正在准备"}
        flags = self.state.get_maintenance_flags()
        pending = [
            source for source in ("ccdi", "bit")
            if not bool(flags.get(source))
        ]
        if not pending:
            self._index_ready = True
            return {
                "ready": True,
                "building": False,
                "imported": 0,
                "message": "中纪检、校纪检时效标志均为 true，沿用已有日期与索引",
            }
        # 首次运行不做“本地数量等于网页数量”的快速通过判断：
        # 即使某个年份数量碰巧相同，也逐页核对网页编号，避免漏号或错号。
        if force_full_dates is None:
            force_full_dates = self.state.is_first_run()
        result = self.run_date_index_maintenance(
            tuple(pending),
            force_full_dates=bool(force_full_dates),
        )
        flags = self.state.get_maintenance_flags()
        ready = all(bool(flags.get(source)) for source in ("ccdi", "bit"))
        errors = result.get("errors") or {}
        return {
            "ready": ready,
            "building": False,
            "imported": 0,
            "errors": errors,
            "message": (
                "日期补全与版本索引已全部完成"
                if ready
                else "；".join(f"{k}: {v}" for k, v in errors.items())
                or "仍有来源未完成日期补全或索引建立"
            ),
        }
        self._index_building = True
        errors: list[str] = []
        source_errors = {"ccdi": "", "bit": ""}
        imported = 0
        try:
            try:
                imported = self.state.import_unregistered_local_files(
                    self.output_dir, rebuild_index=False
                )
            except Exception as exc:
                errors.append(f"扫描遗留 PDF 失败：{exc}")
                logger.exception("扫描本地未登记 PDF 失败")

            missing_ccdi, missing_bit = self._missing_date_targets()
            prior_snapshot = self.state.snapshot()
            prior_backfill = prior_snapshot.get("backfill_status", {}) or {}
            prior_ccdi = prior_backfill.get("ccdi") or {}
            if prior_ccdi.get("force_full"):
                # 清除旧版本遗留的强制重扫标记，但绝不动年份/页码断点。
                self.state.set_backfill_status(
                    "ccdi",
                    extra={"force_full": False, "year_table_version": 2},
                )
                prior_snapshot = self.state.snapshot()
                prior_backfill = prior_snapshot.get("backfill_status", {}) or {}
            source_stats = self.date_source_stats()
            for source, label in (("ccdi", "中纪检"), ("bit", "校纪检")):
                row = source_stats[source]
                prior = prior_backfill.get(source) or {}
                resumable = (
                    row["missing"] > 0
                    and str(prior.get("status") or "")
                    in {"running", "error", "interrupted"}
                    and bool(prior.get("next_page") or prior.get("page"))
                )
                if resumable:
                    continue
                self.state.set_backfill_status(
                    source,
                    status="done" if row["missing"] == 0 else "pending",
                    current=row["dated"],
                    total=row["total"],
                    message=(
                        f"{label}日期已完整"
                        if row["missing"] == 0
                        else f"{label}待补日期 {row['missing']} 条"
                    ),
                    persist=False,
                )
            status_before = self.state.get_version_index_status()
            snapshot_now = self.state.snapshot()
            has_index = bool(snapshot_now.get("version_index"))
            source_index_docs = {
                source: sum(
                    1
                    for record in (
                        snapshot_now.get("version_records") or {}
                    ).values()
                    if isinstance(record, dict)
                    and record.get("source") == source
                )
                for source in ("ccdi", "bit")
            }
            separate_indices = snapshot_now.get("version_indices") or {}
            source_meta_before = status_before.get("sources") or {}

            def source_index_present(source: str) -> bool:
                return bool(separate_indices.get(source)) or source_index_docs[source] == 0

            bit_index_needed = bool(
                imported
                or missing_bit
                or not (source_meta_before.get("bit") or {}).get("ready")
                or not source_index_present("bit")
            )
            ccdi_index_needed = bool(
                imported
                or missing_ccdi
                or not (source_meta_before.get("ccdi") or {}).get("ready")
                or not source_index_present("ccdi")
            )
            has_source_indexes = bool(
                status_before.get("sources")
                and all(
                    isinstance(status_before["sources"].get(source), dict)
                    and (
                        bool((separate_indices.get(source) or {}))
                        or source_index_docs[source] == 0
                    )
                    for source in ("ccdi", "bit")
                )
            )
            needs_work = bool(
                imported
                or missing_ccdi
                or missing_bit
                or not status_before.get("ready")
                or not has_index
                or not has_source_indexes
            )
            if not needs_work:
                self._index_ready = True
                return {
                    "ready": True,
                    "building": False,
                    "imported": 0,
                    "message": "版本索引已就绪（日期完整，无需联网补录）",
                }

            if bit_index_needed:
                self.state.mark_source_index_pending(
                    "bit", "启动检查：校纪检索引需要补建"
                )
            if ccdi_index_needed:
                self.state.mark_source_index_pending(
                    "ccdi", "启动检查：中纪检索引需要补建"
                )
            self.state.set_progress(
                "upload",
                status="running",
                current=0,
                total=len(missing_ccdi) + len(missing_bit),
                message=(
                    "启动检查：正在补录文件日期"
                    f"（中纪检 {len(missing_ccdi)}、校纪检 {len(missing_bit)}）…"
                ),
            )
            crawl_cfg = self.config.get("crawl", {}) or {}
            if missing_bit:
                try:
                    backfill_bit_publish_dates(
                        self.state,
                        rebuild_index=False,
                        stop_event=self._stop_event,
                        pause_event=self._pause_event,
                    )
                except Exception as exc:
                    errors.append(f"校纪检日期补录失败：{exc}")
                    source_errors["bit"] = str(exc)
                    logger.exception("启动时补录校纪检日期失败")
                    row = self.date_source_stats()["bit"]
                    self.state.set_backfill_status(
                        "bit",
                        status="error",
                        current=row["dated"],
                        total=row["total"],
                        message=f"校纪检日期补录失败：{exc}",
                    )
            filename_date_counts = self.state.fill_missing_dates_from_filenames(
                ("ccdi", "bit"), rebuild_index=False
            )
            if any(filename_date_counts.values()):
                logger.info(
                    "文件名日期兜底：中纪检 %s 条，校纪检 %s 条",
                    filename_date_counts.get("ccdi", 0),
                    filename_date_counts.get("bit", 0),
                )
            _, missing_bit_after = self._missing_date_targets()
            bit_index_result = {"ok": False}

            def build_bit_index() -> None:
                try:
                    self.state.rebuild_source_version_index(
                        "bit",
                        ready=not missing_bit_after,
                        reason=(
                            "校纪检日期已补齐，版本索引已建立"
                            if not missing_bit_after
                            else f"校纪检日期仍缺 {len(missing_bit_after)} 条"
                        ),
                        error=source_errors["bit"],
                    )
                    bit_index_result["ok"] = True
                except Exception as exc:
                    source_errors["bit"] = str(exc)
                    errors.append(f"校纪检版本索引建立失败：{exc}")
                    logger.exception("校纪检版本索引建立失败")

            # 校纪检日期处理完即开始建索引；中纪检继续联网补日期，两者并行。
            # 如果校纪检索引上次已经完整建立，则保留它，不在重启时重做。
            bit_index_thread = None
            if bit_index_needed:
                bit_index_thread = threading.Thread(
                    target=build_bit_index,
                    daemon=True,
                    name="bit-version-index",
                )
                bit_index_thread.start()
            else:
                bit_index_result["ok"] = True
            missing_ccdi, _ = self._missing_date_targets()
            if missing_ccdi and not self._stop_event.is_set():
                try:
                    throttle_ranges = resolve_throttle_ranges(crawl_cfg)
                    backfill_ccdi_publish_years(
                        self.state,
                        headless=bool(crawl_cfg.get("playwright_headless", True)),
                        timeout_ms=int(
                            crawl_cfg.get("page_load_timeout_ms", 120_000)
                        ),
                        browser_channel=crawl_cfg.get("browser_channel") or None,
                        stealth=bool(crawl_cfg.get("stealth", True)),
                        stop_event=self._stop_event,
                        pause_event=self._pause_event,
                        throttle_wait_ranges=throttle_ranges,
                        throttle_max_rounds=resolve_throttle_rounds(
                            crawl_cfg, default=len(throttle_ranges)
                        ),
                        rebuild_index_per_batch=False,
                        request_delay_seconds=resolve_delay_range(crawl_cfg),
                        force_full=False,
                    )
                except Exception as exc:
                    errors.append(f"中纪检日期补录失败：{exc}")
                    source_errors["ccdi"] = str(exc)
                    logger.exception("启动时补录中纪检历史年份失败")
                    row = self.date_source_stats()["ccdi"]
                    self.state.set_backfill_status(
                        "ccdi",
                        status="error",
                        current=row["dated"],
                        total=row["total"],
                        message=f"中纪检日期补录失败：{exc}",
                    )

            if bit_index_thread is not None:
                bit_index_thread.join()
            if bit_index_needed and not bit_index_result["ok"]:
                try:
                    self.state.rebuild_source_version_index(
                        "bit",
                        ready=False,
                        error=source_errors["bit"],
                        reason="校纪检版本索引建立失败",
                    )
                except Exception:
                    logger.exception("重试建立校纪检版本索引失败")

            # 联网补录结束后的第二次文件名兜底，覆盖本轮才暴露出的无日期文件。
            filename_date_counts_after = self.state.fill_missing_dates_from_filenames(
                ("ccdi",), rebuild_index=False
            )
            if filename_date_counts_after.get("ccdi"):
                logger.info(
                    "中纪检补录后文件名日期兜底：%s 条",
                    filename_date_counts_after["ccdi"],
                )
            remaining_ccdi, remaining_bit = self._missing_date_targets()
            if ccdi_index_needed:
                try:
                    self.state.rebuild_source_version_index(
                        "ccdi",
                        ready=not remaining_ccdi,
                        reason=(
                            "中纪检日期已补齐，版本索引已建立"
                            if not remaining_ccdi
                            else f"中纪检日期仍缺 {len(remaining_ccdi)} 条"
                        ),
                        error=source_errors["ccdi"],
                    )
                except Exception as exc:
                    source_errors["ccdi"] = str(exc)
                    errors.append(f"中纪检版本索引建立失败：{exc}")
                    logger.exception("中纪检版本索引建立失败")

            final_stats = self.date_source_stats()
            ccdi_backfill = (
                self.state.get_backfill_statuses().get("ccdi") or {}
            )
            for source, label in (("ccdi", "中纪检"), ("bit", "校纪检")):
                row = final_stats[source]
                ccdi_totals_complete = (
                    source == "ccdi"
                    and ccdi_backfill.get("status") == "done"
                )
                self.state.set_backfill_status(
                    source,
                    status=(
                        "done"
                        if row["missing"] == 0 or ccdi_totals_complete
                        else "error"
                    ),
                    current=row["dated"],
                    total=row["total"],
                    message=(
                        (
                            f"{label}年份总数已全部获取；"
                            f"文件名兜底后仍无日期 {row['missing']} 条"
                        )
                        if ccdi_totals_complete and row["missing"] > 0
                        else f"{label}日期补录完成：{row['dated']}/{row['total']}"
                        if row["missing"] == 0
                        else (
                            f"{label}日期仍缺 {row['missing']} 条："
                            f"{row['dated']}/{row['total']}"
                        )
                    ),
                    extra={"missing": row["missing"]},
                    persist=False,
                )
            index_meta = self.state.get_version_index_status()
            ready = bool(index_meta.get("ready"))
            reason = (
                "日期补录完成，最终版本索引已建立"
                if ready
                else (
                    f"日期或索引未就绪：中纪检缺 {len(remaining_ccdi)}、"
                    f"校纪检缺 {len(remaining_bit)}"
                )
            )
            self._index_ready = ready
            message = (
                f"版本索引就绪：补登记 {imported} 个本地文件，"
                "已按完整日期建立索引"
                if ready
                else f"{reason}；为保证版本判断准确，上传已暂停"
            )
            self.state.set_progress(
                "upload",
                status="idle" if ready else "error",
                current=int(final_stats["ccdi"]["dated"]),
                total=int(final_stats["ccdi"]["total"]),
                message=message,
            )
            if ready:
                self.state.clear_alert("version_index")
            else:
                detail = "；".join(errors)
                self.state.set_alert(
                    "version_index",
                    message + (f"（{detail}）" if detail else ""),
                    level="error",
                )
            logger.info(message)
            return {
                "ready": ready,
                "building": False,
                "imported": imported,
                "missing_ccdi": len(remaining_ccdi),
                "missing_bit": len(remaining_bit),
                "errors": errors,
                "message": message,
            }
        finally:
            self._index_building = False

    def _maintain_source(
        self, source: str, *, force_full_dates: bool = False
    ) -> None:
        """单个来源：按需联网补日期，文件名兜底，再建立索引并置时效位。"""
        flags = self.state.get_maintenance_flags()
        if bool(flags.get(source)):
            logger.info("%s时效标志为 true，沿用已有日期与索引", source)
            return
        stats_before = self.date_source_stats()[source]
        needs_date_scan = bool(
            stats_before["missing"] > 0
            or (source == "ccdi" and force_full_dates)
        )
        if needs_date_scan:
            self.state.reset_backfill_progress(source)
            logger.info(
                "%s开始新一轮日期补全：已清空 backfill_status 和旧断点",
                source,
            )
        self.state.set_backfill_status(
            source,
            status="running",
            current=stats_before["dated"],
            total=stats_before["total"],
            message=(
                f"扫描 crawler_state：{stats_before['dated']}/"
                f"{stats_before['total']} 条已有日期"
            ),
        )
        if needs_date_scan:
            crawl_cfg = self.config.get("crawl", {}) or {}
            if source == "ccdi":
                throttle_ranges = resolve_throttle_ranges(crawl_cfg)
                backfill_ccdi_publish_years(
                    self.state,
                    headless=bool(crawl_cfg.get("playwright_headless", True)),
                    timeout_ms=int(
                        crawl_cfg.get("page_load_timeout_ms", 120_000)
                    ),
                    browser_channel=crawl_cfg.get("browser_channel") or None,
                    stealth=bool(crawl_cfg.get("stealth", True)),
                    stop_event=self._stop_event,
                    pause_event=self._pause_event,
                    throttle_wait_ranges=throttle_ranges,
                    throttle_max_rounds=resolve_throttle_rounds(
                        crawl_cfg, default=len(throttle_ranges)
                    ),
                    rebuild_index_per_batch=False,
                    request_delay_seconds=resolve_delay_range(crawl_cfg),
                    force_full=force_full_dates,
                )
            else:
                backfill_bit_publish_dates(
                    self.state,
                    rebuild_index=False,
                    stop_event=self._stop_event,
                    pause_event=self._pause_event,
                )
        match_audit: dict | None = None
        if source == "ccdi":
            # 不能只看页面“匹配到了”：必须回到 documents 核对匹配编号对应的
            # ccdi:<N> 是否真的写入了 publish_date，并检查跨年份重复编号。
            match_audit = audit_ccdi_match_dates(self.state, repair=True)
            self.state.set_backfill_status(
                "ccdi",
                extra={"match_audit": match_audit},
            )
            if force_full_dates and not match_audit.get("audit_complete"):
                raise RuntimeError(
                    "中纪检强制重扫后仍有年份缺少逐页编号审计，"
                    "无法证明网页编号没有跨年重复"
                )
            if not match_audit.get("consistent"):
                raise RuntimeError(
                    "中纪检编号/日期一致性校验失败："
                    f"跨年重复 {match_audit['repeated_count']}，"
                    f"同年跨页重复 "
                    f"{match_audit['same_year_repeated_count']}，"
                    f"未匹配 {match_audit['unmatched_count']}，"
                    f"匹配但缺日期 "
                    f"{match_audit['matched_missing_date_count']}，"
                    f"日期年份不符 "
                    f"{match_audit['matched_wrong_year_count']}"
                )
        # 联网补录后仍无日期的文件，按文件名年份兜底。
        self.state.fill_missing_dates_from_filenames(
            (source,), rebuild_index=False
        )
        stats_after = self.date_source_stats()[source]
        backfill_row = (
            self.state.get_backfill_statuses().get(source) or {}
        )
        if source == "bit" and stats_after["missing"] > 0:
            raise RuntimeError(
                f"校纪检仍有 {stats_after['missing']} 条文件没有日期"
            )
        if (
            source == "ccdi"
            and stats_after["missing"] > 0
            and backfill_row.get("status") != "done"
        ):
            raise RuntimeError(
                "中纪检年份总数尚未全部获取，日期补全未完成"
            )
        self.state.rebuild_source_version_index(
            source,
            ready=True,
            reason=(
                f"日期补全完成（{stats_after['dated']}/{stats_after['total']}）"
                "，版本索引已建立"
            ),
            require_all_dates=False,
        )
        index_meta = self.state.get_version_index_status()
        source_ready = bool(
            ((index_meta.get("sources") or {}).get(source) or {}).get("ready")
        )
        if not source_ready:
            raise RuntimeError(f"{source} 版本索引建立后仍未就绪")
        self.state.set_backfill_status(
            source,
            status="done",
            current=stats_after["dated"],
            total=stats_after["total"],
            message=(
                f"日期补全与索引完成：{stats_after['dated']}/"
                f"{stats_after['total']} 条已有日期"
            ),
            extra={
                key: value
                for key, value in backfill_row.items()
                if key
                not in {
                    "status",
                    "current",
                    "total",
                    "message",
                    "updated_at",
                }
            },
        )
        self.state.set_maintenance_flag(source, True)

    def run_date_index_maintenance(
        self,
        sources: list[str] | tuple[str, ...] = ("ccdi", "bit"),
        *,
        force_full_dates: bool = False,
    ) -> dict[str, bool]:
        """并行执行各来源日期补全和索引建立；true 的来源直接沿用。"""
        requested = [source for source in sources if source in ("ccdi", "bit")]
        if not requested:
            return {"ccdi": True, "bit": True}
        if self._index_building:
            raise RuntimeError("日期补全/索引建立正在进行")
        self._index_building = True
        outcomes: dict[str, bool] = {}
        errors: dict[str, str] = {}

        def worker(source: str) -> None:
            try:
                self._maintain_source(
                    source,
                    force_full_dates=force_full_dates and source == "ccdi",
                )
                outcomes[source] = True
            except Exception as exc:
                errors[source] = str(exc)
                outcomes[source] = False
                logger.exception("%s 日期/索引维护失败", source)
                self.state.set_maintenance_flag(source, False)
                row = self.date_source_stats()[source]
                self.state.set_backfill_status(
                    source,
                    status="error",
                    current=row["dated"],
                    total=row["total"],
                    message=f"日期或索引维护失败：{exc}",
                )
                try:
                    self.state.rebuild_source_version_index(
                        source,
                        ready=False,
                        reason="日期补全或索引建立失败",
                        error=str(exc),
                        require_all_dates=False,
                    )
                except Exception:
                    logger.exception("%s 错误状态写入索引元数据失败", source)

        try:
            threads = [
                threading.Thread(
                    target=worker,
                    args=(source,),
                    daemon=True,
                    name=f"maintenance-{source}",
                )
                for source in requested
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            self._index_building = False
        flags = self.state.get_maintenance_flags()
        self._index_ready = all(
            bool(flags.get(source)) for source in ("ccdi", "bit")
        )
        return {
            "ccdi": bool(flags.get("ccdi")),
            "bit": bool(flags.get("bit")),
            "errors": errors,
        }

    def _post_crawl_maintenance_and_upload(
        self, source: str, *, was_first_run: bool
    ) -> None:
        """爬取后：有更新才置 false，补日期/索引完成后再上传。"""
        flags = self.state.get_maintenance_flags()
        targets: list[str] = []
        if was_first_run:
            self.state.set_all_maintenance_flags(False)
            targets = ["ccdi", "bit"]
        else:
            for item in ("ccdi", "bit"):
                if self._last_crawl_results.get(item, 0) > 0:
                    self.state.set_maintenance_flag(item, False)
                    targets.append(item)
                elif not flags.get(item):
                    targets.append(item)
            targets = sorted(set(targets))
        if self._stop_event.is_set():
            return
        if not targets:
            logger.info("本轮爬取无更新，时效标志保持不变，不执行日期补全、索引和上传")
            return
        result = self.run_date_index_maintenance(
            tuple(targets), force_full_dates=was_first_run
        )
        if not all(
            bool(self.state.get_maintenance_flags().get(item))
            for item in ("ccdi", "bit")
        ):
            logger.error(
                "日期/索引维护未完成，跳过上传：%s", result.get("errors") or result
            )
            return
        if self.upload_mode != "auto":
            logger.info("日期补全和版本索引已完成；上传方式为手动，等待用户上传")
            return
        logger.info("日期补全和版本索引已完成，开始上传最新版本")
        self.upload_now_sync()

    def _upload_pass(self, stop_event: threading.Event | None = None) -> None:
        """执行一次“立即上传”（跳过已上传的），供首次运行的批量上传循环调用。"""
        if (
            self._upload_running
            or self._migration_running
            or self._index_building
            or not self._version_index_is_ready()
        ):
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
        if (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
            logger.warning("定时爬取跳过：已有任务运行中（这个时间点不再补跑，等下一天）")
            return
        source = self.test_source if self.mode == "test" else "all"
        self.crawl_sync(source=source, max_items=self._max_items_for_mode())

    def scheduled_upload(self) -> None:
        """定时任务触发的上传：测试模式立即推送全部待上传，运行模式按保留天数。

        到点时如果爬取还没结束，上传会与爬取并行启动（不再整体跳过）。
        """
        if (
            self._upload_running
            or self._migration_running
            or self._index_building
            or self._crawl_running
        ):
            logger.warning("定时上传跳过：爬取、维护或上传任务正在运行")
            return
        if not self._version_index_is_ready():
            logger.warning("定时上传跳过：版本索引未就绪")
            return
        # 测试模式的「间隔（分钟）」上传不走这里（由 _interval_loop 直接触发），
        # 所以到这里的是：运行模式，或测试模式的「按每日时间点」。
        if self._upload_strategy() == "interval":
            days = self._upload_interval_days()
            if days <= 0:
                logger.info(
                    "定时上传：上传间隔为 0（随爬取立即上传），本次检查点清一遍待上传文件"
                )
                self._run_upload_job(immediate=True)
                return
            due, due_at = self._upload_due()
            if not due:
                anchor = self._last_upload_datetime()
                logger.info(
                    "定时上传跳过：每 %s 天统一上传，上次上传 %s，下次不早于 %s",
                    days,
                    anchor.strftime("%Y-%m-%d %H:%M:%S") if anchor else "—",
                    due_at.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return
            logger.info(
                "定时上传：距上次上传已满 %s 天，上传全部未上传文件（不看文件年龄）",
                days,
            )
            self._run_upload_job(immediate=True)
            return
        if self.mode == "test":
            self._run_upload_job(immediate=True)
        else:
            self._run_upload_job(immediate=False)

    def _run_upload_job(self, *, immediate: bool) -> None:
        """按当前是否在爬取决定同步/异步执行上传。"""
        if self._crawl_running:
            try:
                self.start_upload(allow_during_crawl=True, immediate=immediate)
                logger.info("上传任务已启动，与爬取并行执行")
            except RuntimeError as exc:
                logger.warning("定时上传未启动：%s", exc)
            return
        if immediate:
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
        self.schedule_throttle_retry()
        self.schedule_failed_retry()

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
        if (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
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
    ) -> dict[str, int]:
        self._begin_run_log()
        self._last_crawl_results = {"ccdi": 0, "bit": 0}
        try:
            results = run_crawl_jobs(
                self.state,
                self.config,
                source=source,
                max_items=max_items,
                stop_event=stop_event,
                pause_event=pause_event,
                parallel=parallel,
                categories=categories,
                retry_probe=self._retry_probe,
            )
            self._last_crawl_results = {
                "ccdi": int((results or {}).get("ccdi", 0)),
                "bit": int((results or {}).get("bit", 0)),
            }
            return self._last_crawl_results
        finally:
            self._end_run_log()

    # ------------------------------------------------------------------
    # 每次爬取一个日志文件：<开始>至<结束>.log
    # ------------------------------------------------------------------
    def _begin_run_log(self) -> None:
        if not self.run_log.enabled:
            return
        try:
            report = self.run_log.check_previous()
            errors = int(report.get("errors") or 0)
            if errors:
                names = report.get("files") or []
                shown = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
                self.state.set_alert(
                    RUN_LOG_ALERT_KEY,
                    f"昨日（{report.get('date', '')}）爬取日志有 {errors} 处 ERROR"
                    f"：{shown}（已保留，目录 {self.run_log.directory}）",
                    level="error",
                )
            else:
                self.state.clear_alert(RUN_LOG_ALERT_KEY)
            removed = self.run_log.prune()
            if removed:
                logger.info(
                    "已清理 %s 份过期日志（保留 %s 天、无 ERROR 的才删）：%s",
                    len(removed),
                    self.run_log.keep_days,
                    "、".join(removed[:3]) + ("…" if len(removed) > 3 else ""),
                )
            path = self.run_log.start()
            if path:
                logger.info("本次爬取日志：%s", path.name)
        except Exception:
            logger.exception("初始化每日运行日志失败（不影响爬取）")

    def _end_run_log(self) -> None:
        if not self.run_log.enabled:
            return
        try:
            path = self.run_log.finish()
            if path:
                logger.info("本次爬取结束，日志：%s", path.name)
        except Exception:
            logger.exception("收尾每日运行日志失败")

    def _upload_blocking(
        self,
        immediate: bool,
        stop_event: threading.Event | None,
        pause_event: threading.Event | None = None,
    ) -> None:
        self.state.clear_alert("upload_error")
        run_upload_jobs(
            self.state,
            self.config,
            immediate=immediate,
            stop_event=stop_event,
            pause_event=pause_event,
        )

    def crawl_sync(self, source: str = "all", max_items: int | None = None) -> None:
        """同步执行爬取（定时任务 / 首次运行 / 命令行用）。"""
        if (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
            logger.warning("爬取跳过：已有任务运行中")
            return
        first_time = self.state.is_first_run() or not bool(
            self.state.snapshot().get("documents")
        )
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
        if not self._stop_event.is_set():
            self._post_crawl_maintenance_and_upload(
                source, was_first_run=first_time
            )

    def _after_crawl(self, source: str) -> None:
        """一轮爬取结束后的收尾：安排跳过分类重试、判断首次运行是否完成。"""
        self._retry_round = 0
        self.schedule_skipped_retry()
        # id_walk 方式没有"被跳过的子分类"，限流结束时靠这里安排 30 分钟后的自动重试
        self.schedule_throttle_retry()
        # 有失败编号就开启失败批量重试（30 分钟一轮，最多 3 轮）
        self.schedule_failed_retry()
        if not self.state.is_first_run():
            return
        need = {"ccdi", "bit"} if source == "all" else {source}
        # 兜底：中纪检已凑够库总量也算跑完（旧版本可能没写 pass_done）
        if "ccdi" in need and not self.state.is_pass_done("ccdi"):
            snap = self.state.snapshot()
            docs = snap.get("documents") or {}
            ccdi_count = sum(1 for d in docs.values() if d.get("source") == "ccdi")
            total = int(
                ((snap.get("total_estimate") or {}).get("ccdi") or {}).get("value")
                or 0
            )
            if total and ccdi_count >= total:
                self.state.mark_pass_done("ccdi")
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
        # 首次运行结束：清掉所有爬取重试任务与遗留的失败编号，断点不再参与续爬
        self.cancel_skipped_retry(reason="首次运行已完成")
        self.cancel_throttle_retry(reason="首次运行已完成")
        self.cancel_failed_retry()
        rec = self.state.get_id_walk_position("ccdi") or {}
        if rec:
            rec.update({"failed_ids": [], "failed_since": "", "turn_back_done": True})
            last = int(rec.get("last_id") or 0)
            rec["scanned_max"] = max(int(rec.get("scanned_max") or 0), last)
            self.state.set_id_walk_position(
                "ccdi", last, **{k: v for k, v in rec.items() if k != "last_id"}
            )
        logger.info("首次运行已完整结束（%s 都跑完了），已清除「首次执行」标记", "、".join(sorted(need)))

    def _ccdi_id_walk_pending(self) -> bool:
        """按编号爬取时，编号还没扫到上限就返回 True。"""
        cfg = self.config.get("crawl", {}) or {}
        if str(cfg.get("crawl_mode") or "id_walk").strip().lower() != "id_walk":
            return False
        # 双向扫描已跑完（turn_back_done）就算全量结束——断点游标可能被重试写成
        # 很小的编号（例如 6528），不能只凭 last_id 判断
        if (self.state.get_id_walk_position("ccdi") or {}).get("turn_back_done"):
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

    # ------------------------------------------------------------------
    # 持续限流：退避 3 轮仍失败后，隔一段时间自动重新开一轮
    # ------------------------------------------------------------------
    def _throttle_retry_minutes(self) -> int:
        cfg = self.config.get("crawl", {}) or {}
        try:
            return max(0, int(cfg.get("throttle_retry_minutes", 30) or 0))
        except (TypeError, ValueError):
            return 30

    def _throttle_alert_active(self) -> bool:
        return any(
            a.get("key") == THROTTLE_ALERT_KEY
            for a in self.state.list_alerts()
        )

    def next_throttle_retry_at(self) -> str | None:
        throttle = self.state.get_throttle_state("ccdi") or {}
        if throttle.get("context") == "date_backfill":
            return None
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job(THROTTLE_RETRY_JOB_ID)
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time.strftime("%Y-%m-%dT%H:%M:%S")

    def cancel_throttle_retry(self, *, reason: str = "") -> None:
        if self._scheduler is None:
            return
        if self._scheduler.get_job(THROTTLE_RETRY_JOB_ID):
            self._scheduler.remove_job(THROTTLE_RETRY_JOB_ID)
            logger.info(
                "已取消待执行的限流自动重试%s", f"（{reason}）" if reason else ""
            )

    def schedule_throttle_retry(self, *, minutes: int | None = None) -> None:
        """中纪检持续限流结束时，安排若干分钟后再跑一轮（续爬同一位置）。"""
        if not self._throttle_alert_active():
            self.cancel_throttle_retry()
            return
        if self._stop_event.is_set():
            # 用户主动点「终止」的，不自动重试
            return
        if self._scheduler is None:
            return
        delay = self._throttle_retry_minutes() if minutes is None else max(0, minutes)
        if delay <= 0:
            logger.info(
                "中纪检持续限流；自动重试已关闭（crawl.throttle_retry_minutes=0）"
            )
            return
        run_at = datetime.now() + timedelta(minutes=delay)
        self._scheduler.add_job(
            self.retry_throttled_ccdi,
            DateTrigger(run_date=run_at),
            id=THROTTLE_RETRY_JOB_ID,
            replace_existing=True,
        )
        # 把具体重试时间写进顶部横幅，界面上能直接看到
        self.state.set_alert(
            THROTTLE_ALERT_KEY,
            f"{THROTTLE_ALERT_TEXT.rstrip('。')}。"
            f"将在 {run_at.strftime('%Y-%m-%d %H:%M:%S')} 自动重试。",
            level="error",
        )
        logger.info(
            "已安排中纪检限流自动重试：%s（%s 分钟后）",
            run_at.strftime("%Y-%m-%d %H:%M:%S"),
            delay,
        )

    def retry_throttled_ccdi(self) -> None:
        """定时触发：限流结束后的自动重试（从中纪检断点续爬）。"""
        if self._crawl_running or self._upload_running or self._migration_running:
            logger.warning("限流自动重试推迟：当前有任务运行中，10 分钟后再试")
            self.schedule_throttle_retry(minutes=10)
            return
        logger.info("限流自动重试：重新开始中纪检爬取")
        self._stop_event.clear()
        self._pause_event.clear()
        self._crawl_running = True
        try:
            self._run_crawl_with_upload("ccdi", async_upload=True)
        except Exception:
            logger.exception("限流自动重试失败")
        finally:
            self._crawl_running = False

    # ------------------------------------------------------------------
    # 失败编号（failed_ids）批量重试：30 分钟一轮，最多 3 轮
    # ------------------------------------------------------------------
    def reconcile_startup(self) -> bool:
        """启动对账：两个来源都齐了就直接清掉「首次执行」与所有续爬/重试状态。"""
        snap = self.state.snapshot()
        docs = snap.get("documents") or {}
        ccdi = sum(1 for d in docs.values() if d.get("source") == "ccdi")
        bit = sum(1 for d in docs.values() if d.get("source") == "bit")
        ccdi_total = int(
            ((snap.get("total_estimate") or {}).get("ccdi") or {}).get("value") or 0
        )
        # 重新按页码算一遍中纪检总数（缓存值可能已经过时）
        crawl_cfg = self.config.get("crawl", {}) or {}
        try:
            with PlaywrightPdfExporter(
                headless=bool(crawl_cfg.get("playwright_headless", True)),
                timeout_ms=int(crawl_cfg.get("page_load_timeout_ms", 120_000)),
                pdf_scale=float(crawl_cfg.get("pdf_scale_ccdi", 1.0)),
                browser_channel=crawl_cfg.get("browser_channel") or None,
                stealth=bool(crawl_cfg.get("stealth", True)),
                user_agents=list(DEFAULT_USER_AGENTS),
            ) as exporter:
                fresh = _estimate_all_data_total(exporter)
            if fresh:
                ccdi_total = int(fresh)
                self.state.set_total_estimate("ccdi", ccdi_total)
                logger.info("启动对账：中纪检总数重算为 %s 条", ccdi_total)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动对账：中纪检总数重算失败（改用缓存值 %s）：%s", ccdi_total, exc)
        bit_total = None
        try:
            bit_total = _parse_declared_total(_fetch_html(BIT_INDEX))
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动对账：校纪检目录读取失败：%s", exc)
        if bit_total is None:
            # 读不到就退回上次记录的总数，避免因为一次网络抖动导致对账不通过
            bit_total = self.state.get_source_total("bit") or 0
        logger.info(
            "启动对账：中纪检 %s/%s，校纪检 %s/%s",
            ccdi,
            ccdi_total or "?",
            bit,
            bit_total if bit_total is not None else "?",
        )
        if not (ccdi_total and bit_total and ccdi >= ccdi_total and bit >= bit_total):
            return False
        self.state.set_first_run(False)
        self.state.reset_pass_done()
        self.cancel_skipped_retry(reason="启动对账已完成")
        self.cancel_throttle_retry(reason="启动对账已完成")
        self.cancel_failed_retry()
        rec = self.state.get_id_walk_position("ccdi") or {}
        if rec:
            rec.update({"failed_ids": [], "failed_since": "", "turn_back_done": True})
            last = int(rec.get("last_id") or 0)
            rec["scanned_max"] = max(int(rec.get("scanned_max") or 0), last)
            self.state.set_id_walk_position(
                "ccdi", last, **{k: v for k, v in rec.items() if k != "last_id"}
            )
        logger.info("启动对账：数据已齐全，已清除「首次执行」标记与全部续爬/重试状态")
        return True

    def _failed_ids(self) -> list[int]:
        rec = self.state.get_id_walk_position("ccdi") or {}
        out: list[int] = []
        for item in rec.get("failed_ids") or []:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        if out:
            # 已经达到库总量：遗留的失败编号多半是无效网址，不再安排重试
            snap = self.state.snapshot()
            docs = snap.get("documents") or {}
            count = sum(1 for d in docs.values() if d.get("source") == "ccdi")
            total = int(
                ((snap.get("total_estimate") or {}).get("ccdi") or {}).get("value")
                or 0
            )
            if total and count >= total:
                return []
        return sorted(set(out))

    def _retry_probe(self) -> list[int]:
        """主循环每个编号前调用：有抢占式重试请求就返回失败编号列表。"""
        if not self._retry_interrupt.is_set():
            return []
        self._retry_interrupt.clear()
        return self._failed_ids()

    def next_failed_retry_at(self) -> str | None:
        return self._next_run_at(FAILED_RETRY_JOB_ID)

    def cancel_failed_retry(self) -> None:
        if self._scheduler is not None and self._scheduler.get_job(FAILED_RETRY_JOB_ID):
            self._scheduler.remove_job(FAILED_RETRY_JOB_ID)

    def schedule_failed_retry(
        self, *, minutes: int | None = None, from_now: bool = False
    ) -> None:
        """有失败编号就安排 30 分钟后的批量重试；没有就关掉开关。"""
        ids = self._failed_ids()
        if not ids:
            self.cancel_failed_retry()
            self._failed_round = 0
            self.state.clear_alert(FAILED_ALERT_KEY)
            return
        if self._scheduler is None:
            return
        if self._scheduler.get_job(FAILED_RETRY_JOB_ID):
            return
        if self._failed_round >= FAILED_RETRY_MAX_ROUNDS:
            logger.warning(
                "失败编号已重试 %s 轮仍未成功，停止自动重试：%s",
                self._failed_round,
                ids[:10],
            )
            self._mark_failed_ids_alert(ids)
            return
        delay = FAILED_RETRY_MINUTES if minutes is None else max(0, minutes)
        if from_now:
            run_at = datetime.now() + timedelta(minutes=delay)
        else:
            rec = self.state.get_id_walk_position("ccdi") or {}
            try:
                base = datetime.fromisoformat(str(rec.get("failed_since") or ""))
            except (TypeError, ValueError):
                base = datetime.now()
            run_at = base + timedelta(minutes=delay)
            if run_at <= datetime.now():
                run_at = datetime.now() + timedelta(minutes=1)
        self._scheduler.add_job(
            self.retry_failed_ids_job,
            DateTrigger(run_date=run_at),
            id=FAILED_RETRY_JOB_ID,
            replace_existing=True,
        )
        logger.info(
            "已安排失败编号重试：%s（%s 个，第 %s/%s 轮）",
            run_at.strftime("%Y-%m-%d %H:%M:%S"),
            len(ids),
            self._failed_round + 1,
            FAILED_RETRY_MAX_ROUNDS,
        )

    def _mark_failed_ids_alert(self, ids: list[int]) -> None:
        shown = "、".join(str(i) for i in ids[:10]) + ("…" if len(ids) > 10 else "")
        self.state.set_alert(
            FAILED_ALERT_KEY,
            f"中纪检有 {len(ids)} 个编号连续 {FAILED_RETRY_MAX_ROUNDS} 轮重试仍失败：{shown}"
            "（可点「重试失败编号」手动再试）",
            level="error",
        )

    def retry_failed_ids_job(self) -> None:
        """定时触发：批量重试所有失败编号。"""
        ids = self._failed_ids()
        if not ids:
            self.cancel_failed_retry()
            return
        if self._crawl_running or self._upload_running or self._migration_running:
            # 抢占式：让正在爬取的主循环在下个编号前插入失败编号重试
            logger.info("失败编号重试：通知正在运行的主扫描插队处理")
            self._retry_interrupt.set()
            return
        self._failed_round += 1
        logger.info(
            "开始第 %s/%s 轮失败编号重试：%s 个（%s）",
            self._failed_round,
            FAILED_RETRY_MAX_ROUNDS,
            len(ids),
            ids[:10],
        )
        self._run_failed_retry_blocking(ids)
        remaining = self._failed_ids()
        if not remaining:
            logger.info("失败编号已全部重试成功，关闭失败重试开关")
            self._failed_round = 0
            self.state.clear_alert(FAILED_ALERT_KEY)
            self.cancel_failed_retry()
            return
        if self._failed_round >= FAILED_RETRY_MAX_ROUNDS:
            logger.warning("失败编号连续 %s 轮仍未成功：%s", self._failed_round, remaining)
            self._mark_failed_ids_alert(remaining)
            return
        self.schedule_failed_retry()

    def _run_failed_retry_blocking(self, ids: list[int]) -> None:
        was_first = self.state.is_first_run()
        self._stop_event.clear()
        self._pause_event.clear()
        self._crawl_running = True
        try:
            run_crawl_jobs(
                self.state,
                self.config,
                source="ccdi",
                stop_event=self._stop_event,
                pause_event=self._pause_event,
                retry_ids=ids,
            )
        except Exception:
            logger.exception("失败编号重试任务异常")
        finally:
            self._crawl_running = False
        # 重试补充了新文件时，同样先补日期、建索引，再上传。
        self._after_crawl("ccdi")
        if not self._stop_event.is_set():
            self._post_crawl_maintenance_and_upload(
                "ccdi", was_first_run=was_first
            )

    def start_failed_retry_now(self) -> dict:
        """UI「重试失败编号」：立即异步重试，结束后重新安排 30 分钟后的下一次。"""
        ids = self._failed_ids()
        if not ids:
            raise RuntimeError("当前没有失败编号")
        if self._crawl_running or self._upload_running or self._migration_running:
            # 抢占式：不另起任务，让主循环插队重试，重试完自动回到原编号
            self._retry_interrupt.set()
            logger.info("已通知正在运行的主扫描：插入失败编号重试")
            return {"started": True, "queued": True, "count": len(ids)}
        self.cancel_failed_retry()

        def worker() -> None:
            was_first = self.state.is_first_run()
            self._crawl_running = True
            try:
                run_crawl_jobs(
                    self.state,
                    self.config,
                    source="ccdi",
                    stop_event=self._stop_event,
                    pause_event=self._pause_event,
                    retry_ids=ids,
                )
            except Exception:
                logger.exception("手动重试失败编号异常")
            finally:
                self._crawl_running = False
            self._after_crawl("ccdi")
            if not self._stop_event.is_set():
                self._post_crawl_maintenance_and_upload(
                    "ccdi", was_first_run=was_first
                )
            remaining = self._failed_ids()
            if not remaining:
                self._failed_round = 0
                self.state.clear_alert(FAILED_ALERT_KEY)
                return
            self.schedule_failed_retry(from_now=True)

        self._stop_event.clear()
        self._pause_event.clear()
        self._crawl_running = True
        threading.Thread(target=worker, daemon=True, name="failed-retry").start()
        return {"started": True, "count": len(ids)}

    def upload_sync(self) -> None:
        """同步执行定时上传（按保留天数）。"""
        if (
            self._upload_running
            or self._crawl_running
            or self._migration_running
            or self._index_building
        ):
            logger.warning("上传跳过：已有任务运行中")
            return
        if not self._version_index_is_ready():
            logger.warning("上传跳过：版本索引尚未就绪")
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
        if (
            self._upload_running
            or self._crawl_running
            or self._migration_running
            or self._index_building
        ):
            logger.warning("立即上传跳过：已有任务运行中")
            return
        if not self._version_index_is_ready():
            logger.warning("立即上传跳过：版本索引尚未就绪")
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
        try:
            self._crawl_blocking(
                source, max_items, self._stop_event, self._pause_event, self.parallel
            )
        except Exception:
            logger.exception("爬取失败")
        finally:
            self._crawl_running = False
        # 本次爬取若跳过了中纪检子分类，按配置的间隔安排自动重试
        self._after_crawl(source)
        if self._stop_event.is_set():
            logger.info("爬取已终止，跳过日期补全、索引和上传")
            return
        self._post_crawl_maintenance_and_upload(
            source, was_first_run=first_time
        )

    def crawl_now_sync(self, source: str = "all") -> None:
        """命令行 --crawl-now：同步跑一次，行为与界面点「开始」一致。"""
        if (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
            logger.warning("爬取跳过：已有任务运行中")
            return
        self._run_crawl_with_upload(source, async_upload=False)

    def start_crawl(self, source: str = "all") -> dict:
        """UI 触发：异步执行爬取；结束后按设置决定是否自动上传。"""
        if (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
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

    def start_upload(
        self, *, allow_during_crawl: bool = False, immediate: bool = True
    ) -> dict:
        """异步上传；默认只允许空闲时（UI 的「立即上传」）。

        allow_during_crawl=True 时允许与爬取并行——定时上传到点就要传，
        不能因为爬取还没结束就整体跳过。
        """
        if (
            self._upload_running
            or self._migration_running
            or self._index_building
            or (self._crawl_running and not allow_during_crawl)
        ):
            raise RuntimeError("已有任务运行中")
        if not self._version_index_is_ready():
            raise RuntimeError("版本索引尚未就绪，暂时不能上传")
        if not self._crawl_running:
            # 爬取在跑时不要动这两个事件，否则会把用户暂停的任务"顺带"继续
            self._stop_event.clear()
            self._pause_event.clear()
        elif self._stop_event.is_set():
            raise RuntimeError("当前任务已终止")
        self._upload_running = True

        def worker() -> None:
            try:
                self._upload_blocking(immediate, self._stop_event, self._pause_event)
            except Exception as exc:
                logger.exception("UI 触发的上传失败")
                reason = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:400]
                self.state.set_progress(
                    "upload", status="error", message=f"上传异常：{reason}"
                )
                self.state.set_alert(
                    "upload_error", f"上传异常中断：{reason}", level="error"
                )
            finally:
                self._upload_running = False

        threading.Thread(target=worker, daemon=True, name="ui-upload").start()
        return {"started": True}

    def stop_all(self) -> dict:
        """终止所有任务（爬取 / 上传，含自动上传），并取消待执行的中纪检跳过分类重试。"""
        self.cancel_skipped_retry(reason="已终止所有任务")
        self.cancel_throttle_retry(reason="已终止所有任务")
        self.cancel_failed_retry()
        if not (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
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

    def request_stop(self) -> None:
        """外部信号（systemd stop / 关机）要求停止：解除暂停并置停止位。"""
        self._pause_event.clear()
        self._stop_event.set()

    def pause(self) -> dict:
        """暂停当前运行中的任务。"""
        if not (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
            return {"paused": False, "message": "当前没有运行中的任务"}
        self._pause_event.set()
        return {"paused": True, "message": "任务已暂停"}

    def resume(self) -> dict:
        """继续已暂停的任务。"""
        if not (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
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
        if self._crawl_running or self._upload_running or self._migration_running:
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
                if not (
                    self._crawl_running
                    or self._upload_running
                    or self._migration_running
                    or self._index_building
                ):
                    logger.info("测试模式间隔触发：开始爬取（%s）", self.test_source)
                    self.crawl_sync(
                        source=self.test_source, max_items=self._max_items_for_mode()
                    )
            if self._next_upload_at and now >= self._next_upload_at:
                self._next_upload_at = self._compute_next(self.upload_interval_minutes)
                if not (
                    self._crawl_running
                    or self._upload_running
                    or self._migration_running
                    or self._index_building
                ):
                    logger.info("测试模式间隔触发：开始上传")
                    self.upload_now_sync()

    def agent_status(self) -> dict:
        """查询学科智能体连接状态与两个知识库的文档状态统计。"""
        return get_agent_status(self.config, self.state)

    def _persist_dataset_ids(self, ccdi_id: str, bit_id: str) -> None:
        rag = self.config.setdefault("ragflow", {})
        rag["dataset_id_ccdi"] = ccdi_id
        rag["dataset_id_bit"] = bit_id
        if self.config_path:
            try:
                _update_ragflow_in_config(
                    self.config_path,
                    {"dataset_id_ccdi": ccdi_id, "dataset_id_bit": bit_id},
                )
            except OSError as exc:
                logger.warning(
                    "新库 ID 已保存到内存，但写回 config.yaml 失败：%s", exc
                )

    def _migration_blocking(self) -> None:
        """迁库后台任务：确认日期与最终索引就绪后再重建并上传。"""
        rag_cfg = self.config.get("ragflow", {}) or {}
        if not rag_cfg.get("base_url") or not rag_cfg.get("api_key"):
            raise RuntimeError("未配置学科智能体地址或 API Key")
        prepared = self.prepare_version_index_on_startup()
        if not prepared.get("ready"):
            raise RuntimeError(
                prepared.get("message")
                or "日期或版本索引尚未就绪，已停止迁库"
            )
        if self._stop_event.is_set():
            raise RuntimeError("迁库准备已终止，原知识库未删除")
        uploader = RagflowKbUploader(
            str(rag_cfg.get("base_url") or ""),
            str(rag_cfg.get("api_key") or ""),
        )
        result = migrate_dataset_libraries(
            self.state,
            self.config,
            uploader,
            stop_event=self._stop_event,
            pause_event=self._pause_event,
            dataset_created_callback=self._persist_dataset_ids,
        )
        if result.get("stopped"):
            message = "新库已创建，但重新上传已终止；点击「立即上传」可继续"
            self.state.set_progress("upload", status="idle", message=message)
            self.state.set_alert(
                "dataset_migration", message, level="warning"
            )
            logger.warning(message)
            return
        message = (
            "迁库完成：中纪检 {} 条、校纪检 {} 条最新文件已重新上传".format(
                result.get("uploaded", {}).get("ccdi", 0),
                result.get("uploaded", {}).get("bit", 0),
            )
        )
        self.state.set_progress(
            "upload", status="idle", message=message
        )
        self.state.clear_alert("dataset_migration")
        logger.info(message)

    def start_dataset_migration(self) -> dict:
        """UI 二次确认后触发迁库；与爬取、上传、定时上传互斥。"""
        if (
            self._crawl_running
            or self._upload_running
            or self._migration_running
            or self._index_building
        ):
            raise RuntimeError("已有任务运行中，无法迁库")
        self.state.reload()
        progress = self.state.snapshot().get("progress") or {}
        if any(
            (progress.get(key) or {}).get("status") == "running"
            for key in ("ccdi", "bit", "upload")
        ):
            raise RuntimeError("状态文件显示仍有任务运行中，无法迁库")
        self._stop_event.clear()
        self._pause_event.clear()
        self._migration_running = True

        def worker() -> None:
            try:
                self._migration_blocking()
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:400]
                logger.exception("迁库失败")
                self.state.set_dataset_migration(
                    status="error",
                    error=reason,
                    finished_at=datetime.now().isoformat(),
                )
                self.state.set_progress(
                    "upload", status="error", message=f"迁库失败：{reason}"
                )
                self.state.set_alert(
                    "dataset_migration",
                    f"迁库失败：{reason}",
                    level="error",
                )
            finally:
                self._migration_running = False

        threading.Thread(
            target=worker, daemon=True, name="dataset-migration"
        ).start()
        return {"started": True, "message": "迁库任务已启动"}

    def set_agent_config(
        self,
        *,
        base_url: str,
        api_key: str,
        dataset_name_ccdi: str,
        dataset_name_bit: str,
    ) -> dict:
        """修改学科智能体地址、API Key 与两个库名称，并写回配置文件。"""
        if self._crawl_running or self._upload_running or self._migration_running:
            raise RuntimeError("已有任务运行中，无法修改学科智能体配置")
        rag = self.config.setdefault("ragflow", {})
        old_base_url = str(rag.get("base_url") or "").rstrip("/")
        old_names = (rag.get("dataset_name_ccdi"), rag.get("dataset_name_bit"))
        rag["base_url"] = (base_url or "").strip().rstrip("/")
        rag["api_key"] = (api_key or "").strip()
        rag["dataset_name_ccdi"] = (dataset_name_ccdi or "").strip() or "中纪检"
        rag["dataset_name_bit"] = (dataset_name_bit or "").strip() or "校纪检"
        # 库名变了就不能继续沿用旧库 ID，否则上传会写进原库。
        if (
            old_base_url != rag["base_url"]
            or old_names[0] != rag["dataset_name_ccdi"]
            or old_names[1] != rag["dataset_name_bit"]
        ):
            rag["dataset_id_ccdi"] = ""
            rag["dataset_id_bit"] = ""
        if self.config_path:
            _update_ragflow_in_config(
                self.config_path,
                {
                    "base_url": rag["base_url"],
                    "api_key": rag["api_key"],
                    "dataset_name_ccdi": rag["dataset_name_ccdi"],
                    "dataset_name_bit": rag["dataset_name_bit"],
                    "dataset_id_ccdi": rag.get("dataset_id_ccdi", ""),
                    "dataset_id_bit": rag.get("dataset_id_bit", ""),
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
    _setup_logging(config)

    output_dir = Path(config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "crawler_state.json"
    state = CrawlerState(state_path)
    controller = JobController(state, config)
    controller.config_path = config_path
    _install_signal_handlers(controller)

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

    # 先保留磁盘上的重跑意图。启动对账可能因“总数已齐”清除 first_run_pending，
    # 但这一次 reload 仍须按首次运行强制执行完整年份扫描。
    first_run_requested = state.is_first_run()
    # 首次运行标记：一旦开始就持久化，只有整轮爬完（两个来源都跑完）才清除。
    # 所以首次运行中途被中断，重启后仍然按"首次运行"继续。
    # 启动先对账：两个来源都已经齐全时，直接清掉首次执行标记与续爬/重试状态。
    try:
        controller.reconcile_startup()
    except Exception:
        logger.exception("启动对账失败（不影响正常运行）")
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

    controller.start_interval_scheduler()
    scheduler = None
    if not args.no_schedule or not args.no_ui:
        scheduler = start_scheduler(state, config, controller)
    else:
        logger.info("已跳过定时任务（--no-schedule --no-ui：单次执行模式）")

    # 首次全量运行必须先完成爬取，再进入日期补全、索引和上传。
    defer_maintenance = first_run and not args.no_auto_start
    if defer_maintenance:
        flags = state.get_maintenance_flags()
        index_ready = all(bool(flags.get(k)) for k in ("ccdi", "bit"))
        logger.info("首次运行：日期补全与索引将在全量爬取完成后自动执行")
    else:
        index_result = controller.prepare_version_index_on_startup(
            force_full_dates=first_run_requested
        )
        index_ready = bool(index_result.get("ready"))
        if not index_ready:
            logger.error("启动索引准备未完成：%s", index_result.get("message"))

    ran_work = False
    try:
        if not index_ready and not defer_maintenance:
            logger.error("版本索引未就绪，本次启动跳过自动爬取与上传")
        elif first_run and not args.no_auto_start:
            logger.info("首次运行：全量爬取，结束后补日期、建索引并上传")
            controller.mark_first_run(True)
            controller.crawl_sync()
            ran_work = True
        else:
            if args.crawl_now:
                controller.crawl_now_sync(source=args.source)
                ran_work = True
            if args.upload_now:
                controller.upload_now_sync()
                ran_work = True
    except KeyboardInterrupt:
        # systemd stop / 关机发的 SIGTERM 也走这里：停止位已置上，直接退出
        logger.info("收到停止信号，已中断当前任务，进程退出")
        return

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
        logger.info("收到退出信号，正在关闭…")
        controller.request_stop()
        if scheduler:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
