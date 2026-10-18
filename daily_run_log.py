"""每日爬取运行日志：每次爬取单独一个文件，文件名 = 开始时间至结束时间。

- 开始爬取时创建 `<开始>.log` 并挂到根 logger 上；
- 爬取结束（正常/异常/被终止）时关闭并改名为 `<开始>至<结束>.log`；
- 下一次爬取开始前检查上一份日志里有没有 ERROR：
  有 -> 报告给界面并永久保留；没有 -> 超过 keep_days 天后删除。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

TIME_FMT = "%Y-%m-%d-%H-%M-%S"
SEP = "至"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def _parse_start(path: Path) -> datetime | None:
    """从文件名里取开始时间（`2026-10-18-09-00-00至....log`）。"""
    head = path.stem.split(SEP, 1)[0]
    try:
        return datetime.strptime(head, TIME_FMT)
    except ValueError:
        return None


def _scan_errors(path: Path) -> int:
    """统计文件里的 ERROR 行数（顺带识别未捕获异常的 traceback）。"""
    count = 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "[ERROR]" in line or "Traceback (most recent call last)" in line:
                    count += 1
    except OSError:
        return 0
    return count


class DailyRunLog:
    """为每次爬取维护一个独立的日志文件。"""

    def __init__(self, directory: Path, *, keep_days: int = 7, enabled: bool = True):
        self.directory = Path(directory)
        self.keep_days = max(1, int(keep_days))
        self.enabled = bool(enabled)
        self._handler: logging.FileHandler | None = None
        self._pending: Path | None = None
        self._started_at: datetime | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> Path | None:
        """开始一次爬取：创建日志文件并挂到根 logger。"""
        if not self.enabled:
            return None
        with self._lock:
            if self._handler is not None:
                return self._pending
            self.directory.mkdir(parents=True, exist_ok=True)
            self._started_at = datetime.now()
            self._pending = self.directory / f"{self._started_at.strftime(TIME_FMT)}.log"
            handler = logging.FileHandler(self._pending, encoding="utf-8")
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            logging.getLogger().addHandler(handler)
            self._handler = handler
            return self._pending

    def finish(self) -> Path | None:
        """结束本次爬取：关闭并改名为 `<开始>至<结束>.log`。"""
        with self._lock:
            if self._handler is None or self._pending is None:
                return None
            root = logging.getLogger()
            try:
                root.removeHandler(self._handler)
                self._handler.close()
            finally:
                self._handler = None
            ended_at = datetime.now()
            target = self.directory / (
                f"{self._started_at.strftime(TIME_FMT)}{SEP}"
                f"{ended_at.strftime(TIME_FMT)}.log"
            )
            try:
                self._pending.replace(target)
            except OSError:
                return self._pending
            finally:
                self._pending = None
            return target

    # ------------------------------------------------------------------
    # 上一次日志检查 / 过期清理
    # ------------------------------------------------------------------
    def list_logs(self) -> list[Path]:
        if not self.directory.exists():
            return []
        items = [p for p in self.directory.glob("*.log") if _parse_start(p)]
        return sorted(items, key=lambda p: _parse_start(p) or datetime.min)

    def check_previous(self) -> dict:
        """检查「昨天」这一天的所有日志（按文件名的开始日期判断）。"""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = today - timedelta(days=1)
        hits: list[tuple[str, int]] = []
        total = 0
        for path in self.list_logs():
            started = _parse_start(path)
            if started is None or not (since <= started < today):
                continue
            errors = _scan_errors(path)
            if errors:
                hits.append((path.name, errors))
                total += errors
        return {
            "date": since.strftime("%Y-%m-%d"),
            "files": [name for name, _ in hits],
            "errors": total,
        }

    def prune(self) -> list[str]:
        """删除超过 keep_days 且没有 ERROR 的日志；有 ERROR 的一律保留。"""
        cutoff = datetime.now() - timedelta(days=self.keep_days)
        removed: list[str] = []
        for path in self.list_logs():
            started = _parse_start(path)
            if started is None or started >= cutoff:
                continue
            if _scan_errors(path) > 0:
                continue
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                continue
        return removed
