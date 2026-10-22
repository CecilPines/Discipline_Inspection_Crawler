"""爬虫与上传任务的全局状态（JSON 持久化，供 UI 与定时任务读取）。"""

from __future__ import annotations

import json
import hashlib
import re
import threading
from functools import wraps
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

from regulation_versions import (
    build_version_index,
    date_from_filename,
    link_suffix_from_key,
    parse_date_value,
    split_version_title,
    stable_version_id,
    version_family_key,
)

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
        self.backfill_checkpoint_path = self.state_path.with_name(
            "backfill_resume.json"
        )
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
            "source_totals": {},
            "first_run_pending": False,
            "documents": {},
            # 版本记录与 documents 分离：同一 doc_key 的替换文件也保留旧版本。
            "version_records": {},
            "version_index": {},
            "version_indices": {"ccdi": {}, "bit": {}},
            "version_index_meta": {
                "ready": False,
                "built_at": None,
                "document_count": 0,
                "dated_documents": 0,
                "missing_dates": 0,
                "reason": "",
                "sources": {
                    "ccdi": {
                        "ready": False,
                        "built_at": None,
                        "document_count": 0,
                        "dated_documents": 0,
                        "missing_dates": 0,
                        "reason": "",
                        "error": "",
                    },
                    "bit": {
                        "ready": False,
                        "built_at": None,
                        "document_count": 0,
                        "dated_documents": 0,
                        "missing_dates": 0,
                        "reason": "",
                        "error": "",
                    },
                },
            },
            "source_entry_signatures": {},
            "parse_queue": {
                "active": [],
                "retry": [],
                "wave": 0,
                "message": "",
                "updated_at": None,
            },
            "dataset_migration": {},
            "maintenance_flags": {
                "schema_version": 4,
                "ccdi": False,
                "bit": False,
            },
            "backfill_status": {
                "ccdi": {
                    "status": "idle",
                    "current": 0,
                    "total": 0,
                    "message": "",
                    "updated_at": None,
                },
                "bit": {
                    "status": "idle",
                    "current": 0,
                    "total": 0,
                    "message": "",
                    "updated_at": None,
                },
            },
        }

    @_locked
    def load(self) -> None:
        if not self.state_path.exists():
            self.save()
            return
        keep = self._data
        needs_save = False
        with _lock:
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {**self._default_data(), **raw}
                    changed = self._ensure_version_records()
                    migrated = self._migrate_maintenance_schema()
                    if not migrated:
                        self._merge_backfill_checkpoint()
                    needs_save = bool(
                        migrated
                        or changed
                        or "version_records" not in raw
                        or "version_index" not in raw
                        or "version_index_meta" not in raw
                        or "backfill_status" not in raw
                        or "parse_queue" not in raw
                    )
            except (json.JSONDecodeError, OSError):
                # 读取失败（多半是另一个进程正在写盘）：保留内存里的数据，
                # 绝不回退成空状态——否则会丢掉已登记的文档记录
                self._data = keep
        if needs_save:
            try:
                self.save()
            except OSError:
                # 状态自动升级写入失败时保留已成功解析的内存数据；下次启动再试。
                pass
        self._loaded_mtime = self._mtime()

    def _migrate_maintenance_schema(self) -> bool:
        """升级到新的时效标志流程，并丢弃旧日期补录断点。"""
        flags = self._data.setdefault("maintenance_flags", {})
        try:
            version = int(flags.get("schema_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= 4:
            return False
        try:
            self.backfill_checkpoint_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._data["maintenance_flags"] = {
            "schema_version": 4,
            "ccdi": False,
            "bit": False,
        }
        defaults = self._default_data()
        self._data["backfill_status"] = defaults["backfill_status"]
        meta = self._data.setdefault("version_index_meta", {})
        meta["ready"] = False
        meta["reason"] = "维护流程已升级，等待重新核对日期和索引"
        for source in ("ccdi", "bit"):
            entry = (meta.setdefault("sources", {})).setdefault(source, {})
            entry["ready"] = False
            entry["reason"] = "等待重新核对日期和索引"
        return True

    @_locked
    def get_maintenance_flags(self) -> dict[str, Any]:
        return deepcopy(self._data.get("maintenance_flags") or {})

    @_locked
    def set_maintenance_flag(self, source: str, value: bool) -> None:
        if source not in ("ccdi", "bit"):
            raise ValueError("source 必须为 ccdi 或 bit")
        flags = self._data.setdefault("maintenance_flags", {})
        flags["schema_version"] = 4
        flags[source] = bool(value)
        self.save()

    @_locked
    def set_all_maintenance_flags(self, value: bool) -> None:
        flags = self._data.setdefault("maintenance_flags", {})
        flags["schema_version"] = 4
        flags["ccdi"] = bool(value)
        flags["bit"] = bool(value)
        self.save()

    def _read_backfill_checkpoint(self) -> dict[str, Any]:
        if not self.backfill_checkpoint_path.exists():
            return {}
        try:
            data = json.loads(
                self.backfill_checkpoint_path.read_text(encoding="utf-8")
            )
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _merge_backfill_checkpoint(self) -> None:
        checkpoint = self._read_backfill_checkpoint()
        if not checkpoint:
            return
        status = self._data.setdefault("backfill_status", {})
        for source in ("ccdi", "bit"):
            row = checkpoint.get(source)
            if isinstance(row, dict):
                status.setdefault(source, {}).update(row)

    def _document_version_record(
        self, doc_key: str, meta: dict[str, Any]
    ) -> dict[str, Any]:
        title = str(meta.get("title") or "")
        base_title, version_label, revision_year = split_version_title(title)
        publish_date = parse_date_value(
            meta.get("publish_date"), meta.get("publish_year")
        )
        pdf_path = str(meta.get("pdf_path") or "")
        version_id = str(
            meta.get("version_id")
            or stable_version_id(
                doc_key,
                title=title,
                url=str(meta.get("url") or ""),
                pdf_path=pdf_path,
                publish_date=publish_date,
            )
        )
        meta["version_id"] = version_id
        meta["version_family_key"] = version_family_key(
            str(meta.get("source") or ""), title
        )
        meta["base_title"] = base_title
        meta["version_label"] = version_label
        meta["revision_year"] = revision_year
        meta["publish_date"] = publish_date
        return {
            "version_id": version_id,
            "document_key": doc_key,
            "source": meta.get("source") or "",
            "title": title,
            "base_title": base_title,
            "version_label": version_label,
            "revision_year": revision_year,
            "version_family_key": meta["version_family_key"],
            "link_suffix": str(
                meta.get("link_suffix") or link_suffix_from_key(doc_key)
            ),
            "url": meta.get("url") or "",
            "pdf_path": pdf_path,
            "category_path": meta.get("category_path") or "",
            "publish_date": publish_date,
            "date_precision": meta.get("date_precision") or "",
            "date_source": meta.get("date_source") or "",
            "crawled_at": meta.get("crawled_at") or "",
            "uploaded_at": meta.get("uploaded_at"),
            "upload_dataset_id": meta.get("upload_dataset_id"),
            "ragflow_document_id": meta.get("ragflow_document_id"),
        }

    def _ensure_version_records(self) -> bool:
        """为旧状态里的 documents 自动补版本记录，保持原 uploaded_at 不变。"""
        records = self._data.setdefault("version_records", {})
        changed = False
        for doc_key, meta in (self._data.get("documents") or {}).items():
            if not isinstance(meta, dict):
                continue
            record = self._document_version_record(doc_key, meta)
            version_id = record["version_id"]
            if version_id not in records:
                records[version_id] = record
                changed = True
            elif records[version_id].get("pdf_path") != record["pdf_path"]:
                records[version_id] = record
                changed = True
        return changed

    def _index_date_stats(self) -> dict[str, dict[str, Any]]:
        docs = self._data.get("documents") or {}
        sources = {
            "ccdi": {
                "total": 0,
                "dated": 0,
                "missing": 0,
                "versions": 0,
                "filename_dated": 0,
            },
            "bit": {
                "total": 0,
                "dated": 0,
                "missing": 0,
                "versions": 0,
                "filename_dated": 0,
            },
        }
        for record in (self._data.get("version_records") or {}).values():
            if not isinstance(record, dict):
                continue
            source = str(record.get("source") or "")
            if source in sources:
                sources[source]["versions"] += 1
        for doc_key, meta in docs.items():
            if not isinstance(meta, dict):
                continue
            source = str(meta.get("source") or "")
            if source not in sources:
                continue
            sources[source]["total"] += 1
            if meta.get("publish_date"):
                sources[source]["dated"] += 1
                if meta.get("date_source") == "filename":
                    sources[source]["filename_dated"] += 1
        for stats in sources.values():
            stats["missing"] = max(0, stats["total"] - stats["dated"])
        return sources

    def _rebuild_version_index(
        self,
        *,
        ready: bool = True,
        reason: str = "",
        source_errors: dict[str, str] | None = None,
    ) -> None:
        records = self._data.setdefault("version_records", {})
        source_errors = source_errors or {}
        indices: dict[str, dict[str, Any]] = {}
        combined: dict[str, dict[str, Any]] = {}
        for source in ("ccdi", "bit"):
            source_records = {
                version_id: record
                for version_id, record in records.items()
                if isinstance(record, dict) and record.get("source") == source
            }
            index = build_version_index(source_records)
            indices[source] = index
            combined.update(index)
        self._data["version_indices"] = indices
        self._data["version_index"] = combined
        source_stats = self._index_date_stats()
        total = sum(int(v["total"]) for v in source_stats.values())
        dated = sum(int(v["dated"]) for v in source_stats.values())
        missing = sum(int(v["missing"]) for v in source_stats.values())
        now = _now_iso()
        source_meta: dict[str, dict[str, Any]] = {}
        for source, stats in source_stats.items():
            source_missing = int(stats["missing"])
            source_ready = bool(ready and source_missing == 0)
            source_error = str(source_errors.get(source) or "")
            source_reason = (
                f"日期已补齐，索引已建立（{len(indices[source])} 个版本族）"
                if source_ready
                else (
                    f"日期仍缺 {source_missing} 条"
                    if source_missing
                    else "版本索引尚未建立"
                )
            )
            source_meta[source] = {
                **stats,
                "ready": source_ready,
                "built_at": now,
                "reason": source_reason,
                "error": source_error,
            }
        overall_ready = all(item["ready"] for item in source_meta.values())
        overall_reason = reason or (
            "中纪检、校纪检索引均已建立"
            if overall_ready
            else "；".join(
                f"{source}: {item['reason']}"
                for source, item in source_meta.items()
                if not item["ready"]
            )
        )
        self._data["version_index_meta"] = {
            "ready": overall_ready,
            "built_at": now,
            "document_count": total,
            "dated_documents": dated,
            "missing_dates": missing,
            "reason": overall_reason,
            "sources": source_meta,
        }

    @_locked
    def rebuild_version_index(
        self,
        *,
        ready: bool = True,
        reason: str = "",
        source_errors: dict[str, str] | None = None,
    ) -> None:
        """用当前版本记录生成最终索引（日期补录完成后调用一次）。"""
        self._rebuild_version_index(
            ready=ready, reason=reason, source_errors=source_errors
        )
        self.save()

    @_locked
    def rebuild_source_version_index(
        self,
        source: str,
        *,
        ready: bool = True,
        reason: str = "",
        error: str = "",
        require_all_dates: bool = True,
    ) -> None:
        """只重建一个来源的索引；另一来源的索引与建立时间保持不变。"""
        if source not in ("ccdi", "bit"):
            raise ValueError("source 必须为 ccdi 或 bit")
        records = self._data.setdefault("version_records", {})
        source_records = {
            version_id: record
            for version_id, record in records.items()
            if isinstance(record, dict) and record.get("source") == source
        }
        index = build_version_index(source_records)
        indices = self._data.setdefault("version_indices", {})
        indices[source] = index
        combined: dict[str, dict[str, Any]] = {}
        for key in ("ccdi", "bit"):
            combined.update(indices.get(key) or {})
        self._data["version_index"] = combined

        stats = self._index_date_stats()[source]
        source_ready = bool(
            ready and (not require_all_dates or int(stats["missing"]) == 0)
        )
        now = _now_iso()
        meta = self._data.setdefault("version_index_meta", {})
        sources = meta.setdefault("sources", {})
        sources[source] = {
            **stats,
            "ready": source_ready,
            "built_at": now,
            "reason": reason
            or (
                f"日期已补齐，索引已建立（{len(index)} 个版本族）"
                if source_ready
                else f"日期仍缺 {stats['missing']} 条"
            ),
            "error": str(error or ""),
        }
        # 尚未单独建立过的另一来源保持未就绪。
        for other in ("ccdi", "bit"):
            sources.setdefault(
                other,
                {
                    "ready": False,
                    "built_at": None,
                    "document_count": 0,
                    "dated_documents": 0,
                    "missing_dates": 0,
                    "versions": 0,
                    "reason": "等待建立该来源索引",
                    "error": "",
                },
            )
        overall_ready = all(bool(item.get("ready")) for item in sources.values())
        meta.update(
            {
                "ready": overall_ready,
                "built_at": now,
                "document_count": sum(
                    int(item.get("total") or 0) for item in sources.values()
                ),
                "dated_documents": sum(
                    int(item.get("dated") or 0) for item in sources.values()
                ),
                "missing_dates": sum(
                    int(item.get("missing") or 0) for item in sources.values()
                ),
                "reason": (
                    "中纪检、校纪检索引均已建立"
                    if overall_ready
                    else "；".join(
                        f"{key}: {value.get('reason') or '未就绪'}"
                        for key, value in sources.items()
                        if not value.get("ready")
                    )
                ),
            }
        )
        self.save()

    @_locked
    def mark_version_index_pending(self, reason: str) -> None:
        meta = self._data.setdefault("version_index_meta", {})
        meta["ready"] = False
        meta["reason"] = str(reason or "版本索引需要重建")
        sources = meta.setdefault("sources", {})
        for source in ("ccdi", "bit"):
            entry = sources.setdefault(source, {})
            entry["ready"] = False
            entry["reason"] = str(reason or "版本索引需要重建")
        meta["ready"] = False
        self.save()

    @_locked
    def mark_source_index_pending(self, source: str, reason: str) -> None:
        """只将指定来源标记为待重建，保留另一来源已完成索引。"""
        if source not in ("ccdi", "bit"):
            raise ValueError("source 必须为 ccdi 或 bit")
        meta = self._data.setdefault("version_index_meta", {})
        sources = meta.setdefault("sources", {})
        entry = sources.setdefault(source, {})
        entry["ready"] = False
        entry["reason"] = str(reason or "版本索引需要重建")
        meta["ready"] = all(
            bool((sources.get(name) or {}).get("ready"))
            for name in ("ccdi", "bit")
        )
        meta["reason"] = f"{source}: {entry['reason']}"
        self.save()

    @_locked
    def get_version_index_status(self) -> dict[str, Any]:
        return deepcopy(self._data.get("version_index_meta") or {})

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
        persist: bool = True,
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
        if persist:
            self.save()

    @_locked
    def set_backfill_status(
        self,
        key: str,
        *,
        status: str | None = None,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> None:
        data = self._data.setdefault("backfill_status", {})
        entry = data.setdefault(key, {})
        if status is not None:
            entry["status"] = status
        if current is not None:
            entry["current"] = max(0, int(current))
        if total is not None:
            entry["total"] = max(0, int(total))
        if message is not None:
            entry["message"] = str(message)
        if extra:
            entry.update(extra)
        entry["updated_at"] = _now_iso()
        if persist:
            self.save()

    @_locked
    def get_backfill_statuses(self) -> dict[str, Any]:
        data = self._data.get("backfill_status") or {}
        return {
            key: dict(value)
            for key, value in data.items()
            if isinstance(value, dict)
        }

    @_locked
    def reset_backfill_progress(self, source: str) -> None:
        """清空指定来源的年份补全进度和断点，下一次从第一页开始。"""
        if source not in ("ccdi", "bit"):
            raise ValueError("source 必须为 ccdi 或 bit")
        status = self._data.setdefault("backfill_status", {})
        status[source] = {
            "status": "idle",
            "current": 0,
            "total": 0,
            "message": "",
            "updated_at": _now_iso(),
            "year": None,
            "page": 0,
            "next_page": 1,
            "page_count": 0,
            "year_total_count": None,
            "year_scanned": 0,
            "year_filled": 0,
            "year_rows": [],
            "completed_years": [],
            "incomplete_years": [],
            "throttle_message": "",
            "throttle_resume_at": "",
        }
        self.clear_backfill_checkpoint(source)
        self.save()

    @_locked
    def save_backfill_checkpoint(self, source: str, **fields: Any) -> None:
        """把日期补录断点写入独立小文件，避免每页重写完整状态文件。"""
        checkpoint = self._read_backfill_checkpoint()
        row = checkpoint.setdefault(source, {})
        row.update(
            {
                key: value
                for key, value in fields.items()
                if value is not None
            }
        )
        row["updated_at"] = _now_iso()
        self.backfill_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.backfill_checkpoint_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.backfill_checkpoint_path)
        status = self._data.setdefault("backfill_status", {}).setdefault(
            source, {}
        )
        status.update(row)

    @_locked
    def clear_backfill_checkpoint(self, source: str) -> None:
        checkpoint = self._read_backfill_checkpoint()
        if source not in checkpoint:
            return
        checkpoint.pop(source, None)
        if checkpoint:
            tmp = self.backfill_checkpoint_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.backfill_checkpoint_path)
        else:
            try:
                self.backfill_checkpoint_path.unlink(missing_ok=True)
            except OSError:
                pass

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
        self,
        source: str,
        *,
        round_no: int = 0,
        resume_at: str = "",
        context: str = "",
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
            "context": str(context),
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
        publish_date: str = "",
        date_precision: str = "",
        link_suffix: str = "",
    ) -> bool:
        """登记新文档。若 doc_key 已存在则返回 False。"""
        docs = self._data.setdefault("documents", {})
        if doc_key in docs:
            return False
        meta: dict[str, Any] = {
            "source": source,
            "title": title,
            "url": url,
            "pdf_path": pdf_path,
            "category_path": category_path,
            "crawled_at": _now_iso(),
            "uploaded_at": None,
            "upload_dataset_id": None,
            "ragflow_document_id": None,
            "parse_status": "UNSTART",
            "publish_date": parse_date_value(publish_date),
            "date_precision": date_precision,
        }
        if link_suffix:
            meta["link_suffix"] = str(link_suffix)
        record = self._document_version_record(doc_key, meta)
        docs[doc_key] = meta
        self._data.setdefault("version_records", {})[record["version_id"]] = record
        self._rebuild_version_index()
        self.bump_daily_new(1, persist=False)
        self.save()
        return True

    @_locked
    def update_document_version(
        self,
        doc_key: str,
        *,
        source: str,
        title: str,
        url: str,
        pdf_path: str,
        category_path: str = "",
        publish_date: str = "",
        date_precision: str = "",
        link_suffix: str = "",
        count_as_new: bool = True,
    ) -> str:
        """登记同一来源条目的新文件，并保留该条目此前的版本记录。

        返回新版本 ID。旧版本的 uploaded_at / RAGFlow 文档 ID 保留在旧版本记录
        中，供上传最新版后从知识库删除旧文档。
        """
        docs = self._data.setdefault("documents", {})
        old = docs.get(doc_key) if isinstance(docs.get(doc_key), dict) else {}
        meta: dict[str, Any] = {
            "source": source,
            "title": title,
            "url": url,
            "pdf_path": pdf_path,
            "category_path": category_path,
            "crawled_at": _now_iso(),
            "uploaded_at": None,
            "upload_dataset_id": None,
            "ragflow_document_id": None,
            "parse_status": "UNSTART",
            "publish_date": parse_date_value(publish_date),
            "date_precision": date_precision,
        }
        if link_suffix:
            meta["link_suffix"] = str(link_suffix)
        # 同一路径只是补写元数据时沿用当前版本 ID，避免把同一文件拆成两个版本。
        same_file = bool(old and str(old.get("pdf_path") or "") == pdf_path)
        if same_file and old.get("version_id"):
            meta["version_id"] = str(old["version_id"])
        record = self._document_version_record(doc_key, meta)
        docs[doc_key] = meta
        records = self._data.setdefault("version_records", {})
        records[record["version_id"]] = record
        self._rebuild_version_index()
        if count_as_new:
            self.bump_daily_new(1, persist=False)
        self.save()
        return str(record["version_id"])

    @_locked
    def update_document_metadata(
        self,
        doc_key: str,
        updates: dict[str, Any],
        *,
        rebuild_index: bool = True,
    ) -> None:
        """补写发布日期、标题等元数据，不改变当前文件与版本 ID。"""
        docs = self._data.setdefault("documents", {})
        meta = docs.get(doc_key)
        if not isinstance(meta, dict):
            return
        allowed = {
            "title",
            "url",
            "pdf_path",
            "category_path",
            "publish_date",
            "publish_year",
            "date_precision",
            "link_suffix",
        }
        changed = False
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key in ("publish_date", "publish_year"):
                value = parse_date_value(value)
            if meta.get(key) != value:
                meta[key] = value
                changed = True
        if not changed:
            return
        record = self._document_version_record(doc_key, meta)
        self._data.setdefault("version_records", {})[
            str(meta["version_id"])
        ] = record
        if rebuild_index:
            self._rebuild_version_index()
        self.save()

    @_locked
    def set_document_publish_date(
        self,
        doc_key: str,
        value: str,
        *,
        precision: str = "",
        date_source: str = "",
        rebuild_index: bool = True,
    ) -> bool:
        docs = self._data.setdefault("documents", {})
        meta = docs.get(doc_key)
        if not isinstance(meta, dict):
            return False
        parsed = parse_date_value(value)
        if not parsed:
            return False
        if meta.get("publish_date") == parsed and (
            not precision or meta.get("date_precision") == precision
        ) and (
            not date_source or meta.get("date_source") == date_source
        ):
            return False
        meta["publish_date"] = parsed
        if precision:
            meta["date_precision"] = precision
        if date_source:
            meta["date_source"] = date_source
        record = self._document_version_record(doc_key, meta)
        self._data.setdefault("version_records", {})[
            str(meta["version_id"])
        ] = record
        if rebuild_index:
            self._rebuild_version_index()
        self.save()
        return True

    @_locked
    def set_document_publish_dates(
        self,
        values: dict[str, str],
        *,
        precision: str = "",
        date_source: str = "",
        rebuild_index: bool = True,
    ) -> int:
        """批量写入发布日期，只落盘一次（供历史年份回填使用）。"""
        docs = self._data.setdefault("documents", {})
        records = self._data.setdefault("version_records", {})
        changed = 0
        for doc_key, value in values.items():
            meta = docs.get(doc_key)
            parsed = parse_date_value(value)
            if not isinstance(meta, dict) or not parsed:
                continue
            if meta.get("publish_date") == parsed and (
                not precision or meta.get("date_precision") == precision
            ) and (
                not date_source or meta.get("date_source") == date_source
            ):
                continue
            meta["publish_date"] = parsed
            if precision:
                meta["date_precision"] = precision
            if date_source:
                meta["date_source"] = date_source
            record = self._document_version_record(doc_key, meta)
            records[str(meta["version_id"])] = record
            changed += 1
        if changed:
            if rebuild_index:
                self._rebuild_version_index()
            self.save()
        return changed

    @_locked
    def fill_missing_dates_from_filenames(
        self,
        sources: tuple[str, ...] = ("ccdi", "bit"),
        *,
        rebuild_index: bool = False,
    ) -> dict[str, int]:
        """联网补录后仍缺日期时，从文件名末尾提取年份或完整日期。"""
        docs = self._data.setdefault("documents", {})
        records = self._data.setdefault("version_records", {})
        counts = {source: 0 for source in sources}
        for meta in docs.values():
            if not isinstance(meta, dict) or meta.get("publish_date"):
                continue
            source = str(meta.get("source") or "")
            if source not in counts:
                continue
            value = date_from_filename(str(meta.get("pdf_path") or ""))
            if not value:
                continue
            if source == "ccdi":
                value = value[:4]
            meta["publish_date"] = value
            meta["date_precision"] = "day" if len(value) == 10 else "year"
            meta["date_source"] = "filename"
            version_id = str(meta.get("version_id") or "")
            record = records.get(version_id)
            if isinstance(record, dict):
                record["publish_date"] = value
                record["date_precision"] = meta["date_precision"]
                record["date_source"] = "filename"
            counts[source] += 1
        if any(counts.values()):
            if rebuild_index:
                self._rebuild_version_index()
            self.save()
        return counts

    @_locked
    def repoint_document(self, doc_key: str, pdf_path: str) -> None:
        """已登记文档的 PDF 被重新导出时，把路径改到新文件（避免留下孤儿文件）。"""
        rec = (self._data.get("documents") or {}).get(doc_key)
        if not isinstance(rec, dict):
            return
        if rec.get("pdf_path") != pdf_path:
            rec["pdf_path"] = pdf_path
            version_id = str(rec.get("version_id") or "")
            version = (self._data.get("version_records") or {}).get(version_id)
            if isinstance(version, dict):
                version["pdf_path"] = pdf_path
                self._rebuild_version_index()
            self.save()

    @_locked
    def set_source_total(self, source: str, value: int) -> None:
        """记录来源目录页声明的总条数（如校纪检「共 N 条」），用于判断有没有更新。"""
        data = self._data.setdefault("source_totals", {})
        data[source] = {"value": int(value), "updated_at": _now_iso()}
        self.save()

    @_locked
    def get_source_total(self, source: str) -> int | None:
        rec = (self._data.get("source_totals") or {}).get(source)
        if not isinstance(rec, dict):
            return None
        try:
            return int(rec.get("value") or 0)
        except (TypeError, ValueError):
            return None

    @_locked
    def mark_uploaded(
        self,
        doc_key: str,
        dataset_id: str,
        *,
        ragflow_document_id: str = "",
        parse_status: str = "UNSTART",
    ) -> None:
        docs = self._data.setdefault("documents", {})
        if doc_key not in docs:
            return
        docs[doc_key]["uploaded_at"] = _now_iso()
        docs[doc_key]["upload_dataset_id"] = dataset_id
        if ragflow_document_id:
            docs[doc_key]["ragflow_document_id"] = ragflow_document_id
        docs[doc_key]["parse_status"] = parse_status
        version_id = str(docs[doc_key].get("version_id") or "")
        record = (self._data.get("version_records") or {}).get(version_id)
        if isinstance(record, dict):
            record.update(
                {
                    "uploaded_at": docs[doc_key]["uploaded_at"],
                    "upload_dataset_id": dataset_id,
                    "ragflow_document_id": ragflow_document_id or None,
                    "parse_status": parse_status,
                }
            )
        self.save()

    @_locked
    def mark_uploaded_many(self, uploads: list[dict[str, Any]]) -> None:
        """批量写入上传结果，避免 500 文件窗口逐条落盘。"""
        docs = self._data.setdefault("documents", {})
        records = self._data.setdefault("version_records", {})
        now = _now_iso()
        changed = False
        for item in uploads:
            doc_key = str(item.get("doc_key") or "")
            meta = docs.get(doc_key)
            if not isinstance(meta, dict):
                continue
            dataset_id = str(item.get("dataset_id") or "")
            doc_id = str(item.get("ragflow_document_id") or "")
            meta["uploaded_at"] = now
            meta["upload_dataset_id"] = dataset_id
            if doc_id:
                meta["ragflow_document_id"] = doc_id
            meta["parse_status"] = "UNSTART"
            record = records.get(str(meta.get("version_id") or ""))
            if isinstance(record, dict):
                record.update(
                    {
                        "uploaded_at": now,
                        "upload_dataset_id": dataset_id,
                        "ragflow_document_id": doc_id or None,
                        "parse_status": "UNSTART",
                    }
                )
            changed = True
        if changed:
            self.save()

    @_locked
    def set_document_parse_state(
        self,
        doc_key: str,
        *,
        status: str,
        ragflow_document_id: str = "",
        error: str = "",
    ) -> None:
        docs = self._data.setdefault("documents", {})
        meta = docs.get(doc_key)
        if not isinstance(meta, dict):
            return
        meta["parse_status"] = str(status or "")
        if error:
            meta["parse_error"] = error
        elif status == "DONE":
            meta.pop("parse_error", None)
        if ragflow_document_id:
            meta["ragflow_document_id"] = ragflow_document_id
        version_id = str(meta.get("version_id") or "")
        record = (self._data.get("version_records") or {}).get(version_id)
        if isinstance(record, dict):
            record["parse_status"] = meta["parse_status"]
            record["parse_error"] = meta.get("parse_error") or ""
            if ragflow_document_id:
                record["ragflow_document_id"] = ragflow_document_id
        self.save()

    @_locked
    def set_document_parse_states(
        self, updates: dict[str, dict[str, Any]]
    ) -> None:
        """批量更新解析状态，供一个 500 文件窗口一次落盘。"""
        docs = self._data.setdefault("documents", {})
        records = self._data.setdefault("version_records", {})
        changed = False
        for doc_key, fields in updates.items():
            meta = docs.get(doc_key)
            if not isinstance(meta, dict):
                continue
            status = str(fields.get("status") or "")
            if status:
                meta["parse_status"] = status
            error = str(fields.get("error") or "")
            if error:
                meta["parse_error"] = error
            elif status == "DONE":
                meta.pop("parse_error", None)
            doc_id = str(fields.get("ragflow_document_id") or "")
            if doc_id:
                meta["ragflow_document_id"] = doc_id
            version = records.get(str(meta.get("version_id") or ""))
            if isinstance(version, dict):
                version["parse_status"] = meta.get("parse_status")
                version["parse_error"] = meta.get("parse_error") or ""
                if doc_id:
                    version["ragflow_document_id"] = doc_id
            changed = True
        if changed:
            self.save()

    def _latest_version_ids(self) -> set[str]:
        latest = {
            str(entry.get("latest_version_id") or "")
            for entry in (self._data.get("version_index") or {}).values()
            if isinstance(entry, dict)
        }
        latest.discard("")
        return latest

    @_locked
    def superseded_uploaded_versions(self, doc_key: str) -> list[dict[str, Any]]:
        """返回同一标题族中已被当前版本取代、且曾经上传过的版本记录。"""
        docs = self._data.get("documents") or {}
        meta = docs.get(doc_key)
        if not isinstance(meta, dict):
            return []
        family_key = str(meta.get("version_family_key") or "")
        current_id = str(meta.get("version_id") or "")
        rows: list[dict[str, Any]] = []
        for version_id, record in (self._data.get("version_records") or {}).items():
            if not isinstance(record, dict):
                continue
            if version_id == current_id:
                continue
            if str(record.get("version_family_key") or "") != family_key:
                continue
            if record.get("uploaded_at") or record.get("ragflow_document_id"):
                rows.append({"version_id": version_id, **record})
        return rows

    @_locked
    def mark_superseded_versions_deleted(
        self, version_ids: list[str], dataset_id: str
    ) -> None:
        """旧版本文档从知识库删除后，清理其库关联但保留本地文件索引。"""
        ids = set(str(value) for value in version_ids)
        records = self._data.setdefault("version_records", {})
        changed = False
        for version_id in ids:
            record = records.get(version_id)
            if not isinstance(record, dict):
                continue
            if record.get("upload_dataset_id") not in (None, dataset_id):
                continue
            record["uploaded_at"] = None
            record["upload_dataset_id"] = None
            record["ragflow_document_id"] = None
            record["superseded_deleted_at"] = _now_iso()
            changed = True
        if changed:
            self.save()

    @_locked
    def mark_pdf_path(self, doc_key: str, pdf_path: str) -> None:
        """修正已登记文档的 PDF 路径（文件被移动/重写后，避免每轮重复导出）。"""
        docs = self._data.setdefault("documents", {})
        if doc_key not in docs:
            return
        docs[doc_key]["pdf_path"] = pdf_path
        version_id = str(docs[doc_key].get("version_id") or "")
        version = (self._data.get("version_records") or {}).get(version_id)
        if isinstance(version, dict):
            version["pdf_path"] = pdf_path
            self._rebuild_version_index()
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
        latest_ids = self._latest_version_ids()
        result: list[str] = []
        for key, meta in self._data.get("documents", {}).items():
            if meta.get("source") != source:
                continue
            if meta.get("uploaded_at"):
                continue
            version_id = str(meta.get("version_id") or "")
            if latest_ids and version_id and version_id not in latest_ids:
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

    @_locked
    def set_parse_queue(self, payload: dict[str, Any]) -> None:
        queue = self._data.setdefault("parse_queue", {})
        queue.clear()
        queue.update(payload)
        queue["updated_at"] = _now_iso()
        self.save()

    @_locked
    def get_parse_queue(self) -> dict[str, Any]:
        return dict(self._data.get("parse_queue") or {})

    @_locked
    def set_dataset_migration(self, **fields: Any) -> None:
        record = self._data.setdefault("dataset_migration", {})
        record.update(fields)
        record["updated_at"] = _now_iso()
        self.save()

    @_locked
    def set_source_entry_signatures(
        self, source: str, signatures: dict[str, dict[str, Any]]
    ) -> None:
        data = self._data.setdefault("source_entry_signatures", {})
        data[source] = {str(k): dict(v) for k, v in signatures.items()}
        self.save()

    @_locked
    def get_source_entry_signatures(self, source: str) -> dict[str, dict[str, Any]]:
        data = (self._data.get("source_entry_signatures") or {}).get(source) or {}
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}

    @_locked
    def import_unregistered_local_files(
        self, output_dir: Path, *, rebuild_index: bool = True
    ) -> int:
        """把输出目录中尚未登记的 PDF 补进版本索引（不增加“今日新增”）。"""
        root = Path(output_dir)
        docs = self._data.setdefault("documents", {})
        known_paths = {
            str(Path(meta.get("pdf_path") or "").resolve())
            for meta in docs.values()
            if isinstance(meta, dict) and meta.get("pdf_path")
        }
        records = self._data.setdefault("version_records", {})
        imported = 0
        for source, folder_name in (("ccdi", "中纪检"), ("bit", "校纪检")):
            source_root = root / folder_name
            if not source_root.is_dir():
                continue
            for path in source_root.rglob("*.pdf"):
                try:
                    resolved = str(path.resolve())
                except OSError:
                    continue
                if resolved in known_paths or not path.is_file():
                    continue
                stem = re.sub(
                    r"^(?:.*?)?\[\d+\]\s+",
                    "",
                    path.stem,
                )
                stem = re.sub(r"_\d{8}(?:_\d+)?$", "", stem)
                stem = re.sub(r"_\d{4}(?:_\d+)?$", "", stem)
                title = stem.strip() or path.stem
                try:
                    rel = path.relative_to(root)
                    parts = rel.parent.parts
                    if parts and parts[0] == folder_name:
                        parts = parts[1:]
                    category = Path(*parts).as_posix() if parts else ""
                    crawled_at = datetime.fromtimestamp(path.stat().st_mtime).replace(
                        microsecond=0
                    ).isoformat()
                except OSError:
                    continue
                digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]
                doc_key = f"local:{digest}"
                meta: dict[str, Any] = {
                    "source": source,
                    "title": title,
                    "url": "",
                    "pdf_path": resolved,
                    "category_path": category,
                    "crawled_at": crawled_at,
                    "uploaded_at": None,
                    "upload_dataset_id": None,
                    "ragflow_document_id": None,
                    "parse_status": "UNSTART",
                    "publish_date": "",
                    "date_precision": "",
                    "imported_from_disk": True,
                }
                date_match = re.search(r"_((?:19|20)\d{2})(\d{2})(\d{2})(?:_\d+)?$", path.stem)
                if date_match:
                    meta["publish_date"] = parse_date_value(
                        "-".join(date_match.groups())
                    )
                    meta["date_precision"] = "day"
                else:
                    year_match = re.search(r"_((?:19|20)\d{2})(?:_\d+)?$", path.stem)
                    if year_match:
                        meta["publish_date"] = year_match.group(1)
                        meta["date_precision"] = "year"
                record = self._document_version_record(doc_key, meta)
                docs[doc_key] = meta
                records[record["version_id"]] = record
                known_paths.add(resolved)
                imported += 1
        if imported:
            if rebuild_index:
                self._rebuild_version_index()
            self.save()
        return imported

    @_locked
    def version_families(self, *, multi_only: bool = True) -> list[dict[str, Any]]:
        result = []
        for entry in (self._data.get("version_index") or {}).values():
            if not isinstance(entry, dict):
                continue
            versions = entry.get("versions") or []
            if multi_only and len(versions) <= 1:
                continue
            result.append(
                {
                    **entry,
                    "versions": sorted(
                        [dict(v) for v in versions],
                        key=lambda v: (
                            1 if v.get("is_latest") else 0,
                            str(v.get("publish_date") or ""),
                            str(v.get("crawled_at") or ""),
                        ),
                        reverse=True,
                    ),
                }
            )
        result.sort(key=lambda x: (x.get("source") or "", x.get("title") or ""))
        return result

    @_locked
    def reset_uploads_for_dataset_migration(self) -> None:
        """迁库前清空知识库关联，本地文件与版本记录保持不变。"""
        now = _now_iso()
        for meta in (self._data.get("documents") or {}).values():
            if not isinstance(meta, dict):
                continue
            meta["uploaded_at"] = None
            meta["upload_dataset_id"] = None
            meta["ragflow_document_id"] = None
            meta["parse_status"] = "UNSTART"
            meta.pop("parse_error", None)
        for record in (self._data.get("version_records") or {}).values():
            if not isinstance(record, dict):
                continue
            record["uploaded_at"] = None
            record["upload_dataset_id"] = None
            record["ragflow_document_id"] = None
            record["parse_status"] = "UNSTART"
            record["parse_error"] = ""
        self._data["parse_queue"] = {
            "active": [],
            "retry": [],
            "wave": 0,
            "message": "迁库：等待重新上传",
            "updated_at": now,
        }
        self.save()
