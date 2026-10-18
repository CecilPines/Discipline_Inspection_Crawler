"""爬虫与上传任务的全局状态（JSON 持久化，供 UI 与定时任务读取）。"""

from __future__ import annotations

import json
import threading
from functools import wraps
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

# 可重入锁：所有读写 self._data 的方法都在锁内执行，
# 避免 UI 线程 deepcopy 时爬虫/上传线程正在往字典里加记录
# （RuntimeError: dictionary changed size during iteration）。
_lock = threading.RLock()


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with _lock:
            return method(self, *args, **kwargs)

    return wrapper


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class CrawlerState:
    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self._data: dict[str, Any] = self._default_data()
        self._loaded_mtime: float | None = None
        self.load()

    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "last_crawl_time": {"ccdi": None, "bit": None},
            "last_upload_time": {"ccdi": None, "bit": None},
            "daily_new_count": {},
            "progress": {
                "ccdi": {"status": "idle", "current": 0, "total": 0, "message": ""},
                "bit": {"status": "idle", "current": 0, "total": 0, "message": ""},
                "upload": {"status": "idle", "current": 0, "total": 0, "message": ""},
            },
            "alerts": {},
            "skipped_categories": {},
            "crawl_cursor": {},
            "category_status": {},
            "throttle_state": {},
            "pass_done": {},
            "total_estimate": {},
            "first_run_pending": False,
            "documents": {},
        }

    @_locked
    def load(self) -> None:
        if not self.state_path.exists():
            self.save()
            return
        keep = self._data
        with _lock:
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {**self._default_data(), **raw}
            except (json.JSONDecodeError, OSError):
                # 读取失败（多半是另一个进程正在写盘）：保留内存里的数据，
                # 绝不回退成空状态——否则会丢掉已登记的文档记录
                self._data = keep
        self._loaded_mtime = self._mtime()

    def _mtime(self) -> float | None:
        try:
            return self.state_path.stat().st_mtime
        except OSError:
            return None

    @_locked
    def reload(self) -> None:
        """若状态文件被其它进程（例如终端里的单次任务）改过，则重新载入。

        供 UI 实时显示由终端命令触发的任务进度。
        """
        mtime = self._mtime()
        if mtime is None or mtime == self._loaded_mtime:
            return
        self.load()

    @_locked
    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        self._loaded_mtime = self._mtime()

    @_locked
    def snapshot(self) -> dict[str, Any]:
        with _lock:
            return deepcopy(self._data)

    @_locked
    def set_progress(
        self,
        source: str,
        *,
        status: str | None = None,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        note: str | None = None,
        extra: dict[str, Any] | None = None,
        cursor: dict[str, Any] | None = None,
    ) -> None:
        """更新进度。cursor 给定时顺带刷新续爬位置（与进度共用同一次落盘）。"""
        prog = self._data.setdefault("progress", {}).setdefault(
            source, {"status": "idle", "current": 0, "total": 0, "message": ""}
        )
        if status is not None:
            prog["status"] = status
        if current is not None:
            prog["current"] = current
        if total is not None:
            prog["total"] = total
        if message is not None:
            prog["message"] = message
        if note is not None:
            prog["note"] = note
        if extra:
            prog.update(extra)
        if cursor is not None:
            self._set_cursor(source, **cursor)
        self.save()

    @_locked
    def set_last_crawl(self, source: str) -> None:
        self._data.setdefault("last_crawl_time", {})[source] = _now_iso()
        self.save()

    @_locked
    def set_last_upload(self, source: str) -> None:
        self._data.setdefault("last_upload_time", {})[source] = _now_iso()
        self.save()

    @_locked
    def bump_daily_new(self, count: int = 1, *, persist: bool = True) -> None:
        key = date.today().isoformat()
        daily = self._data.setdefault("daily_new_count", {})
        daily[key] = int(daily.get(key, 0)) + count
        if persist:
            self.save()

    @_locked
    def set_alert(self, key: str, message: str, level: str = "warning") -> None:
        """登记一条界面提示（会持久化，供 UI 顶部横幅展示）。"""
        alerts = self._data.setdefault("alerts", {})
        alerts[key] = {"level": level, "message": message, "time": _now_iso()}
        self.save()

    @_locked
    def clear_alert(self, key: str) -> None:
        alerts = self._data.setdefault("alerts", {})
        if key in alerts:
            del alerts[key]
            self.save()

    @_locked
    def list_alerts(self) -> list[dict[str, Any]]:
        alerts = self._data.get("alerts", {})
        return [{"key": k, **v} for k, v in alerts.items()]

    @_locked
    def set_skipped_categories(
        self, source: str, items: list[dict[str, Any]], *, retryable: bool = True
    ) -> None:
        """登记某来源本次被跳过的子分类（供界面标识与定时重试）。

        items 为空表示本次没有跳过任何分类，直接清空记录。
        retryable=False 表示这次跳过不适合自动重试（例如被人机验证中断）。
        """
        data = self._data.setdefault("skipped_categories", {})
        if items:
            data[source] = {
                "items": [dict(i) for i in items],
                "retryable": bool(retryable),
                "updated_at": _now_iso(),
            }
        else:
            data.pop(source, None)
        self.save()

    @_locked
    def get_skipped_categories(
        self, source: str, *, retryable_only: bool = False
    ) -> list[dict[str, Any]]:
        """取某来源被跳过的子分类；retryable_only=True 时忽略不安排重试的记录。"""
        entry = self._data.get("skipped_categories", {}).get(source)
        if not isinstance(entry, dict):
            return []
        if retryable_only and not entry.get("retryable", False):
            return []
        items = entry.get("items") or []
        return [dict(i) for i in items if isinstance(i, dict)]

    @_locked
    def set_crawl_cursor(
        self,
        source: str,
        *,
        code: str = "",
        page: int = 1,
        scanned: int = 0,
        finished: bool = False,
    ) -> None:
        """记录爬取进度位置（分类导航码 + 页码），供中断重启后接着爬。

        finished=True 表示上一轮已经完整跑完，下次应当从头全量扫描。
        """
        self._set_cursor(
            source, code=code, page=page, scanned=scanned, finished=finished
        )
        self.save()

    @_locked
    def _set_cursor(
        self,
        source: str,
        *,
        code: str = "",
        page: int = 1,
        scanned: int = 0,
        finished: bool = False,
    ) -> None:
        """只改内存、不落盘（由调用方决定何时 save）。"""
        cursors = self._data.setdefault("crawl_cursor", {})
        cursors[source] = {
            "code": code,
            "page": max(1, int(page)),
            "scanned": max(0, int(scanned)),
            "finished": bool(finished),
            "updated_at": _now_iso(),
        }

    @_locked
    def get_crawl_cursor(self, source: str) -> dict[str, Any] | None:
        cur = self._data.get("crawl_cursor", {}).get(source)
        return dict(cur) if isinstance(cur, dict) else None

    @_locked
    def set_category_status(
        self, source: str, code: str, **fields: Any
    ) -> None:
        """记录某个子分类的爬取状态（共多少条 / 状态 / 跳过原因等）。"""
        cats = self._data.setdefault("category_status", {}).setdefault(source, {})
        entry = cats.setdefault(code, {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        entry["updated_at"] = _now_iso()
        self.save()

    @_locked
    def get_category_status(self, source: str) -> dict[str, dict[str, Any]]:
        cats = self._data.get("category_status", {}).get(source)
        if not isinstance(cats, dict):
            return {}
        return {k: dict(v) for k, v in cats.items() if isinstance(v, dict)}

    @_locked
    def update_category_statuses(
        self, source: str, updates: dict[str, dict[str, Any]]
    ) -> None:
        """批量更新多个子分类状态（只落盘一次）。"""
        cats = self._data.setdefault("category_status", {}).setdefault(source, {})
        now = _now_iso()
        for code, fields in updates.items():
            entry = cats.setdefault(code, {})
            entry.update({k: v for k, v in fields.items() if v is not None})
            entry["updated_at"] = now
        self.save()

    @_locked
    def set_throttle_state(
        self, source: str, *, round_no: int = 0, resume_at: str = ""
    ) -> None:
        """记录限流退避的轮次与「到点重试时间」，供中断重启后接着等。"""
        data = self._data.setdefault("throttle_state", {})
        if not round_no and not resume_at:
            if data.pop(source, None) is not None:
                self.save()
            return
        data[source] = {
            "round_no": max(0, int(round_no)),
            "resume_at": str(resume_at),
            "updated_at": _now_iso(),
        }
        self.save()

    @_locked
    def get_throttle_state(self, source: str) -> dict[str, Any] | None:
        rec = self._data.get("throttle_state", {}).get(source)
        return dict(rec) if isinstance(rec, dict) else None

    @_locked
    def clear_throttle_state(self, source: str) -> None:
        data = self._data.setdefault("throttle_state", {})
        if data.pop(source, None) is not None:
            self.save()

    @_locked
    def set_id_walk_position(
        self, source: str, last_id: int, **extra: Any
    ) -> None:
        """记录「按编号爬取」已经走到哪个编号（日常增量从此往上找）。

        extra 里可带 scanned_max（已核对过的最大编号）与 failed_ids（导出失败、
        下次需要重新尝试的编号），用于重启后跳过已知无效编号，避免从头再扫。
        """
        data = self._data.setdefault("id_walk", {})
        rec: dict[str, Any] = {
            "last_id": max(0, int(last_id)),
            "updated_at": _now_iso(),
        }
        for key, value in extra.items():
            if value is not None:
                rec[key] = value
        data[source] = rec
        self.save()

    @_locked
    def get_id_walk_position(self, source: str) -> dict[str, Any] | None:
        rec = self._data.get("id_walk", {}).get(source)
        return dict(rec) if isinstance(rec, dict) else None

    @_locked
    def set_total_estimate(self, source: str, value: int) -> None:
        """缓存「库总量估算」（如中纪检 6444），重启时不必每次都去站点上重算。"""
        data = self._data.setdefault("total_estimate", {})
        data[source] = {"value": int(value), "updated_at": _now_iso()}
        self.save()

    @_locked
    def get_total_estimate(
        self, source: str, max_age_hours: float | None = 24.0
    ) -> int | None:
        """取缓存的库总量；max_age_hours=None 表示不检查新鲜度（首次运行续跑用）。"""
        rec = (self._data.get("total_estimate") or {}).get(source)
        if not isinstance(rec, dict):
            return None
        try:
            value = int(rec.get("value") or 0)
            updated = datetime.fromisoformat(str(rec.get("updated_at") or ""))
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        if max_age_hours is not None:
            age_hours = (datetime.now() - updated).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                return None
        return value

    @_locked
    def set_first_run(self, value: bool) -> None:
        """标记/取消“首次运行”（首次采集完成、且下一次定时任务执行后取消）。"""
        self._data["first_run_pending"] = bool(value)
        self.save()

    @_locked
    def is_first_run(self) -> bool:
        return bool(self._data.get("first_run_pending"))

    @_locked
    def mark_pass_done(self, source: str) -> None:
        """标记某个来源已完成一次「完整遍历」（首次运行据此判断是否可以收尾）。"""
        data = self._data.setdefault("pass_done", {})
        data[source] = True
        self.save()

    @_locked
    def is_pass_done(self, source: str) -> bool:
        return bool((self._data.get("pass_done") or {}).get(source))

    @_locked
    def reset_pass_done(self) -> None:
        self._data["pass_done"] = {}
        self.save()

    @_locked
    def register_document(
        self,
        doc_key: str,
        *,
        source: str,
        title: str,
        url: str,
        pdf_path: str,
        category_path: str = "",
    ) -> bool:
        """登记新文档。若 doc_key 已存在则返回 False。"""
        docs = self._data.setdefault("documents", {})
        if doc_key in docs:
            return False
        docs[doc_key] = {
            "source": source,
            "title": title,
            "url": url,
            "pdf_path": pdf_path,
            "category_path": category_path,
            "crawled_at": _now_iso(),
            "uploaded_at": None,
            "upload_dataset_id": None,
        }
        self.bump_daily_new(1, persist=False)
        self.save()
        return True

    @_locked
    def repoint_document(self, doc_key: str, pdf_path: str) -> None:
        """已登记文档的 PDF 被重新导出时，把路径改到新文件（避免留下孤儿文件）。"""
        rec = (self._data.get("documents") or {}).get(doc_key)
        if not isinstance(rec, dict):
            return
        if rec.get("pdf_path") != pdf_path:
            rec["pdf_path"] = pdf_path
            self.save()

    @_locked
    def mark_uploaded(self, doc_key: str, dataset_id: str) -> None:
        docs = self._data.setdefault("documents", {})
        if doc_key not in docs:
            return
        docs[doc_key]["uploaded_at"] = _now_iso()
        docs[doc_key]["upload_dataset_id"] = dataset_id
        self.save()

    @_locked
    def mark_pdf_path(self, doc_key: str, pdf_path: str) -> None:
        """修正已登记文档的 PDF 路径（文件被移动/重写后，避免每轮重复导出）。"""
        docs = self._data.setdefault("documents", {})
        if doc_key not in docs:
            return
        docs[doc_key]["pdf_path"] = pdf_path
        self.save()

    @_locked
    def list_documents(self, source: str | None = None) -> list[dict[str, Any]]:
        docs = self._data.get("documents", {})
        rows = []
        for key, meta in docs.items():
            row = {"doc_key": key, **meta}
            if source is None or meta.get("source") == source:
                rows.append(row)
        rows.sort(key=lambda x: x.get("crawled_at") or "", reverse=True)
        return rows

    @_locked
    def pending_upload_keys(
        self,
        source: str,
        min_age_days: int,
        max_age_days: int,
        *,
        immediate: bool = False,
    ) -> list[str]:
        """返回尚未上传的文档 key。

        immediate=True 时忽略保留天数（首次运行立即上传全部新文件）；
        否则只返回已保存满 min_age_days 的文档。max_age_days 保留参数位，
        当前不设上传截止时间，失败文件会在后续上传任务中继续重试。
        """
        now = datetime.now()
        result: list[str] = []
        for key, meta in self._data.get("documents", {}).items():
            if meta.get("source") != source:
                continue
            if meta.get("uploaded_at"):
                continue
            crawled_raw = meta.get("crawled_at")
            if not crawled_raw:
                continue
            if not immediate:
                crawled = datetime.fromisoformat(crawled_raw)
                age_days = (now - crawled).total_seconds() / 86400
                if age_days < min_age_days:
                    continue
            result.append(key)
        return result
