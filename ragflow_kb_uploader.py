"""
RAGFlow 知识库上传与解析（参考 ragflow_kb_manager.py，独立实现供主程序调用）。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from crawl_common import wait_while_paused
from crawler_state import CrawlerState

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".md", ".mdx",
    ".csv", ".xlsx", ".xls", ".ppt", ".pptx",
    ".jpeg", ".jpg", ".png", ".tif", ".gif",
}


class RagflowKbUploader:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _check(self, resp: requests.Response, action: str) -> dict[str, Any]:
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"{action} 失败: {data.get('message', '未知错误')}")
        return data

    def list_datasets(self) -> list[dict[str, Any]]:
        resp = requests.get(
            f"{self.base_url}/datasets",
            headers=self.headers,
            params={"page": 1, "page_size": 200},
            timeout=60,
        )
        data = self._check(resp, "列出知识库")
        return data.get("data") or []

    def find_dataset_id_by_name(self, name: str) -> str | None:
        for ds in self.list_datasets():
            if ds.get("name") == name:
                return ds.get("id")
        return None

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
        if configured_id:
            return configured_id
        found = self.find_dataset_id_by_name(name)
        if found:
            return found
        return self.create_dataset(
            name,
            chunk_method=chunk_method,
            chunk_token_num=chunk_token_num,
            description=f"纪律法规爬虫自动创建: {name}",
        )

    def upload_documents(self, dataset_id: str, file_paths: list[str]) -> list[str]:
        files = []
        opened = []
        for p in file_paths:
            path = Path(p)
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if not path.is_file():
                continue
            fobj = path.open("rb")
            opened.append(fobj)
            files.append(("file", (path.name, fobj)))
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
            return [doc["id"] for doc in data.get("data") or []]
        finally:
            for fobj in opened:
                fobj.close()

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
) -> int:
    """上传指定来源的本地 PDF，返回成功上传数量。

    immediate=True 时上传全部尚未上传的文件（首次运行用）；
    否则仅上传已保存满 min_age_days 的文件。
    """
    keys = state.pending_upload_keys(
        source, min_age_days, max_age_days, immediate=immediate
    )
    if not keys:
        return 0

    total = len(keys)
    state.set_progress(
        "upload",
        status="running",
        current=0,
        total=total,
        message=f"上传 {source}: 0/{total}",
    )
    uploaded = 0
    batch: list[str] = []
    batch_keys: list[str] = []
    batch_size = 5

    def flush_batch() -> None:
        nonlocal uploaded, batch, batch_keys
        if not batch:
            return
        state.set_progress(
            "upload",
            current=uploaded,
            message=f"上传 {source}: {uploaded}/{total}（正在推送 {len(batch)} 个文件…）",
        )
        doc_ids = uploader.upload_documents(dataset_id, batch)
        uploader.start_parsing(dataset_id, doc_ids)
        for key in batch_keys:
            state.mark_uploaded(key, dataset_id)
            uploaded += 1
        batch = []
        batch_keys = []
        # current 按“已完成”计数，保证进度条反映真实上传进度
        state.set_progress(
            "upload", current=uploaded, message=f"上传 {source}: {uploaded}/{total}"
        )

    for key in keys:
        if stop_event and stop_event.is_set():
            break
        wait_while_paused(pause_event, stop_event)
        if stop_event and stop_event.is_set():
            break
        meta = state.snapshot()["documents"].get(key, {})
        pdf_path = meta.get("pdf_path")
        if not pdf_path or not Path(pdf_path).is_file():
            continue
        batch.append(pdf_path)
        batch_keys.append(key)
        if len(batch) >= batch_size:
            flush_batch()

    if not (stop_event and stop_event.is_set()):
        flush_batch()
    if stop_event and stop_event.is_set():
        state.set_progress("upload", status="idle", message=f"{source} 上传已终止: {uploaded}")
    else:
        state.set_progress("upload", status="idle", message=f"{source} 上传完成: {uploaded}")
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
    """按配置对中纪检、校纪检两个库执行待上传批次。"""
    rag_cfg = config.get("ragflow", {})
    up_cfg = config.get("upload", {})
    min_age = int(up_cfg.get("min_age_days", 3))
    max_age = int(up_cfg.get("max_age_days", 4))
    chunk_method = rag_cfg.get("chunk_method", "laws")
    chunk_token = int(rag_cfg.get("chunk_token_num", 512))

    uploader = RagflowKbUploader(
        rag_cfg["base_url"],
        rag_cfg["api_key"],
    )

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
        ),
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


def get_agent_status(config: dict[str, Any]) -> dict[str, Any]:
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
