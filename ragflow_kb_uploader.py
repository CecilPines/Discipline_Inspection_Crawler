"""
RAGFlow 知识库上传与解析（参考 ragflow_kb_manager.py，独立实现供主程序调用）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from crawl_common import wait_while_paused
from crawler_state import CrawlerState

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".md", ".mdx",
    ".csv", ".xlsx", ".xls", ".ppt", ".pptx",
    ".jpeg", ".jpg", ".png", ".tif", ".gif",
}


class RagflowApiError(RuntimeError):
    def __init__(
        self,
        action: str,
        message: Any,
        *,
        code: Any = None,
        http_status: int | None = None,
    ):
        self.action = action
        self.message = str(message)
        self.code = code
        self.http_status = http_status
        details = []
        if http_status is not None:
            details.append(f"HTTP {http_status}")
        if code is not None:
            details.append(f"code={code}")
        suffix = f"（{'，'.join(details)}）" if details else ""
        super().__init__(f"{action} 失败{suffix}: {self.message}")


class RagflowKbUploader:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _check(self, resp: requests.Response, action: str) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            text = (resp.text or "").strip().replace("\n", " ")[:300]
            raise RagflowApiError(
                action,
                text or "响应不是 JSON",
                http_status=resp.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise RagflowApiError(
                action,
                f"响应结构异常: {type(data).__name__}",
                http_status=resp.status_code,
            )
        if data.get("code") != 0:
            raise RagflowApiError(
                action,
                data.get("message", "未知错误"),
                code=data.get("code"),
                http_status=resp.status_code,
            )
        return data

    def list_datasets(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while page <= 1000:
            last_error: Exception | None = None
            data: dict[str, Any] | None = None
            for attempt in range(1, 4):
                try:
                    resp = requests.get(
                        f"{self.base_url}/datasets",
                        headers=self.headers,
                        params={"page": page, "page_size": 200},
                        timeout=60,
                    )
                    data = self._check(resp, "列出知识库")
                    break
                except (requests.RequestException, RagflowApiError) as exc:
                    last_error = exc
                    retryable = isinstance(exc, requests.RequestException)
                    if isinstance(exc, RagflowApiError):
                        retryable = (
                            exc.http_status is not None
                            and (
                                exc.http_status == 429
                                or exc.http_status >= 500
                            )
                        ) or str(exc.code) in {
                            "1",
                            "100",
                            "103",
                            "104",
                            "105",
                            "107",
                            "500",
                            "2003",
                        }
                    if (
                        isinstance(exc, RagflowApiError)
                        and str(exc.code) == "2003"
                        and attempt >= 3
                    ):
                        raise RuntimeError(
                            "列出知识库失败：RAGFlow 连续返回内部错误 2003。"
                            "请检查 RAGFlow 容器及其数据库、Redis、对象存储是否正常，"
                            "同时确认 API Key 和环境变量 RAGFLOW_API_KEY 未被旧值覆盖"
                        ) from exc
                    if not retryable or attempt >= 3:
                        raise
                    delay = 2 ** (attempt - 1)
                    logger.warning(
                        "列出 RAGFlow 知识库临时失败（第 %s/3 次）：%s；%s 秒后重试",
                        attempt,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            if data is None:
                raise RuntimeError(
                    "列出知识库失败：RAGFlow 连续返回异常"
                    + (f"：{last_error}" if last_error else "")
                )
            body = data.get("data")
            if isinstance(body, dict):
                items = body.get("datasets") or body.get("items") or []
            else:
                items = body or []
            if not items:
                break
            added = 0
            for ds in items:
                if not isinstance(ds, dict) or not ds.get("id"):
                    continue
                dataset_id = str(ds["id"])
                if dataset_id in seen:
                    continue
                seen.add(dataset_id)
                result.append(dict(ds))
                added += 1
            total = _extract_total(data)
            if total is not None and len(result) >= total:
                break
            if not added:
                break
            page += 1
        return result

    def find_dataset_id_by_name(self, name: str) -> str | None:
        for ds in self.list_datasets():
            if ds.get("name") == name:
                return ds.get("id")
        return None

    def find_datasets_by_name(self, name: str) -> list[dict[str, Any]]:
        return [ds for ds in self.list_datasets() if ds.get("name") == name]

    def create_dataset(
        self,
        name: str,
        *,
        chunk_method: str = "laws",
        chunk_token_num: int = 512,
        description: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "name": name,
            "chunk_method": chunk_method,
            "parser_config": {
                "chunk_token_num": chunk_token_num,
                "layout_recognize": "DeepDOC",
                "raptor": {"use_raptor": False},
            },
        }
        if description:
            payload["description"] = description
        resp = requests.post(
            f"{self.base_url}/datasets",
            headers=self.headers,
            json=payload,
            timeout=60,
        )
        data = self._check(resp, "创建知识库")
        return data["data"]["id"]

    def ensure_dataset_id(
        self,
        name: str,
        configured_id: str = "",
        *,
        chunk_method: str = "laws",
        chunk_token_num: int = 512,
    ) -> str:
        datasets = self.list_datasets()
        if configured_id:
            for ds in datasets:
                if str(ds.get("id") or "") == str(configured_id):
                    if ds.get("name") == name:
                        return configured_id
                    logger.warning(
                        "配置的知识库 ID %s 对应“%s”，与名称“%s”不一致，按名称重新解析",
                        configured_id,
                        ds.get("name"),
                        name,
                    )
                    break
        for ds in datasets:
            if ds.get("name") == name and ds.get("id"):
                return str(ds["id"])
        return self.create_dataset(
            name,
            chunk_method=chunk_method,
            chunk_token_num=chunk_token_num,
            description=f"纪律法规爬虫自动创建: {name}",
        )

    def delete_datasets(self, dataset_ids: list[str]) -> None:
        ids = [str(value) for value in dataset_ids if value]
        if not ids:
            return
        resp = requests.delete(
            f"{self.base_url}/datasets",
            headers=self.headers,
            json={"ids": ids},
            timeout=120,
        )
        self._check(resp, "删除知识库")

    @staticmethod
    def _is_missing_document_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "documentdoesnotexist" in text
            or "document matching query does not exist" in text
        )

    def delete_dataset_safely(self, dataset_id: str) -> dict[str, Any]:
        """删除单个知识库，并容忍 RAGFlow 文档记录已丢失的脏数据。

        RAGFlow 某些版本的级联删除会因文档记录已被解析任务或其它请求删除而
        返回 DocumentDoesNotExist。此时先逐项清理仍列出的文档，再重试删除；
        若知识库实际已经消失，也按删除成功处理。
        """
        dataset_id = str(dataset_id or "")
        if not dataset_id:
            return {"dataset_id": "", "recovered": False, "documents_deleted": 0}
        try:
            self.delete_datasets([dataset_id])
            return {
                "dataset_id": dataset_id,
                "recovered": False,
                "documents_deleted": 0,
            }
        except RuntimeError as exc:
            if not self._is_missing_document_error(exc):
                raise
            logger.warning(
                "知识库 %s 删除时遇到已丢失的文档记录，先清理残留文档后重试",
                dataset_id,
            )

        # 请求可能已经删除知识库，只是级联任务返回了脏记录。
        if not any(
            str(item.get("id") or "") == dataset_id
            for item in self.list_datasets()
            if isinstance(item, dict)
        ):
            return {
                "dataset_id": dataset_id,
                "recovered": True,
                "documents_deleted": 0,
            }

        try:
            documents = self.list_document_objects(dataset_id)
        except RuntimeError as exc:
            if not self._is_missing_document_error(exc):
                raise
            documents = []
        document_ids = [
            str(item.get("id"))
            for item in documents
            if isinstance(item, dict) and item.get("id")
        ]
        deleted = 0
        failed: list[str] = []
        # 不带损坏 ID 的批量删除通常最快；失败后再逐个定位问题记录。
        for start in range(0, len(document_ids), 100):
            batch = document_ids[start : start + 100]
            try:
                self.delete_documents(dataset_id, batch)
                deleted += len(batch)
                continue
            except RuntimeError as exc:
                if not self._is_missing_document_error(exc):
                    raise
            for document_id in batch:
                try:
                    self.delete_documents(dataset_id, [document_id])
                    deleted += 1
                except RuntimeError as exc:
                    if not self._is_missing_document_error(exc):
                        raise
                    failed.append(document_id)

        try:
            self.delete_datasets([dataset_id])
        except RuntimeError as exc:
            if not self._is_missing_document_error(exc):
                raise
            if any(
                str(item.get("id") or "") == dataset_id
                for item in self.list_datasets()
                if isinstance(item, dict)
            ):
                raise RuntimeError(
                    f"删除知识库 {dataset_id} 失败：RAGFlow 中仍存在无法级联删除的"
                    "孤立文档记录；本次已清理可访问文档，请稍后重试迁库"
                ) from exc
        return {
            "dataset_id": dataset_id,
            "recovered": True,
            "documents_deleted": deleted,
            "ignored_missing_document_ids": failed,
        }

    def upload_documents_detailed(
        self, dataset_id: str, file_paths: list[str]
    ) -> list[dict[str, Any]]:
        files = []
        opened = []
        selected_names: list[str] = []
        for p in file_paths:
            path = Path(p)
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if not path.is_file():
                continue
            fobj = path.open("rb")
            opened.append(fobj)
            files.append(("file", (path.name, fobj)))
            selected_names.append(path.name)
        if not files:
            return []
        try:
            resp = requests.post(
                f"{self.base_url}/datasets/{dataset_id}/documents",
                headers=self.headers,
                files=files,
                timeout=300,
            )
            data = self._check(resp, "上传文档")
            raw_docs = data.get("data") or []
            if isinstance(raw_docs, dict):
                raw_docs = (
                    raw_docs.get("documents")
                    or raw_docs.get("docs")
                    or raw_docs.get("items")
                    or []
                )
            docs = []
            for raw in raw_docs:
                if isinstance(raw, dict) and raw.get("id"):
                    docs.append(dict(raw))
                elif isinstance(raw, str) and raw.strip():
                    docs.append({"id": raw.strip()})
            # 极少数服务端版本不返回 name；按请求顺序补上，保证状态能精确写回。
            for index, doc in enumerate(docs):
                if not doc.get("name") and index < len(selected_names):
                    doc["name"] = selected_names[index]
            return docs
        finally:
            for fobj in opened:
                fobj.close()

    def upload_documents(self, dataset_id: str, file_paths: list[str]) -> list[str]:
        return [
            str(doc["id"])
            for doc in self.upload_documents_detailed(dataset_id, file_paths)
            if doc.get("id")
        ]

    def start_parsing(self, dataset_id: str, document_ids: list[str]) -> None:
        if not document_ids:
            return
        resp = requests.post(
            f"{self.base_url}/datasets/{dataset_id}/chunks",
            headers=self.headers,
            json={"document_ids": document_ids},
            timeout=60,
        )
        self._check(resp, "触发解析")

    def stop_parsing(self, dataset_id: str, document_ids: list[str]) -> None:
        if not document_ids:
            return
        resp = requests.delete(
            f"{self.base_url}/datasets/{dataset_id}/chunks",
            headers=self.headers,
            json={"document_ids": document_ids},
            timeout=60,
        )
        self._check(resp, "取消文档解析")

    def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        ids = [str(value) for value in document_ids if value]
        if not ids:
            return
        resp = requests.delete(
            f"{self.base_url}/datasets/{dataset_id}/documents",
            headers=self.headers,
            json={"ids": ids},
            timeout=120,
        )
        self._check(resp, "删除旧版本文档")

    def list_document_objects(self, dataset_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while page <= 10000:
            resp = requests.get(
                f"{self.base_url}/datasets/{dataset_id}/documents",
                headers=self.headers,
                params={"page": page, "page_size": 100},
                timeout=30,
            )
            data = self._check(resp, "列出知识库文档")
            docs = _extract_docs(data)
            if not docs:
                break
            added = 0
            for raw in docs:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                doc_id = str(raw["id"])
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                item = dict(raw)
                item["run"] = _normalize_run(item)
                result.append(item)
                added += 1
            total = _extract_total(data)
            if total is not None and len(result) >= total:
                break
            if not added:
                break
            page += 1
        return result


def _parse_options(config: dict[str, Any]) -> tuple[int, int, int, int, int]:
    up_cfg = config.get("upload", {}) or {}
    try:
        max_parsing = max(1, int(up_cfg.get("max_parsing_documents", 500)))
    except (TypeError, ValueError):
        max_parsing = 500
    try:
        timeout_seconds = max(
            30, int(float(up_cfg.get("parse_timeout_minutes", 30)) * 60)
        )
    except (TypeError, ValueError):
        timeout_seconds = 30 * 60
    try:
        poll_seconds = max(1, int(up_cfg.get("parse_poll_seconds", 10)))
    except (TypeError, ValueError):
        poll_seconds = 10
    try:
        retry_rounds = max(0, int(up_cfg.get("parse_retry_rounds", 1)))
    except (TypeError, ValueError):
        retry_rounds = 1
    try:
        upload_batch = max(1, min(50, int(up_cfg.get("upload_batch_size", 5))))
    except (TypeError, ValueError):
        upload_batch = 5
    return max_parsing, timeout_seconds, poll_seconds, retry_rounds, upload_batch


def _queue_payload(
    state: CrawlerState,
    *,
    active: list[dict[str, Any]],
    retry: list[dict[str, Any]],
    wave: int,
    message: str,
) -> None:
    state.set_parse_queue(
        {"active": active, "retry": retry, "wave": wave, "message": message}
    )


def _dedupe_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row.get("dataset_id") or ""), str(row.get("document_id") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def _wait_for_parse_capacity(
    uploader: RagflowKbUploader,
    dataset_id: str,
    *,
    max_parsing: int,
    timeout_seconds: int,
    poll_seconds: int,
    stop_event: threading.Event | None,
    pause_event: threading.Event | None,
) -> int:
    """返回当前还能安全上传的解析槽位；必要时等待或取消卡住的旧任务。"""
    started = time.monotonic()
    while not (stop_event and stop_event.is_set()):
        wait_while_paused(pause_event, stop_event)
        docs = uploader.list_document_objects(dataset_id)
        active = [
            doc
            for doc in docs
            if str(doc.get("run") or "").upper() in {"RUNNING", "UNSTART"}
        ]
        capacity = max(0, max_parsing - len(active))
        if capacity > 0:
            return capacity
        if time.monotonic() - started >= timeout_seconds:
            # 旧进程遗留的解析没有本地开始时间。为避免永久占满 500 个槽位，
            # 超时后取消当前批次，让新一轮上传继续推进。
            ids = [str(doc.get("id")) for doc in active if doc.get("id")]
            logger.warning(
                "知识库已有 %s 个文档处于解析中，等待超过 %s 分钟；取消这些解析以释放槽位",
                len(ids),
                timeout_seconds // 60,
            )
            try:
                uploader.stop_parsing(dataset_id, ids)
            except Exception:
                logger.exception("取消知识库中卡住的解析失败")
            return max_parsing
        time.sleep(poll_seconds)
    return 0


def _merge_queue_for_source(
    state: CrawlerState,
    source: str,
    *,
    active: list[dict[str, Any]] | None = None,
    retry: list[dict[str, Any]] | None = None,
    wave: int | None = None,
    message: str = "",
) -> None:
    current = state.get_parse_queue()
    old_active = [
        dict(row) for row in current.get("active", []) if row.get("source") != source
    ]
    old_retry = [
        dict(row) for row in current.get("retry", []) if row.get("source") != source
    ]
    _queue_payload(
        state,
        active=old_active + (active or []),
        retry=_dedupe_queue(old_retry + (retry or [])),
        wave=int(wave if wave is not None else current.get("wave") or 0),
        message=message or str(current.get("message") or ""),
    )


def _wait_parse_completion(
    uploader: RagflowKbUploader,
    state: CrawlerState,
    dataset_id: str,
    entries: list[dict[str, Any]],
    *,
    timeout_seconds: int,
    poll_seconds: int,
    stop_event: threading.Event | None,
    pause_event: threading.Event | None,
    wave: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """等待一波文档解析结束；超时文档取消解析并返回重试队列。"""
    done: list[dict[str, Any]] = []
    retry: list[dict[str, Any]] = []
    waiting = {str(entry["document_id"]): dict(entry) for entry in entries}
    started = time.monotonic()
    while waiting:
        if stop_event and stop_event.is_set():
            retry.extend(waiting.values())
            break
        wait_while_paused(pause_event, stop_event)
        statuses = {
            str(doc.get("id")): str(doc.get("run") or "UNSTART")
            for doc in uploader.list_document_objects(dataset_id)
            if doc.get("id")
        }
        state_updates: dict[str, dict[str, Any]] = {}
        for doc_id, entry in list(waiting.items()):
            run = statuses.get(doc_id, "UNSTART").upper()
            entry["run"] = run
            if run == "DONE":
                state_updates[entry["doc_key"]] = {
                    "status": "DONE",
                    "ragflow_document_id": doc_id,
                }
                done.append(entry)
                waiting.pop(doc_id, None)
            elif run in ("FAIL", "CANCEL"):
                entry["reason"] = f"解析状态 {run}"
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                state_updates[entry["doc_key"]] = {
                    "status": "RETRY",
                    "ragflow_document_id": doc_id,
                    "error": entry["reason"],
                }
                retry.append(entry)
                waiting.pop(doc_id, None)
        if state_updates:
            state.set_document_parse_states(state_updates)
        _merge_queue_for_source(
            state,
            str(entries[0].get("source") or "") if entries else "",
            active=list(waiting.values()),
            retry=retry,
            wave=wave,
            message=(
                f"第 {wave} 波解析：{len(done)} 完成 / {len(waiting)} 进行中 / "
                f"{len(retry)} 待重试"
            ),
        )
        if not waiting:
            break
        if time.monotonic() - started >= timeout_seconds:
            timed_out = list(waiting.values())
            ids = [str(item["document_id"]) for item in timed_out]
            state.set_progress(
                "upload",
                message=f"解析超时，取消 {len(ids)} 个文件并继续上传后续批次…",
            )
            try:
                uploader.stop_parsing(dataset_id, ids)
            except Exception:
                logger.exception("取消超时文档解析失败，仍按待重试处理")
            for entry in timed_out:
                entry["reason"] = f"解析超过 {timeout_seconds // 60} 分钟"
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if timed_out:
                state.set_document_parse_states(
                    {
                        entry["doc_key"]: {
                            "status": "RETRY",
                            "ragflow_document_id": str(entry["document_id"]),
                            "error": entry["reason"],
                        }
                        for entry in timed_out
                    }
                )
            retry.extend(timed_out)
            waiting.clear()
            _merge_queue_for_source(
                state,
                str(entries[0].get("source") or "") if entries else "",
                active=[],
                retry=retry,
                wave=wave,
                message=f"第 {wave} 波有 {len(timed_out)} 个文件解析超时，已排入重试",
            )
            break
        time.sleep(poll_seconds)
    return done, retry


def _cleanup_superseded_documents(
    uploader: RagflowKbUploader,
    state: CrawlerState,
    dataset_id: str,
    doc_keys: list[str],
) -> int:
    """最新版本解析完成后，删除同一标题族中已上传的旧版本。"""
    old_rows: list[dict[str, Any]] = []
    docs = state.snapshot().get("documents") or {}
    current_ids = {
        str((docs.get(doc_key) or {}).get("ragflow_document_id") or "")
        for doc_key in doc_keys
    }
    for doc_key in doc_keys:
        old_rows.extend(state.superseded_uploaded_versions(doc_key))
    if not old_rows:
        return 0
    ids: set[str] = set()
    names: dict[str, list[str]] = {}
    for row in old_rows:
        doc_id = str(row.get("ragflow_document_id") or "")
        if doc_id:
            ids.add(doc_id)
            continue
        name = Path(str(row.get("pdf_path") or "")).name
        if name:
            names.setdefault(name, []).append(str(row.get("version_id") or ""))
    if names:
        dataset_docs = uploader.list_document_objects(dataset_id)
        for doc in dataset_docs:
            name = str(doc.get("name") or "")
            if name in names:
                ids.add(str(doc.get("id") or ""))
    ids.discard("")
    ids.difference_update(current_ids)
    if not ids:
        return 0
    uploader.delete_documents(dataset_id, sorted(ids))
    version_ids = [
        str(row.get("version_id"))
        for row in old_rows
        if row.get("version_id")
    ]
    state.mark_superseded_versions_deleted(version_ids, dataset_id)
    logger.info("已从知识库 %s 删除 %s 个被新版本取代的文档", dataset_id, len(ids))
    return len(ids)


def _process_parse_retry(
    uploader: RagflowKbUploader,
    state: CrawlerState,
    dataset_id: str,
    source: str,
    entries: list[dict[str, Any]],
    *,
    max_parsing: int,
    timeout_seconds: int,
    poll_seconds: int,
    rounds: int,
    stop_event: threading.Event | None,
    pause_event: threading.Event | None,
) -> list[dict[str, Any]]:
    remaining = _dedupe_queue(entries)
    wave = int(state.get_parse_queue().get("wave") or 0) + 1
    for round_no in range(1, rounds + 1):
        if not remaining or (stop_event and stop_event.is_set()):
            break
        next_remaining: list[dict[str, Any]] = []
        for start in range(0, len(remaining), max_parsing):
            chunk = remaining[start : start + max_parsing]
            ids = [str(item.get("document_id") or "") for item in chunk]
            try:
                uploader.start_parsing(dataset_id, ids)
            except Exception as exc:
                for item in chunk:
                    item["reason"] = f"重新触发解析失败：{exc}"
                    next_remaining.append(item)
                continue
            _, failed = _wait_parse_completion(
                uploader,
                state,
                dataset_id,
                chunk,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
                stop_event=stop_event,
                pause_event=pause_event,
                wave=wave,
            )
            wave += 1
            next_remaining.extend(failed)
        remaining = _dedupe_queue(next_remaining)
        logger.info(
            "解析失败重试第 %s/%s 轮：剩余 %s 个", round_no, rounds, len(remaining)
        )
    for item in remaining:
        item["source"] = source
    return remaining


def upload_pending_for_source(
    source: str,
    state: CrawlerState,
    uploader: RagflowKbUploader,
    dataset_id: str,
    *,
    min_age_days: int,
    max_age_days: int,
    immediate: bool = False,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    max_parsing_documents: int = 500,
    parse_timeout_seconds: int = 30 * 60,
    parse_poll_seconds: int = 10,
    parse_retry_rounds: int = 1,
    upload_batch_size: int = 5,
) -> int:
    """上传最新版本，并按波次限制同时解析的文档数量。"""
    keys = state.pending_upload_keys(
        source, min_age_days, max_age_days, immediate=immediate
    )
    current_queue = state.get_parse_queue()
    retry_entries = [
        dict(row)
        for row in current_queue.get("retry", [])
        if row.get("source") == source and row.get("dataset_id") == dataset_id
    ]
    # 进程在“上传成功”和“解析完成落盘”之间被强杀时，active 队列可能没来得及保存。
    # 从文档记录里补齐这些尚未确认解析完成的 RAGFlow 文档。
    for doc_key, meta in (state.snapshot().get("documents") or {}).items():
        if (
            meta.get("source") != source
            or meta.get("upload_dataset_id") != dataset_id
            or not meta.get("uploaded_at")
            or not meta.get("ragflow_document_id")
            or meta.get("parse_status") == "DONE"
        ):
            continue
        retry_entries.append(
            {
                "source": source,
                "dataset_id": dataset_id,
                "document_id": str(meta["ragflow_document_id"]),
                "doc_key": str(doc_key),
                "file_name": Path(str(meta.get("pdf_path") or "")).name,
                "started_at": str(meta.get("uploaded_at")),
                "attempts": 0,
                "reason": "等待确认解析状态",
            }
        )
    retry_entries = _dedupe_queue(retry_entries)
    if not keys and not retry_entries:
        return 0

    total = len(keys)
    uploaded = 0
    parsed = 0
    retry_for_later: list[dict[str, Any]] = []
    wave = int(current_queue.get("wave") or 0)
    state.set_progress(
        "upload",
        status="running",
        current=0,
        total=total,
        message=(
            f"上传 {source}: 0/{total} · 解析上限 {max_parsing_documents}"
            + (f" · 待重试 {len(retry_entries)}" if retry_entries else "")
        ),
    )

    wave_start = 0
    while wave_start < len(keys):
        if stop_event and stop_event.is_set():
            break
        capacity = _wait_for_parse_capacity(
            uploader,
            dataset_id,
            max_parsing=max_parsing_documents,
            timeout_seconds=parse_timeout_seconds,
            poll_seconds=parse_poll_seconds,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        if capacity <= 0:
            break
        wave_keys = keys[wave_start : wave_start + capacity]
        wave_start += len(wave_keys)
        wave += 1
        parse_entries: list[dict[str, Any]] = []
        for batch_start in range(0, len(wave_keys), upload_batch_size):
            if stop_event and stop_event.is_set():
                break
            wait_while_paused(pause_event, stop_event)
            batch_keys = wave_keys[batch_start : batch_start + upload_batch_size]
            paths: list[str] = []
            valid_keys: list[str] = []
            for key in batch_keys:
                meta = state.snapshot().get("documents", {}).get(key) or {}
                pdf_path = str(meta.get("pdf_path") or "")
                if pdf_path and Path(pdf_path).is_file():
                    paths.append(pdf_path)
                    valid_keys.append(key)
            if not paths:
                continue
            state.set_progress(
                "upload",
                current=uploaded,
                total=total,
                message=(
                    f"上传 {source}: {uploaded}/{total} · 第 {wave} 波"
                    f"（正在推送 {len(paths)} 个文件）"
                ),
            )
            docs = uploader.upload_documents_detailed(dataset_id, paths)
            by_name = {str(doc.get("name") or ""): doc for doc in docs}
            ordered = docs if len(docs) == len(paths) else []
            remote_by_name: dict[str, dict[str, Any]] | None = None
            upload_updates: list[dict[str, Any]] = []
            batch_parse_entries: list[dict[str, Any]] = []
            for index, (key, path) in enumerate(zip(valid_keys, paths)):
                doc = ordered[index] if ordered else by_name.get(Path(path).name)
                if (not doc or not doc.get("id")) and remote_by_name is None:
                    # 某些 RAGFlow 版本上传成功但响应里没有文档 ID。重新列出
                    # 当前知识库，按文件名找回刚上传的文档，避免只上传不解析。
                    remote_by_name = {
                        str(item.get("name") or ""): item
                        for item in uploader.list_document_objects(dataset_id)
                        if isinstance(item, dict)
                        and item.get("name")
                        and item.get("id")
                    }
                if not doc or not doc.get("id"):
                    doc = remote_by_name.get(Path(path).name)
                if not doc or not doc.get("id"):
                    continue
                doc_id = str(doc["id"])
                upload_updates.append(
                    {
                        "doc_key": key,
                        "dataset_id": dataset_id,
                        "ragflow_document_id": doc_id,
                    }
                )
                batch_parse_entries.append(
                    {
                        "source": source,
                        "dataset_id": dataset_id,
                        "document_id": doc_id,
                        "doc_key": key,
                        "file_name": Path(path).name,
                        "started_at": datetime.now().replace(microsecond=0).isoformat(),
                        "attempts": 0,
                    }
                )
                uploaded += 1
            if upload_updates:
                state.mark_uploaded_many(upload_updates)
            if batch_parse_entries:
                batch_ids = [
                    str(entry["document_id"]) for entry in batch_parse_entries
                ]
                try:
                    uploader.start_parsing(dataset_id, batch_ids)
                except Exception as exc:
                    logger.exception(
                        "上传批次触发解析失败，%s 个文件将进入重试",
                        len(batch_ids),
                    )
                    for entry in batch_parse_entries:
                        entry["attempts"] = int(entry.get("attempts") or 0) + 1
                        entry["reason"] = f"首次触发解析失败：{exc}"
                        entry["source"] = source
                    retry_for_later.extend(batch_parse_entries)
                else:
                    parse_entries.extend(batch_parse_entries)
                    _merge_queue_for_source(
                        state,
                        source,
                        active=parse_entries,
                        retry=retry_for_later,
                        wave=wave,
                        message=(
                            f"第 {wave} 波已上传 {uploaded} 个，"
                            f"解析已启动 {len(parse_entries)} 个"
                        ),
                    )
            state.set_progress(
                "upload",
                current=uploaded,
                total=total,
                message=(
                    f"上传 {source}: {uploaded}/{total} · 第 {wave} 波"
                    f"解析已启动 {len(parse_entries)} 个"
                    + (
                        f" · 待重试 {len(retry_for_later)} 个"
                        if retry_for_later
                        else ""
                    )
                ),
            )
        if not parse_entries:
            continue
        _merge_queue_for_source(
            state,
            source,
            active=parse_entries,
            retry=retry_for_later,
            wave=wave,
            message=f"第 {wave} 波上传完成，等待 {len(parse_entries)} 个文件解析",
        )
        done, failed = _wait_parse_completion(
            uploader,
            state,
            dataset_id,
            parse_entries,
            timeout_seconds=parse_timeout_seconds,
            poll_seconds=parse_poll_seconds,
            stop_event=stop_event,
            pause_event=pause_event,
            wave=wave,
        )
        parsed += len(done)
        retry_for_later.extend(failed)
        _cleanup_superseded_documents(
            uploader,
            state,
            dataset_id,
            [str(entry["doc_key"]) for entry in done],
        )
        _merge_queue_for_source(
            state,
            source,
            active=[],
            retry=retry_for_later,
            wave=wave,
            message=(
                f"第 {wave} 波结束：解析完成 {len(done)}，"
                f"待重试 {len(retry_for_later)}"
            ),
        )
        state.set_progress(
            "upload",
            current=uploaded,
            total=total,
            message=(
                f"上传 {source}: {uploaded}/{total} · 本波解析完成 {len(done)}"
                + (f" · 待重试 {len(retry_for_later)}" if retry_for_later else "")
            ),
        )

    if not (stop_event and stop_event.is_set()) and parse_retry_rounds > 0:
        retry_for_later = _process_parse_retry(
            uploader,
            state,
            dataset_id,
            source,
            _dedupe_queue(retry_entries + retry_for_later),
            max_parsing=max_parsing_documents,
            timeout_seconds=parse_timeout_seconds,
            poll_seconds=parse_poll_seconds,
            rounds=parse_retry_rounds,
            stop_event=stop_event,
            pause_event=pause_event,
        )

    stopped = bool(stop_event and stop_event.is_set())
    _merge_queue_for_source(
        state,
        source,
        active=[],
        retry=retry_for_later,
        wave=wave,
        message=(
            f"{source} 上传已终止，解析重试仍有 {len(retry_for_later)} 个"
            if stopped
            else (
                f"{source} 上传完成：本次上传 {uploaded} 个，解析完成 {parsed} 个"
                + (f"，待重试 {len(retry_for_later)} 个" if retry_for_later else "")
            )
        ),
    )
    state.set_progress(
        "upload",
        status="idle",
        current=uploaded,
        total=total,
        message=(
            f"{source} 上传已终止: {uploaded}"
            if stopped
            else (
                f"{source} 上传完成: {uploaded}"
                + (f"（解析待重试 {len(retry_for_later)}）" if retry_for_later else "")
            )
        ),
    )
    if uploaded:
        state.set_last_upload(source)
    return uploaded


def run_scheduled_uploads(
    state: CrawlerState,
    config: dict[str, Any],
    *,
    immediate: bool = False,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> dict[str, int]:
    """按配置对中纪检、校纪检两个库执行最新版本上传。"""
    rag_cfg = config.get("ragflow", {})
    up_cfg = config.get("upload", {})
    min_age = int(up_cfg.get("min_age_days", 3))
    max_age = int(up_cfg.get("max_age_days", 4))
    chunk_method = rag_cfg.get("chunk_method", "laws")
    chunk_token = int(rag_cfg.get("chunk_token_num", 512))
    max_parsing, timeout_seconds, poll_seconds, retry_rounds, batch_size = (
        _parse_options(config)
    )

    uploader = RagflowKbUploader(rag_cfg["base_url"], rag_cfg["api_key"])
    ccdi_id = uploader.ensure_dataset_id(
        rag_cfg.get("dataset_name_ccdi", "中纪检"),
        rag_cfg.get("dataset_id_ccdi", ""),
        chunk_method=chunk_method,
        chunk_token_num=chunk_token,
    )
    bit_id = uploader.ensure_dataset_id(
        rag_cfg.get("dataset_name_bit", "校纪检"),
        rag_cfg.get("dataset_id_bit", ""),
        chunk_method=chunk_method,
        chunk_token_num=chunk_token,
    )

    return {
        "ccdi": upload_pending_for_source(
            "ccdi",
            state,
            uploader,
            ccdi_id,
            min_age_days=min_age,
            max_age_days=max_age,
            immediate=immediate,
            stop_event=stop_event,
            pause_event=pause_event,
            max_parsing_documents=max_parsing,
            parse_timeout_seconds=timeout_seconds,
            parse_poll_seconds=poll_seconds,
            parse_retry_rounds=retry_rounds,
            upload_batch_size=batch_size,
        ),
        "bit": upload_pending_for_source(
            "bit",
            state,
            uploader,
            bit_id,
            min_age_days=min_age,
            max_age_days=max_age,
            immediate=immediate,
            stop_event=stop_event,
            pause_event=pause_event,
            max_parsing_documents=max_parsing,
            parse_timeout_seconds=timeout_seconds,
            parse_poll_seconds=poll_seconds,
            parse_retry_rounds=retry_rounds,
            upload_batch_size=batch_size,
        ),
    }


def migrate_dataset_libraries(
    state: CrawlerState,
    config: dict[str, Any],
    uploader: RagflowKbUploader,
    *,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    dataset_created_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """删除并重建两个知识库，再把当前最新版本重新上传。"""
    rag_cfg = config.get("ragflow", {}) or {}
    names = (
        str(rag_cfg.get("dataset_name_ccdi") or "中纪检"),
        str(rag_cfg.get("dataset_name_bit") or "校纪检"),
    )
    if names[0] == names[1]:
        raise RuntimeError("中纪检和校纪检不能使用同一个知识库名称")
    all_datasets = uploader.list_datasets()
    by_id = {
        str(ds.get("id")): dict(ds)
        for ds in all_datasets
        if isinstance(ds, dict) and ds.get("id")
    }
    ids: set[str] = set()
    for label, name, configured in zip(
        ("ccdi", "bit"),
        names,
        (rag_cfg.get("dataset_id_ccdi"), rag_cfg.get("dataset_id_bit")),
    ):
        if configured:
            configured_ds = by_id.get(str(configured))
            if configured_ds is not None and configured_ds.get("name") != name:
                raise RuntimeError(
                    f"配置中的 {label} 库 ID 对应“{configured_ds.get('name')}”，"
                    f"与库名“{name}”不一致；为避免误删，已停止迁库"
                )
            if configured_ds is not None:
                ids.add(str(configured))
        ids.update(
            str(ds.get("id"))
            for ds in all_datasets
            if isinstance(ds, dict) and ds.get("name") == name and ds.get("id")
        )
    state.set_progress(
        "upload",
        status="running",
        current=0,
        total=0,
        message="迁库：正在删除原中纪检、校纪检库…",
    )
    state.set_dataset_migration(
        status="running", old_dataset_ids=sorted(ids), started_at=datetime.now().isoformat()
    )
    deletion_results = []
    for dataset_id in sorted(ids):
        deletion_results.append(
            uploader.delete_dataset_safely(dataset_id)
            if hasattr(uploader, "delete_dataset_safely")
            else uploader.delete_datasets([dataset_id])
        )
    for name in names:
        try:
            uploader.create_dataset(
                name,
                chunk_method=str(rag_cfg.get("chunk_method") or "laws"),
                chunk_token_num=int(rag_cfg.get("chunk_token_num", 512) or 512),
                description=f"纪律法规爬虫迁库重建: {name}",
            )
        except Exception:
            state.set_dataset_migration(
                status="error",
                error=f"重建知识库失败: {name}",
                finished_at=datetime.now().isoformat(),
            )
            raise
    dataset_map = {
        str(ds.get("name")): str(ds.get("id"))
        for ds in uploader.list_datasets()
        if ds.get("name") in names and ds.get("id")
    }
    ccdi_id = dataset_map[names[0]]
    bit_id = dataset_map[names[1]]
    rag_cfg["dataset_id_ccdi"] = ccdi_id
    rag_cfg["dataset_id_bit"] = bit_id
    state.set_dataset_migration(
        status="uploading",
        dataset_id_ccdi=ccdi_id,
        dataset_id_bit=bit_id,
    )
    if dataset_created_callback:
        dataset_created_callback(ccdi_id, bit_id)
    state.reset_uploads_for_dataset_migration()

    up_cfg = config.get("upload", {}) or {}
    max_parsing, timeout_seconds, poll_seconds, retry_rounds, batch_size = (
        _parse_options(config)
    )
    counts = {
        "ccdi": upload_pending_for_source(
            "ccdi",
            state,
            uploader,
            ccdi_id,
            min_age_days=0,
            max_age_days=int(up_cfg.get("max_age_days", 4)),
            immediate=True,
            stop_event=stop_event,
            pause_event=pause_event,
            max_parsing_documents=max_parsing,
            parse_timeout_seconds=timeout_seconds,
            parse_poll_seconds=poll_seconds,
            parse_retry_rounds=retry_rounds,
            upload_batch_size=batch_size,
        ),
        "bit": upload_pending_for_source(
            "bit",
            state,
            uploader,
            bit_id,
            min_age_days=0,
            max_age_days=int(up_cfg.get("max_age_days", 4)),
            immediate=True,
            stop_event=stop_event,
            pause_event=pause_event,
            max_parsing_documents=max_parsing,
            parse_timeout_seconds=timeout_seconds,
            parse_poll_seconds=poll_seconds,
            parse_retry_rounds=retry_rounds,
            upload_batch_size=batch_size,
        ),
    }
    stopped = bool(stop_event and stop_event.is_set())
    state.set_dataset_migration(
        status="interrupted" if stopped else "done",
        dataset_id_ccdi=ccdi_id,
        dataset_id_bit=bit_id,
        uploaded=counts,
        error="新库已创建，但重新上传被终止；再次上传即可继续" if stopped else "",
        finished_at=datetime.now().isoformat(),
    )
    return {
        "dataset_id_ccdi": ccdi_id,
        "dataset_id_bit": bit_id,
        "uploaded": counts,
        "stopped": stopped,
    }


def _mask_api_key(key: str) -> str:
    key = key or ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _list_documents(
    base_url: str, headers: dict, dataset_id: str
) -> list[tuple[str, str | None]]:
    """分页拉取知识库内文档，返回 (文档ID, run状态或None) 列表。

    兼容两种返回结构：data 为文档数组，或 data 为 {docs: [...], total: N}。
    服务端可能限制每页返回条数，因此以「翻到空页」为终止条件，避免漏数。
    """
    items: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    page = 1
    while page <= 5000:
        resp = requests.get(
            f"{base_url}/datasets/{dataset_id}/documents",
            headers=headers,
            params={"page": page, "page_size": 100},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            break
        docs = _extract_docs(data)
        if not docs:
            break
        added = 0
        for doc in docs:
            if isinstance(doc, str):
                doc_id, run = doc, None
            elif isinstance(doc, dict):
                doc_id = doc.get("id")
                run = None
                if doc.get("run") or doc.get("progress") is not None:
                    run = _normalize_run(doc)
            else:
                continue
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                items.append((doc_id, run))
                added += 1
        total = _extract_total(data)
        if total is not None and len(items) >= total:
            break
        if added == 0:
            break
        page += 1
    return items


def _extract_docs(payload: dict[str, Any]) -> list:
    """从列表接口返回体里取出文档数组，兼容 data 为数组或 {docs:[...]} 两种结构。"""
    body = payload.get("data")
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("docs", "documents", "items", "list"):
            value = body.get(key)
            if isinstance(value, list):
                return value
    # 少数实现把数组放在顶层其它字段
    for key in ("docs", "documents", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_total(payload: dict[str, Any]) -> int | None:
    body = payload.get("data")
    if isinstance(body, dict):
        for key in ("total", "total_count", "count"):
            value = body.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    for key in ("total", "total_count", "count"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _normalize_run(doc: dict) -> str:
    """把文档的解析状态归一化为 DONE / RUNNING / FAIL / CANCEL / UNSTART。

    优先用 run 字段；缺失时用 progress 推断（1.0 视为已完成）。
    """
    run = (doc.get("run") or "").strip().upper()
    if run:
        if run == "FAILED":
            return "FAIL"
        return run
    try:
        progress = float(doc.get("progress", 0) or 0)
    except (TypeError, ValueError):
        progress = 0.0
    if progress >= 1.0:
        return "DONE"
    if progress > 0:
        return "RUNNING"
    return "UNSTART"


def _date_progress_from_snapshot(snapshot: dict[str, Any]) -> dict[str, dict[str, int]]:
    result = {
        "ccdi": {"total": 0, "dated": 0, "missing": 0, "filename_dated": 0},
        "bit": {"total": 0, "dated": 0, "missing": 0, "filename_dated": 0},
    }
    for doc_key, meta in (snapshot.get("documents") or {}).items():
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


def _get_document_run(
    base_url: str, headers: dict, dataset_id: str, document_id: str
) -> str | None:
    """查询单个文档的解析状态。"""
    try:
        resp = requests.get(
            f"{base_url}/datasets/{dataset_id}/documents/{document_id}",
            headers=headers,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            return None
        doc = data.get("data") or {}
        if isinstance(doc, list) and doc:
            doc = doc[0]
        if not isinstance(doc, dict):
            return None
        return _normalize_run(doc)
    except Exception:
        return None


def _count_dataset_docs(base_url: str, headers: dict, dataset_id: str) -> dict[str, int]:
    """统计知识库内文档状态数量（只读）。"""
    counts = {"done": 0, "running": 0, "pending": 0, "failed": 0}
    items = _list_documents(base_url, headers, dataset_id)
    if not items:
        return counts
    # 列表已带回 run 的直接统计，其余的并发查询文档详情
    pending_ids = [doc_id for doc_id, run in items if run is None]
    runs: dict[str, str | None] = {doc_id: run for doc_id, run in items if run}
    if pending_ids:
        with ThreadPoolExecutor(max_workers=12) as pool:
            for doc_id, run in zip(
                pending_ids,
                pool.map(
                    lambda did: _get_document_run(base_url, headers, dataset_id, did),
                    pending_ids,
                ),
            ):
                runs[doc_id] = run
    for doc_id, _ in items:
        run = runs.get(doc_id)
        if run == "DONE":
            counts["done"] += 1
        elif run == "RUNNING":
            counts["running"] += 1
        elif run in ("FAIL", "CANCEL"):
            counts["failed"] += 1
        else:
            counts["pending"] += 1
    return counts


def get_agent_status(
    config: dict[str, Any], state: CrawlerState | None = None
) -> dict[str, Any]:
    """只读查询学科智能体（RAGFlow）连接状态与两个知识库的文档状态统计。"""
    rag_cfg = config.get("ragflow", {})
    base_url = (rag_cfg.get("base_url") or "").rstrip("/")
    api_key = rag_cfg.get("api_key") or ""
    result: dict[str, Any] = {
        "ok": False,
        "base_url": base_url,
        "api_key": api_key,
        "api_key_masked": _mask_api_key(api_key),
        "dataset_name_ccdi": rag_cfg.get("dataset_name_ccdi", "中纪检"),
        "dataset_name_bit": rag_cfg.get("dataset_name_bit", "校纪检"),
        "error": None,
        "datasets": [],
    }
    if state is not None:
        snap = state.snapshot()
        queue = snap.get("parse_queue") or {}
        result["upload_state"] = {
            "progress": (snap.get("progress") or {}).get("upload") or {},
            "parse_queue": queue,
            "migration": snap.get("dataset_migration") or {},
            "version_index": snap.get("version_index_meta") or {},
            "backfill_status": snap.get("backfill_status") or {},
            "date_progress": _date_progress_from_snapshot(snap),
            "maintenance_flags": snap.get("maintenance_flags") or {},
        }
    up_cfg = config.get("upload", {}) or {}
    result["parse_settings"] = {
        "max_parsing_documents": int(up_cfg.get("max_parsing_documents", 500) or 500),
        "parse_timeout_minutes": float(up_cfg.get("parse_timeout_minutes", 30) or 30),
        "parse_poll_seconds": int(up_cfg.get("parse_poll_seconds", 10) or 10),
        "parse_retry_rounds": int(up_cfg.get("parse_retry_rounds", 1) or 0),
    }
    if not base_url or not api_key:
        result["error"] = "未配置 RAGFlow 地址或 API Key"
        return result

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(
            f"{base_url}/datasets",
            headers=headers,
            params={"page": 1, "page_size": 200},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            result["error"] = f"连接失败: {data.get('message', '未知错误')}"
            return result
        dataset_list = data.get("data") or []
    except Exception as exc:
        result["error"] = f"连接失败: {exc}"
        return result

    def resolve_dataset(name: str, configured_id: str):
        """返回 (dataset_id, document_count)。"""
        if configured_id:
            for ds in dataset_list:
                if ds.get("id") == configured_id:
                    return configured_id, ds.get("document_count")
            return configured_id, None
        for ds in dataset_list:
            if ds.get("name") == name:
                return ds.get("id"), ds.get("document_count")
        return None, None

    for label, name, configured_id in (
        ("中纪检", rag_cfg.get("dataset_name_ccdi", "中纪检"), rag_cfg.get("dataset_id_ccdi", "")),
        ("校纪检", rag_cfg.get("dataset_name_bit", "校纪检"), rag_cfg.get("dataset_id_bit", "")),
    ):
        dataset_id, doc_count = resolve_dataset(name, configured_id)
        if not dataset_id:
            result["datasets"].append(
                {
                    "name": label,
                    "dataset_id": "",
                    "found": False,
                    "total": 0,
                    "counted": 0,
                    "done": 0,
                    "running": 0,
                    "pending": 0,
                    "failed": 0,
                }
            )
            continue
        counts = _count_dataset_docs(base_url, headers, dataset_id)
        counted = sum(counts.values())
        total = int(doc_count) if isinstance(doc_count, (int, float)) else counted
        logger.info(
            "知识库 %s：声明总数 %s，实际统计 %s（已完成 %s / 解析中 %s / 上传中 %s / 失败 %s）",
            label,
            total,
            counted,
            counts["done"],
            counts["running"],
            counts["pending"],
            counts["failed"],
        )
        if isinstance(doc_count, (int, float)) and counted < int(doc_count):
            logger.warning(
                "知识库 %s：仅统计到 %s 条，少于声明总数 %s，可能分页未取全",
                label,
                counted,
                int(doc_count),
            )
        result["datasets"].append(
            {
                "name": label,
                "dataset_id": dataset_id,
                "found": True,
                "total": total,
                "counted": counted,
                **counts,
            }
        )

    result["ok"] = True
    return result
