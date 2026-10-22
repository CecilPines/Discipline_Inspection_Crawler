"""监控 Web UI：展示进度、文件列表，并支持运行/测试模式下的手动爬取与上传。"""

from __future__ import annotations

import logging
import time
from datetime import date
from functools import wraps

from flask import Flask, jsonify, render_template_string, request

from crawl_ccdi_regulations import CCDI_CATEGORY_PATHS

logger = logging.getLogger(__name__)


def _safe_api(name: str):
    """接口兜底：任何异常都转成 JSON 错误，别让一次请求把整个 UI 线程带崩。"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.exception("接口 %s 处理失败", name)
                return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

        return wrapper

    return decorator


def _ccdi_category_rows(snap: dict) -> list[dict]:
    """把「子分类定义 + 状态文件里的进度 + 已登记文档数」合并成界面用的列表。"""
    status = (snap.get("category_status", {}) or {}).get("ccdi", {}) or {}
    counts: dict[str, int] = {}
    for doc in (snap.get("documents", {}) or {}).values():
        if doc.get("source") != "ccdi":
            continue
        path = doc.get("category_path") or ""
        counts[path] = counts.get(path, 0) + 1
    rows = []
    for main_cat, sub_cat, code in CCDI_CATEGORY_PATHS:
        entry = status.get(code) or {}
        name = f"{main_cat}/{sub_cat}"
        crawled = counts.get(name, 0)
        total = entry.get("total")
        estimated = bool(entry.get("total_estimated", True))
        state = entry.get("status") or "pending"
        derived = False
        # 重新启动时分类状态会被重置成 pending：已登记数达到总数就说明这个分类是完成的
        if state == "pending" and total and crawled >= int(total):
            state = "done"
            derived = True  # 这是「此前已完成」，不是本次刚扫完
        # 标了完成、但登记数明显少于（精确的）总数：说明那一轮没真正爬全
        if state == "done" and not estimated and total and crawled < int(total):
            state = "partial"
        rows.append(
            {
                "code": code,
                "main": main_cat,
                "sub": sub_cat,
                "name": name,
                "total": total,
                "total_estimated": estimated,
                "crawled": crawled,
                "status": state,
                "derived": derived,
                "reason": entry.get("reason") or "",
            }
        )
    # ---- 「其他」统计：一级/二级都用减法算 ----
    main_totals: dict[str, int] = {}
    for code, main_name in (
        ("law_validity_01", "党内法规制度"),
        ("law_validity_02", "国家法律法规"),
    ):
        entry = status.get(code) or {}
        value = entry.get("total")
        if isinstance(value, int):
            main_totals[main_name] = value
    subs_by_main: dict[str, list[int]] = {}
    subs_count: dict[str, int] = {}
    for main_cat, sub_cat, code in CCDI_CATEGORY_PATHS:
        subs_count[main_cat] = subs_count.get(main_cat, 0) + 1
        entry = status.get(code) or {}
        value = entry.get("total")
        if isinstance(value, int):
            subs_by_main.setdefault(main_cat, []).append(value)

    for main_name in ("党内法规制度", "国家法律法规"):
        name = f"{main_name}/其他"
        crawled = counts.get(name, 0)
        total = None
        totals = subs_by_main.get(main_name, [])
        if (
            main_name in main_totals
            and len(totals) == subs_count.get(main_name, 0)
        ):
            total = max(0, main_totals[main_name] - sum(totals))
        rows.append(
            {
                "code": f"other_{main_name}",
                "main": main_name,
                "sub": "其他",
                "name": name,
                "total": total,
                "total_estimated": False,
                "crawled": crawled,
                # 「其他」是兜底目录，不参与"待爬/部分完成"判断，统一显示完成
                "status": "done",
                "derived": False,
                "reason": "" if total is not None else "总数未知（缺一级目录统计）",
            }
        )

    overall = ((snap.get("total_estimate") or {}).get("ccdi") or {}).get("value")
    other_total = None
    if isinstance(overall, int) and len(main_totals) == 2:
        other_total = max(0, overall - sum(main_totals.values()))
    crawled = counts.get("其他", 0)
    rows.append(
            {
                "code": "other_root",
                "main": "其他",
                "sub": "",
                "name": "其他（未归类）",
                "total": other_total,
                "total_estimated": False,
                "crawled": crawled,
                "status": "done",
                "derived": False,
                "reason": "",
            }
        )
    return rows


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>纪律法规爬虫监控</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif; margin: 0 auto; padding: 26px 22px 60px; max-width: 1200px; background: #f4f6fb; color: #1f2937; font-size: 14px; line-height: 1.5; }
    h1 { margin: 0 0 6px; font-size: 22px; letter-spacing: .3px; }
    h1::before { content: ""; display: inline-block; width: 6px; height: 20px; border-radius: 3px; background: #2563eb; margin-right: 10px; vertical-align: -3px; }
    .sub { color: #6b7280; margin: 0 0 22px; font-size: 13px; }
    .card { background: #fff; border: 1px solid #e8edf5; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(16,24,40,.05); margin-bottom: 18px; }
    .card h2 { font-size: 15px; margin: 0 0 14px; color: #111827; font-weight: 600; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 18px; align-items: stretch; margin-bottom: 18px; }
    .grid .card { margin-bottom: 0; }
    .progress-card { position: relative; text-align: center; }
    .progress-card .stat-row { justify-content: center; }
    .pc-title { font-size: 20px; font-weight: 700; letter-spacing: .5px; color: #111827; margin: 0 0 10px; }
    .pc-num { font-size: 26px; font-weight: 700; color: #111827; margin: 8px 0 2px; }
    .pc-msg { font-size: 13px; color: #6b7280; margin-top: 8px; word-break: break-word; }
    /* 中纪检「上次区间」小标签 */
    .range-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center; }
    .tag-range-label { background: #f1f5f9; color: #64748b; border: 1px solid #e2e8f0; }
    .tag-range-fwd { background: #e0edff; color: #1d4ed8; border: 1px solid #c7ddfd; }
    .tag-range-back { background: #f3e8ff; color: #7e22ce; border: 1px solid #e9d5ff; }
    .tag-range-max { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
    .tag-failed { background: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }
    .tag-noupdate { background: #ccfbf1; color: #0f766e; border: 1px solid #99f6e4; }
    .mini-btn { padding: 3px 10px; border: 1px solid #fecaca; border-radius: 999px;
      background: #fff1f2; color: #b91c1c; font-size: 12px; cursor: pointer; }
    .mini-btn:hover:not(:disabled) { background: #ffe4e6; }
    .agent-overview { display: grid; grid-template-columns: minmax(310px, .82fr) minmax(520px, 1.55fr); gap: 18px; align-items: stretch; }
    .agent-libs { display: flex; flex-direction: column; gap: 18px; min-height: 100%; }
    .agent-libs > .card { flex: 1 1 0; margin-bottom: 0; display: flex; flex-direction: column; justify-content: flex-start; padding-top: 18px; }
    .agent-task-card { display: flex; flex-direction: column; height: 100%; }
    .agent-dataset-card { text-align: center; min-width: 0; padding-left: 14px; padding-right: 14px; background: linear-gradient(180deg, #f7fbff 0%, #edf5ff 100%); border-color: #d9e8f8; }
    .agent-dataset-card .stat-row { justify-content: center; }
    .agent-dataset-card .lib-title { text-align: center; }
    .lib-id { margin-top: 7px; padding: 7px 2px; text-align: center; white-space: nowrap; font-family: Consolas, "Courier New", monospace; font-size: 10.8px; line-height: 1.45; color: #475569; background: #f8fafc; border: 1px solid #eef2f7; border-radius: 7px; overflow: hidden; }
    .task-id-list { display: flex; flex-wrap: wrap; gap: 5px; max-height: 156px; overflow: auto; margin-top: 8px; }
    .retry-box { border: 1px solid #fecaca; border-radius: 8px; background: #fff7f7; margin-top: 9px; overflow: hidden; }
    .retry-box > summary { cursor: pointer; list-style: none; padding: 6px 8px; color: #b91c1c; font-size: 12px; font-weight: 600; }
    .retry-box > summary::-webkit-details-marker { display: none; }
    .retry-box > summary::before { content: "▸"; display: inline-block; margin-right: 5px; color: #ef4444; }
    .retry-box[open] > summary::before { transform: rotate(90deg); }
    .retry-box .task-id-list { padding: 0 7px 7px; margin-top: 0; max-height: 130px; }
    .backfill-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 12px 0; }
    .backfill-mini { border: 1px solid #dfe6f0; border-radius: 11px; padding: 12px; background: linear-gradient(180deg, #fff 0%, #f8fafc 100%); min-width: 0; box-shadow: 0 2px 8px rgba(15,23,42,.04); }
    .backfill-mini .bf-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 9px; }
    .backfill-mini .bf-title { font-size: 14px; font-weight: 700; color: #1e293b; }
    .bf-section { min-width: 0; }
    .bf-section-title { display: flex; align-items: center; gap: 6px; margin-bottom: 7px; }
    .bf-section-title .tag { font-weight: 600; }
    .bf-divider { height: 1px; background: linear-gradient(90deg, transparent, #d8e0eb 12%, #d8e0eb 88%, transparent); margin: 12px 0; }
    .bf-num-line { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin: 5px 0; }
    .backfill-mini .bf-num { font-size: 19px; font-weight: 750; color: #111827; }
    .backfill-mini .bf-msg { font-size: 11px; color: #64748b; margin-top: 5px; line-height: 1.45; word-break: break-word; }
    .backfill-mini .bar { height: 6px; margin-top: 6px; }
    .bf-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
    .bf-tags .tag { font-size: 10.5px; padding: 2px 7px; background: #eef4fb; color: #31547a; border: 1px solid #dae7f5; }
    .bf-error { margin-top: 8px; padding: 6px 8px; border-radius: 7px; background: #fff1f2; border: 1px solid #fecdd3; color: #b91c1c; font-size: 11px; text-align: left; word-break: break-word; }
    .year-panel { margin-top: 12px; border-top: 1px solid #e5eaf2; padding-top: 11px; }
    .year-panel > summary { cursor: pointer; list-style: none; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; color: #1e293b; font-weight: 600; }
    .year-panel > summary::-webkit-details-marker { display: none; }
    .year-panel > summary::before { content: "▸"; color: #94a3b8; display: inline-block; }
    .year-panel[open] > summary::before { transform: rotate(90deg); }
    .year-table-wrap { max-height: 340px; overflow: auto; margin-top: 9px; border: 1px solid #e5eaf2; border-radius: 8px; }
    .year-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
    .year-table th { position: sticky; top: 0; background: #f8fafc; color: #64748b; padding: 7px 8px; text-align: left; z-index: 1; }
    .year-table td { padding: 6px 8px; border-top: 1px solid #eef2f7; vertical-align: top; }
    .year-table .year-column-col { width: 92px; }
    .year-table .year-column { white-space: nowrap; }
    .year-table .page-position-col { width: 124px; }
    .year-table .page-position { white-space: nowrap; }
    .year-table .year-description { min-width: 310px; line-height: 1.65; }
    .year-table .year-description .bf-tags { margin-top: 0; }
    .year-table tr.current td { background: #eff6ff; }
    .year-table tr.current td:first-child { box-shadow: inset 3px 0 0 #3b82f6; }
    .year-table tr.year-main-row { cursor: pointer; }
    .year-table tr.year-main-row:hover td { background: #f8fbff; }
    .year-table tr.year-page-detail td { background: #f8fafc; padding: 8px 12px; }
    .year-page-table { width: 100%; border-collapse: collapse; font-size: 11px; }
    .year-page-table th, .year-page-table td { padding: 4px 7px; border-bottom: 1px solid #edf1f6; text-align: left; }
    .year-page-table th { color: #64748b; font-weight: 600; background: transparent; position: static; }
    .year-page-table .page-index { padding-left: 24px; width: 140px; white-space: nowrap; }
    .year-page-table .page-counts { width: 112px; white-space: nowrap; }
    .year-page-table .page-status { width: 96px; white-space: nowrap; }
    .year-page-table .page-ids { min-width: 360px; }
    .page-id-details > summary { cursor: pointer; color: #2563eb; font-size: 11px; list-style: none; display: flex; align-items: center; flex-wrap: wrap; gap: 5px; }
    .page-id-details > summary::-webkit-details-marker { display: none; }
    .page-id-details > summary::before { content: "▸"; display: inline-block; margin-right: 4px; }
    .page-id-details[open] > summary::before { transform: rotate(90deg); }
    .page-id-details .tag, .year-table .inline-num { padding: 1px 6px; font-size: 10.5px; }
    .page-id-box { margin-top: 6px; padding: 6px 8px; border-radius: 7px; border: 1px solid; line-height: 2; }
    .page-id-box.web { background: #eff6ff; border-color: #bfdbfe; }
    .page-id-box.local { background: #ecfdf5; border-color: #bbf7d0; }
    .page-id-box .box-label { display: inline-block; margin-right: 6px; font-weight: 600; font-size: 10.5px; }
    .page-id-box.web .box-label { color: #1d4ed8; }
    .page-id-box.local .box-label { color: #15803d; }
    .id-chip { display: inline-block; margin: 2px 4px 2px 0; padding: 1px 6px; border-radius: 5px; border: 1px solid; font-size: 10.5px; line-height: 1.45; }
    .id-chip.web { color: #1e40af; background: #dbeafe; border-color: #93c5fd; }
    .id-chip.local { color: #166534; background: #dcfce7; border-color: #86efac; }
    .id-chip.unmatched { color: #991b1b; background: #fee2e2; border-color: #f87171; font-weight: 700; }
    .year-progress-mini { height: 4px; background: #e8ecf4; border-radius: 999px; overflow: hidden; margin-top: 4px; min-width: 72px; }
    .year-progress-mini > span { display: block; height: 100%; background: linear-gradient(90deg, #60a5fa, #2563eb); }
    @media (max-width: 640px) { .backfill-grid { grid-template-columns: 1fr; } }
    .version-families { margin-top: 4px; }
    .section-toggle { border: none; background: transparent; color: #64748b; cursor: pointer; font-size: 12px; padding: 2px 5px; }
    .version-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; margin: 10px 0; }
    .version-controls input[type="search"] { flex: 1 1 260px; min-width: 180px; padding: 7px 9px; border: 1px solid #d5dbe5; border-radius: 8px; }
    .version-controls select { padding: 5px 7px; border: 1px solid #d5dbe5; border-radius: 6px; background: #fff; }
    .version-pager { display: flex; justify-content: center; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 12px; }
    .version-pager button { padding: 5px 10px; border: 1px solid #d5dbe5; border-radius: 7px; background: #fff; cursor: pointer; }
    .version-pager button.active { background: #2563eb; border-color: #2563eb; color: #fff; }
    .version-pager input { width: 54px; padding: 5px 7px; border: 1px solid #d5dbe5; border-radius: 6px; text-align: center; }
    .version-family { border: 1px solid #e5eaf2; border-radius: 9px; margin-top: 8px; background: #fbfcfe; }
    .version-family > summary { cursor: pointer; padding: 9px 11px; list-style: none; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .version-family > summary::-webkit-details-marker { display: none; }
    .version-family > summary::before { content: "▸"; color: #94a3b8; }
    .version-family[open] > summary::before { transform: rotate(90deg); }
    .version-row { display: grid; grid-template-columns: minmax(130px, 160px) minmax(220px, 1.25fr) minmax(180px, 1fr) 112px; gap: 12px; padding: 8px 11px; border-top: 1px solid #eef1f6; align-items: start; font-size: 12px; }
    .version-row > div { min-width: 0; overflow-wrap: anywhere; }
    .version-row .tag { max-width: 100%; white-space: normal; word-break: break-all; }
    .version-row.latest { background: #f0fdf4; }
    .version-row.version-head { background: #f8fafc; color: #64748b; font-weight: 600; font-size: 11px; }
    .badge-latest { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
    .dialog-mask { position: fixed; inset: 0; background: rgba(15,23,42,.48); display: none; align-items: center; justify-content: center; padding: 20px; z-index: 50; }
    .dialog-mask.open { display: flex; }
    .dialog-box { width: min(560px, 100%); background: #fff; border-radius: 14px; padding: 22px; box-shadow: 0 24px 60px rgba(15,23,42,.25); }
    .dialog-box h3 { margin: 0 0 10px; color: #991b1b; }
    .dialog-box p { margin: 8px 0; color: #475569; line-height: 1.7; }
    .dialog-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
    .dialog-actions button { padding: 8px 16px; border-radius: 8px; border: 1px solid #d5dbe5; background: #fff; cursor: pointer; }
    .dialog-actions .danger { color: #fff; background: #dc2626; border-color: #dc2626; }
    @media (max-width: 900px) { .agent-overview { grid-template-columns: 1fr; } .version-row { grid-template-columns: 64px 1fr; } }
    /* 中纪检：子分类进度 + 右侧两张卡片，等高排列 */
    .ccdi-bottom { display: flex; gap: 16px; align-items: stretch; margin-bottom: 18px; }
    .ccdi-cat-col { flex: 1 1 auto; min-width: 0; display: flex; }
    .ccdi-cat-col > .card { flex: 1 1 auto; margin-bottom: 0; }
    .ccdi-side { flex: 0 0 360px; display: flex; flex-direction: column; gap: 16px; }
    .ccdi-side .card { margin-bottom: 0; }
    .ccdi-side .card:last-child { flex: 1 1 auto; display: flex; flex-direction: column; }
    .range-tags { border: 1px solid #e6ebf3; background: #fbfcfe; border-radius: 10px;
      padding: 8px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
    .range-tags .tag { text-align: center; }
    .range-tags .tag-range-label { grid-column: 1 / -1; text-align: left; }
    .failed-actions { display: flex; gap: 8px; margin-top: 8px; }
    .failed-ids { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; max-height: 190px; overflow: auto; }
    .side-title { font-size: 15px; font-weight: 600; color: #111827; margin-bottom: 10px; }
    @media (max-width: 900px) {
      .ccdi-bottom { flex-direction: column; }
      .ccdi-side { flex: 0 0 auto; }
    }
    .pc-stop { position: absolute; top: 14px; right: 14px; width: 32px; height: 32px; font-size: 12px; }
    .pc-stop:hover:not(:disabled) { box-shadow: 0 4px 10px rgba(239,68,68,.28); }
    .stat { font-size: 24px; font-weight: 650; color: #111827; margin-top: 4px; }
    .lib-title { font-size: 15px; font-weight: 600; margin-bottom: 6px; }
    .card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .card-head h2 { margin: 0; }
    .th-sort { cursor: pointer; user-select: none; white-space: nowrap; }
    .th-sort .arrow { font-size: 10px; color: #bbb; margin-left: 4px; }
    .th-sort.active { color: #2563eb; }
    .th-sort.active .arrow { color: #2563eb; }
    .files-controls { display: flex; align-items: center; gap: 8px; font-size: 13px; }
    .files-controls button { padding: 5px 12px; border: 1px solid #d5dbe5; border-radius: 8px; background: #fff; cursor: pointer; font-size: 12px; color: #374151; transition: .15s; }
    .files-controls button:hover:not(:disabled) { border-color: #93b4f5; color: #2563eb; }
    .files-controls button:disabled { opacity: .5; cursor: not-allowed; }
    .files-controls select { padding: 4px 6px; border: 1px solid #d5dbe5; border-radius: 6px; font-size: 12px; }
    .pager { display: inline-flex; gap: 3px; align-items: center; }
    .pager button { min-width: 26px; padding: 3px 6px !important; font-size: 12px; border: 1px solid #d5dbe5; border-radius: 6px; background: #fff; color: #374151; cursor: pointer; }
    .pager button:hover:not(:disabled):not(.active) { border-color: #93b4f5; color: #2563eb; }
    .pager button.active { background: #2563eb; border-color: #2563eb; color: #fff; cursor: default; }
    .pager .gap { color: #9aa3af; font-size: 12px; padding: 0 2px; user-select: none; }
    .tag-cat { background: #e0f2fe; color: #0369a1; }
    .tag-src-ccdi { background: #dbeafe; color: #1d4ed8; }
    .tag-src-bit { background: #ede9fe; color: #6d28d9; }
    .tag-yes { background: #dcfce7; color: #15803d; }
    .tag-no { background: #f1f5f9; color: #64748b; }
    .tag-time { background: #eef2f7; color: #334155; border: 1px solid #e2e8f0; font-family: Consolas, "Courier New", monospace; }
    .tag-time.next { background: #e0edff; color: #1d4ed8; border-color: #c7ddfd; }
    .files-table { table-layout: fixed; }
    .files-table td { word-break: break-word; }
    .files-table td:first-child, .files-table td:last-child { white-space: nowrap; }
    .alert { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; font-size: 13px; }
    .alert-error { background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }
    .alert-warning { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
    .alert-info { background: #dbeafe; color: #1e40af; border: 1px solid #bfdbfe; }
    .alert button { border: none; background: transparent; cursor: pointer; color: inherit; font-size: 12px; text-decoration: underline; flex: 0 0 auto; }
    .stat-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; }
    .pill.done { background: #dcfce7; color: #15803d; }
    .pill.running { background: #dbeafe; color: #1d4ed8; }
    .pill.pending { background: #fef3c7; color: #b45309; }
    .pill.failed { background: #fee2e2; color: #b91c1c; }
    .muted { color: #6b7280; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #eef1f6; vertical-align: top; }
    th { background: #f8fafc; color: #4b5563; font-weight: 600; position: sticky; top: 0; z-index: 1; }
    tbody tr:hover { background: #f8fbff; }
    .bar { height: 8px; background: #e8ecf4; border-radius: 999px; overflow: hidden; margin-top: 8px; }
    .bar > span { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, #60a5fa, #2563eb); transition: width .3s ease; }
    .bar.indeterminate > span { width: 38%; background: linear-gradient(90deg, #bfdbfe, #60a5fa); animation: indet 1.3s linear infinite; }
    @keyframes indet { 0% { margin-left: -38%; } 100% { margin-left: 100%; } }
    .note { margin-top: 8px; font-size: 12px; color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 5px 9px; }
    .skipped-list { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
    .skipped-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; color: #7c2d12; background: #fff7ed; border: 1px solid #fed7aa; border-radius: 6px; padding: 3px 8px; }
    .skipped-item .sk-cat { font-weight: 600; word-break: break-all; }
    .skipped-item .sk-reason { flex: 0 0 auto; color: #9a3412; background: #ffedd5; border-radius: 999px; padding: 1px 8px; font-size: 11px; }
    /* 子分类进度面板（整行、可折叠） */
    .cat-panel > summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: 10px; padding: 2px 0 10px; }
    .cat-panel > summary::-webkit-details-marker { display: none; }
    .cat-panel > summary::before { content: "▸"; color: #94a3b8; transition: transform .15s; display: inline-block; }
    .cat-panel[open] > summary::before { transform: rotate(90deg); }
    .cat-summary-title { font-weight: 600; color: #111827; font-size: 14px; }
    .cat-summary-meta { color: #64748b; font-size: 12.5px; }
    .cat-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .cat-table th { text-align: left; padding: 7px 10px; background: #f8fafc; color: #64748b; font-weight: 600; border-bottom: 1px solid #eef1f6; position: static; }
    .cat-table td { padding: 6px 10px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
    .cat-table tr.current td { background: #eff6ff; }
    .cat-table tr.current td:first-child { box-shadow: inset 3px 0 0 #3b82f6; }
    .cat-table .num { font-family: Consolas, "Courier New", monospace; color: #475569; white-space: nowrap; }
    .cat-table .progress-mini { height: 5px; background: #e8ecf4; border-radius: 999px; overflow: hidden; margin-top: 4px; min-width: 90px; }
    .cat-table .progress-mini > span { display: block; height: 100%; background: linear-gradient(90deg, #60a5fa, #2563eb); }
    .cat-scroll { max-height: 380px; overflow-y: auto; }
    .tag-cat-pending { background: #f1f5f9; color: #64748b; }
    .tag-cat-running { background: #dbeafe; color: #1d4ed8; }
    .tag-cat-done { background: #dcfce7; color: #15803d; }
    .tag-cat-skipped { background: #fee2e2; color: #b91c1c; }
    .tag-cat-interrupted { background: #fef3c7; color: #b45309; }
    .tag-cat-partial { background: #ffedd5; color: #c2410c; }
    .tag-retry { background: #fff7ed; color: #9a3412; border: 1px solid #fed7aa; }
    .tag-retry-pending { background: #eef4ff; color: #1d4ed8; border: 1px solid #d6e4ff; }
    .url-line { font-family: Consolas, "Courier New", monospace; font-size: 11.5px; color: #475569; word-break: break-all; }
    .url-line.prev { color: #94a3b8; font-size: 11px; }
    .tag-http { font-family: Consolas, monospace; font-size: 11px; padding: 1px 6px; border-radius: 4px; background: #e5e7eb; color: #374151; }
    .tag-http.ok { background: #dcfce7; color: #15803d; }
    .tag-http.redirect { background: #dbeafe; color: #1d4ed8; }
    .tag-http.bad { background: #fee2e2; color: #b91c1c; }
    .tag-no-retry { background: #f8fafc; color: #94a3b8; border: 1px solid #e2e8f0; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
    .running { background: #dbeafe; color: #1d4ed8; }
    .idle { background: #e5e7eb; color: #374151; }
    .error { background: #fee2e2; color: #b91c1c; }
    .progress-card.is-error { border-color: #fca5a5; box-shadow: 0 0 0 3px rgba(239,68,68,.08); }
    .progress-card.progress-ccdi { background: linear-gradient(180deg, #f5f9ff 0%, #eaf3ff 100%); border-color: #d6e7fb; }
    .progress-card.progress-bit { background: linear-gradient(180deg, #f9f7ff 0%, #f0ebff 100%); border-color: #e3dafb; }
    .controls { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end; }
    .controls label { font-size: 13px; color: #444; display: flex; flex-direction: column; gap: 4px; }
    .controls select, .controls input { padding: 7px 9px; border: 1px solid #d5dbe5; border-radius: 8px; font-size: 13px; background: #fff; color: #1f2937; }
    .controls select:focus, .controls input:focus { outline: none; border-color: #93b4f5; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
    .controls button { padding: 8px 15px; border: none; border-radius: 8px; cursor: pointer; font-size: 13px; background: #eef1f6; color: #374151; transition: .15s; }
    .controls button:hover:not(:disabled) { filter: brightness(.96); }
    #top_btn_upload { background: #16a34a; color: #fff; }
    /* 图标按钮：顶栏控制按钮与卡片内的终止按钮共用 */
    button.icon-btn { padding: 0; border: none; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; color: #fff; background: #2563eb; line-height: 1; font-family: "Segoe UI Symbol", "Segoe UI Emoji", -apple-system, sans-serif; transition: .15s; cursor: pointer; }
    button.icon-btn:hover:not(:disabled) { filter: none; transform: translateY(-1px); }
    button.icon-btn.pause-mode { background: #f59e0b; }
    button.icon-btn.danger { background: #ef4444; }
    button.icon-btn:disabled { opacity: .5; cursor: not-allowed; }
    .controls .icon-btn { width: 42px; height: 42px; font-size: 15px; }
    .controls .icon-btn:hover:not(:disabled) { box-shadow: 0 4px 10px rgba(37,99,235,.3); }
    .controls .icon-btn.pause-mode:hover:not(:disabled) { box-shadow: 0 4px 10px rgba(245,158,11,.3); }
    .controls .icon-btn.danger:hover:not(:disabled) { box-shadow: 0 4px 10px rgba(239,68,68,.3); }
    .controls button:disabled { opacity: .5; cursor: not-allowed; }
    .tab-group { display: inline-flex; border: 1px solid #d5dbe5; border-radius: 8px; overflow: hidden; }
    .tab-group .tab { padding: 7px 14px; border: none; background: #fff; cursor: pointer; font-size: 13px; color: #444; }
    .tab-group .tab:hover:not(.active) { background: #f1f5fb; }
    .tab-group .tab.active { background: #2563eb; color: #fff; }
    .tabs { display: flex; gap: 4px; border-bottom: 1px solid #d5dbe5; margin: 4px 0 14px; }
    .tabs .tab { padding: 8px 18px; border: 1px solid transparent; border-bottom: none; border-radius: 8px 8px 0 0; background: #eef1f6; color: #555; cursor: pointer; font-size: 13px; margin-bottom: -1px; }
    .tabs .tab:hover:not(.active) { background: #e6ebf4; }
    .tabs .tab.active { background: #fff; border-color: #d5dbe5; color: #111; font-weight: 600; }
    .mode-panel .controls { margin-top: 10px; }
    .field-block { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: #444; }
    .interval-row { display: flex; gap: 6px; align-items: center; }
    .interval-row input { width: 80px; }
    .interval-row input[type="time"] { width: 120px; }
    .sched-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .tag-first { background: #fef3c7; color: #b45309; border: 1px solid #fde68a; }
    .tag-daily { background: #eef2f7; color: #475569; border: 1px solid #e2e8f0; }
    .config-view { margin-top: 12px; border-top: 1px dashed #e5e9f2; padding-top: 10px; }
    .cfg-details { margin-top: 12px; }
    .cfg-details > summary { cursor: pointer; font-size: 13px; color: #2563eb;
      list-style: none; display: inline-flex; align-items: center; gap: 6px; user-select: none; }
    .cfg-details > summary::-webkit-details-marker { display: none; }
    .cfg-details > summary::before { content: "▸"; font-size: 12px; transition: transform .15s; }
    .cfg-details[open] > summary::before { transform: rotate(90deg); }
    .cfg-details > summary:hover { color: #1d4ed8; text-decoration: underline; }
    .cfg-row { display: flex; gap: 12px; font-size: 12px; padding: 2px 0; }
    .cfg-key { color: #6b7280; min-width: 260px; }
    .cfg-val { color: #111827; font-family: Consolas, "Courier New", monospace; word-break: break-all; }
    #ctl_status { margin-top: 10px; font-size: 13px; }
    .hint { font-size: 12px; color: #9aa3af; margin-top: 8px; line-height: 1.6; }
  </style>
</head>
<body>
  <h1>纪律法规爬虫监控</h1>
  <p class="sub">界面自动刷新：运行中 1 秒 / 空闲 5 秒 · 输出目录：<code id="output_dir_display">{{ output_dir }}</code></p>

  <div id="alerts"></div>

  <div class="card">
    <h2>总控制</h2>
    <div class="controls">
      <button id="top_btn_toggle" class="icon-btn" onclick="toggleRun()" title="开始">▶</button>
      <button id="top_btn_stop" class="icon-btn danger" onclick="stopTop()" title="终止所有任务">■</button>
      <button id="top_btn_upload" onclick="startUpload()">立即上传</button>
      <label>保存位置
        <input id="top_output_dir" type="text" style="width:320px" />
      </label>
      <button id="top_btn_output_dir" onclick="setOutputDir('top')">应用保存位置</button>
      <div class="field-block">
        <span>爬取方式</span>
        <div class="tab-group" id="parallel_tabs">
          <button type="button" class="tab" data-parallel="sequential" onclick="setParallel(false)">顺序</button>
          <button type="button" class="tab" data-parallel="parallel" onclick="setParallel(true)">并行</button>
        </div>
      </div>
      <div class="field-block">
        <span>上传方式</span>
        <div class="tab-group" id="upload_tabs">
          <button type="button" class="tab" data-upload="manual" onclick="setUpload('manual')">手动上传</button>
          <button type="button" class="tab" data-upload="auto" onclick="setUpload('auto')">自动上传</button>
        </div>
      </div>
      <span class="muted" id="top_state">—</span>
    </div>
    <div id="top_upload_progress" style="display:none;margin-top:10px">
      <div class="muted" id="top_upload_text">—</div>
      <div class="bar" id="top_upload_bar_wrap"><span id="top_upload_bar" style="width:0%"></span></div>
    </div>
    <div class="hint">「开始」按当前模式启动任务；任务运行中点击「暂停」可挂起，「开始」会从暂停处继续；「终止」会终止所有任务。爬取方式选「并行」时中纪检与校纪检同时爬取（来源为「全部」时生效）。</div>
  </div>

  <div class="card">
    <h2>任务控制</h2>
    <div class="tabs" id="mode_tabs">
      <button type="button" class="tab" data-mode="run" onclick="setMode('run')">运行模式</button>
      <button type="button" class="tab" data-mode="test" onclick="setMode('test')">测试模式</button>
    </div>

    <div class="mode-panel" id="panel_run">
      <div class="muted">运行模式：全量爬取全部来源（中纪检 + 校纪检）。</div>
      <div class="controls">
        <label>每日爬取时间（留空=不启用）
          <input id="run_crawl_time" type="time" onchange="saveScheduleTimes('run')" />
        </label>
        <label>每日上传时间（留空=不启用）
          <input id="run_upload_time" type="time" onchange="saveScheduleTimes('run')" />
        </label>
        <label>上传间隔（天，0=随爬取立即上传）
          <input id="run_upload_days" type="number" min="0" max="365" step="1" value="3"
                 onchange="saveUploadDays(this)" />
        </label>
      </div>
      <div class="stat-row" id="run_schedule_info"></div>
      <details class="cfg-details">
        <summary>配置（只读，点击展开 / 收起）</summary>
        <div class="config-view" id="run_config_view"></div>
      </details>
    </div>

    <div class="mode-panel" id="panel_test" style="display:none">
      <div class="muted">测试模式：可选择来源，并限制每个来源的爬取条数。</div>
      <div class="controls">
        <label>来源
          <select id="test_source" onchange="saveSettings()">
            <option value="all">全部</option>
            <option value="ccdi">中纪检</option>
            <option value="bit">校纪检</option>
          </select>
        </label>
        <label>测试爬取条数（每个来源）
          <input id="test_count" type="number" min="1" value="5" onchange="saveSettings()" />
        </label>
        <label>校纪检 PDF 缩放（0.1 ~ 2.0）
          <input id="test_pdf_scale_bit" type="number" min="0.1" max="2" step="0.05" value="0.9" onchange="savePdfScale()" />
        </label>
        <div class="field-block">
          <span>全量测试</span>
          <div class="tab-group" id="fulltest_tabs">
            <button type="button" class="tab" data-fulltest="off" onclick="setFullTest(false)">按条数</button>
            <button type="button" class="tab" data-fulltest="on" onclick="setFullTest(true)">全量测试</button>
          </div>
        </div>
        <div class="field-block">
          <span>定时方式（爬取与上传）</span>
          <div class="sched-row">
            <div class="tab-group" id="testsched_tabs">
              <button type="button" class="tab" data-testsched="interval" onclick="setTestSched('interval')">按间隔</button>
              <button type="button" class="tab" data-testsched="daily" onclick="setTestSched('daily')">按每日时间点</button>
            </div>
            <div id="test_interval_block" class="interval-row">
              <span class="muted">爬取</span>
              <input id="test_crawl_interval" type="number" min="0" step="1" value="0" title="0=关闭" onchange="saveTestIntervals()" />
              <select id="test_crawl_unit" onchange="saveTestIntervals()">
                <option value="1">分钟</option>
                <option value="60">小时</option>
              </select>
              <span class="muted">上传</span>
              <input id="test_upload_interval" type="number" min="0" step="1" value="0" title="0=关闭" onchange="saveTestIntervals()" />
              <select id="test_upload_unit" onchange="saveTestIntervals()">
                <option value="1">分钟</option>
                <option value="60">小时</option>
              </select>
            </div>
            <div id="test_daily_block" class="interval-row" style="display:none">
              <span class="muted">爬取</span>
              <input id="test_crawl_time" type="time" title="留空=不启用" onchange="saveScheduleTimes('test')" />
              <span class="muted">上传</span>
              <input id="test_upload_time" type="time" title="留空=不启用" onchange="saveScheduleTimes('test')" />
              <span class="muted">每</span>
              <input id="test_upload_days" type="number" min="0" max="365" step="1" value="3"
                     title="0=随爬取立即上传" onchange="saveUploadDays(this)" />
              <span class="muted">天上传</span>
            </div>
          </div>
        </div>
      </div>
      <div class="stat-row" id="test_schedule_info"></div>
      <div class="hint" id="test_sched_hint"></div>
      <div class="hint">间隔到点后按上面的「来源 + 测试爬取条数」执行：来源为「全部」时，中纪检、校纪检各爬该条数。间隔上传会把当前所有未上传的文件推送到知识库；爬取与上传不会同时进行，若到点时任务正忙则顺延到下一个间隔。</div>
    </div>

    <div class="hint">选择模式后，用上方「总控制」的开始/暂停/终止来运行。上传选「自动」时看「上传间隔（天）」：填 0 → 每次爬取结束后立即上传全部未上传文件（和首次运行一样）；填 N → 每 N 天到上传时间点统一上传一次。选「手动」时，爬取完成后点「立即上传」。</div>
    <div class="muted" id="ctl_status">—</div>
  </div>

  <div class="card">
    <h2>学科智能体信息</h2>
    <div class="controls">
      <label>地址
        <input id="agent_base_url" type="text" style="width:190px" />
      </label>
      <label>API
        <input id="agent_api_key" type="text" style="width:340px" />
      </label>
      <label>中纪检库名称
        <input id="agent_name_ccdi" type="text" style="width:160px" />
      </label>
      <label>校纪检库名称
        <input id="agent_name_bit" type="text" style="width:160px" />
      </label>
      <button id="agent_save_btn" onclick="saveAgentConfig()">保存配置</button>
      <button id="agent_refresh_btn" onclick="refreshAgentStatus()">手动刷新</button>
      <button id="agent_migrate_btn" onclick="openMigrationDialog()" style="background:#b91c1c;color:#fff">迁库</button>
    </div>
    <div id="agent_msg" class="hint" style="display:none;color:#16a34a;margin-top:10px"></div>
    <div id="agent_error" class="hint" style="display:none;color:#dc2626;margin-top:10px"></div>
    <div class="agent-overview" style="margin-top:14px">
      <div id="agent_datasets" class="agent-libs"></div>
      <div class="card agent-task-card" id="agent_task_card"></div>
    </div>
  </div>

  <div class="grid" id="summary"></div>
    <div class="ccdi-bottom">
      <div id="category_panel" class="ccdi-cat-col"></div>
      <div class="ccdi-side">
        <div class="card side-card" id="range_card"></div>
        <div class="card side-card" id="failed_card"></div>
      </div>
    </div>

  <div class="card">
    <div class="card-head">
      <h2>历史记录</h2>
      <div class="files-controls">
        <span class="muted">每页
          <select id="files_page_size" onchange="setFilesPageSize(this.value)">
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
        </span>
        <button id="files_prev" onclick="setFilesPage(filesPage - 1)">上一页</button>
        <span class="pager" id="files_pager"></span>
        <span class="muted" id="files_page_info">—</span>
        <button id="files_next" onclick="setFilesPage(filesPage + 1)">下一页</button>
      </div>
    </div>
    <table class="files-table">
      <colgroup>
        <col style="width:90px" />
        <col />
        <col style="width:220px" />
        <col style="width:170px" />
        <col style="width:90px" />
      </colgroup>
      <thead>
        <tr>
          <th class="th-sort" onclick="sortFilesBy('source')">来源<span class="arrow" id="arrow_source"></span></th>
          <th class="th-sort" onclick="sortFilesBy('title')">标题<span class="arrow" id="arrow_title"></span></th>
          <th class="th-sort" onclick="sortFilesBy('category_path')">分类<span class="arrow" id="arrow_category_path"></span></th>
          <th class="th-sort" onclick="sortFilesBy('crawled_at')">爬取时间<span class="arrow" id="arrow_crawled_at"></span></th>
          <th class="th-sort" onclick="sortFilesBy('uploaded')">已上传<span class="arrow" id="arrow_uploaded"></span></th>
        </tr>
      </thead>
      <tbody id="files"></tbody>
    </table>
  </div>

  <div class="card" id="versions_card">
    <div class="card-head">
      <h2><button id="version_toggle_btn" class="section-toggle" onclick="toggleVersionsCard()" title="展开/收起">▼</button> 多版本文档</h2>
      <span class="muted">仅最新版本会进入学科智能体</span>
    </div>
    <div id="version_card_body">
      <div class="version-controls">
        <input id="version_search" type="search" placeholder="搜索标题、链接后缀或位置" oninput="changeVersionSearch(this.value)" />
        <div class="tab-group" id="version_source_tabs">
          <button type="button" class="tab active" data-source="all" onclick="setVersionSource('all')">全部</button>
          <button type="button" class="tab" data-source="ccdi" onclick="setVersionSource('ccdi')">中纪检</button>
          <button type="button" class="tab" data-source="bit" onclick="setVersionSource('bit')">校纪检</button>
        </div>
        <label class="muted">每页
          <select id="version_page_size" onchange="changeVersionPageSize(this.value)">
            <option value="5">5</option>
            <option value="10" selected>10</option>
            <option value="20">20</option>
            <option value="50">50</option>
          </select>
        </label>
      </div>
      <div id="version_families" class="version-families"></div>
      <div class="version-pager">
        <button onclick="setVersionPage(versionPage - 1)">上一页</button>
        <span id="version_pager"></span>
        <button onclick="setVersionPage(versionPage + 1)">下一页</button>
        <span class="muted">跳至</span>
        <input id="version_jump" type="number" min="1" value="1" onchange="jumpVersionPage(this.value)" />
        <span class="muted">页</span>
        <span id="version_page_info" class="muted">—</span>
      </div>
    </div>
  </div>

  <div class="dialog-mask" id="migration_dialog" role="dialog" aria-modal="true" aria-labelledby="migration_title">
    <div class="dialog-box">
      <h3 id="migration_title">确认迁移学科智能体知识库？</h3>
      <p>此操作会删除当前的「中纪检」和「校纪检」知识库，然后创建同名新库，并把本地每个标题族的最新版本重新上传。</p>
      <p>迁库前会尽量补录历史发布日期；旧库数据删除后无法通过本程序恢复。任务执行期间不能爬取或上传。</p>
      <div class="dialog-actions">
        <button type="button" onclick="closeMigrationDialog()">取消</button>
        <button type="button" class="danger" id="migration_confirm_btn" onclick="confirmMigration()">再次确认并迁库</button>
      </div>
    </div>
  </div>

<script>
async function api(url, options) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

function setCtlStatus(text, isError) {
  const el = document.getElementById('ctl_status');
  el.textContent = text;
  el.style.color = isError ? '#dc2626' : '#16a34a';
}

let uiMode = 'run';
let uiUpload = 'manual';
let agentFullApiKey = '';
let agentMaskedApiKey = '';
let prevUploadRunning = false;
let prevRunningAny = false;
let uiRunning = false;
let uiPaused = false;
// 状态文件里是否有来源仍标记为 running（可能是终端/其它进程在跑）
let uiStateBusy = false;
let uiParallel = true;
let uiFullTest = false;
let uiTestSched = 'interval';
let filesPage = 1;
let filesPageSize = 20;
let filesSort = 'crawled_at';
let filesDir = 'desc';
let filesPages = 1;
let uploadHideTimer = null;
let uploadSessionActive = false;
let lastUploadSig = null;
let uiYearPanelOpen = true;
const expandedYears = new Set();
const expandedPageDetails = new Set();
let allVersionFamilies = [];
let versionPage = 1;
let versionPageSize = 10;
let versionSource = 'all';
let versionQuery = '';
let versionCardOpen = true;

function applyVisual() {
  const auto = (uiUpload === 'auto');
  document.getElementById('panel_run').style.display = (uiMode === 'run') ? 'block' : 'none';
  document.getElementById('panel_test').style.display = (uiMode === 'test') ? 'block' : 'none';
  document.querySelectorAll('#mode_tabs .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.mode === uiMode);
  });
  document.querySelectorAll('#upload_tabs .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.upload === uiUpload);
  });
  document.querySelectorAll('#parallel_tabs .tab').forEach(t => {
    t.classList.toggle('active', (t.dataset.parallel === 'parallel') === uiParallel);
  });
  document.querySelectorAll('#fulltest_tabs .tab').forEach(t => {
    t.classList.toggle('active', (t.dataset.fulltest === 'on') === uiFullTest);
  });
  document.querySelectorAll('#testsched_tabs .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.testsched === uiTestSched);
  });
  const isInterval = (uiTestSched !== 'daily');
  const intervalBlock = document.getElementById('test_interval_block');
  if (intervalBlock) intervalBlock.style.display = isInterval ? 'flex' : 'none';
  const dailyBlock = document.getElementById('test_daily_block');
  if (dailyBlock) dailyBlock.style.display = isInterval ? 'none' : 'flex';
  const schedHint = document.getElementById('test_sched_hint');
  if (schedHint) {
    schedHint.textContent = isInterval
      ? '按上面的间隔自动触发爬取与上传（来源为「全部」时两个来源并行）。'
      : '按上面的「每日爬取/上传时间」触发：爬取使用测试模式的来源与条数，上传推送全部未上传文件。';
  }
  document.getElementById('top_btn_upload').style.display = auto ? 'none' : 'inline-block';
}

// 用户正在编辑的输入框不要被轮询回写覆盖（否则会出现“打字/点击上下键不灵敏”）
function setInputValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (document.activeElement === el) return;
  el.value = value;
}

function applySettings(s) {
  uiMode = (s.mode === 'test') ? 'test' : 'run';
  uiUpload = s.upload_mode === 'auto' ? 'auto' : 'manual';
  uiParallel = !!s.parallel;
  uiFullTest = !!s.full_test;
  uiTestSched = (s.test_schedule_mode === 'daily') ? 'daily' : 'interval';
  setInputValue('test_count', s.test_count);
  setInputValue('test_pdf_scale_bit', s.pdf_scale_bit);
  if (document.getElementById('run_crawl_time')) {
    setInputValue('run_crawl_time', s.crawl_time || '');
    setInputValue('run_upload_time', s.upload_time || '');
    setInputValue('test_crawl_time', s.crawl_time || '');
    setInputValue('test_upload_time', s.upload_time || '');
    if (s.upload_interval_days !== undefined && s.upload_interval_days !== null) {
      setInputValue('run_upload_days', s.upload_interval_days);
      setInputValue('test_upload_days', s.upload_interval_days);
    }
    document.getElementById('run_schedule_info').innerHTML =
      timeTag('下一次爬取', fmtTime(s.next_daily_crawl_at), 'next')
      + timeTag('下一次上传', nextUploadLabel(s), 'next');
  }
  if (s.test_source) {
    document.getElementById('test_source').value = s.test_source;
  }
  applyIntervalDisplay('test_crawl_interval', 'test_crawl_unit', s.crawl_interval_minutes);
  applyIntervalDisplay('test_upload_interval', 'test_upload_unit', s.upload_interval_minutes);
  const schedEl = document.getElementById('test_schedule_info');
  if (schedEl) {
    schedEl.innerHTML =
      timeTag('下一次爬取', fmtTime(uiTestSched === 'daily' ? s.next_daily_crawl_at : s.next_crawl_at), 'next')
      + timeTag('下一次上传',
          uiTestSched === 'daily' ? nextUploadLabel(s) : fmtTime(s.next_upload_at), 'next');
  }
  if (s.output_dir) {
    setInputValue('top_output_dir', s.output_dir);
    document.getElementById('output_dir_display').textContent = s.output_dir;
  }
  if (s.config_info) renderConfigInfo(s.config_info);
  applyVisual();
  uiRunning = !!(
    s.crawl_running || s.upload_running || s.migration_running || s.index_building
  );
  uiPaused = !!s.paused;
  const toggleBtn = document.getElementById('top_btn_toggle');
  toggleBtn.textContent = (uiRunning && !uiPaused) ? '⏸' : '▶';
  toggleBtn.title = uiPaused ? '继续' : (uiRunning ? '暂停' : '开始');
  toggleBtn.classList.toggle('pause-mode', uiRunning && !uiPaused);
  // 状态文件里还有来源显示「运行中」时，停止按钮必须可用，
  // 否则会出现「进度卡片在爬、顶部停止却是灰的」。
  const busy = uiRunning || uiStateBusy;
  const indexReady = s.index_ready !== false;
  toggleBtn.disabled = false;
  document.getElementById('top_btn_stop').disabled = !busy;
  document.getElementById('top_btn_upload').disabled = busy || !indexReady;
  const migrateBtn = document.getElementById('agent_migrate_btn');
  if (migrateBtn) migrateBtn.disabled = busy;
  document.getElementById('top_btn_output_dir').disabled = uiRunning;
  document.getElementById('top_output_dir').disabled = uiRunning;
  document.getElementById('top_state').textContent =
    !busy ? '空闲' : (uiRunning ? (uiPaused ? '已暂停' : '运行中') : '运行中（其它进程）');
}

function currentSettings() {
  return {
    mode: uiMode,
    test_count: parseInt(document.getElementById('test_count').value, 10),
    test_source: document.getElementById('test_source').value,
    full_test: uiFullTest,
    test_schedule_mode: uiTestSched,
    upload_mode: uiUpload,
    parallel: uiParallel,
  };
}

async function saveSettings() {
  const r = await api('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(currentSettings()),
  });
  if (!r.ok) { setCtlStatus('保存失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('设置已保存', false);
}

function setMode(mode) {
  uiMode = mode;
  applyVisual();
  if (agentFullApiKey) {
    document.getElementById('agent_api_key').value = agentFullApiKey;
  }
  saveSettings();
}

function setUpload(mode) {
  uiUpload = mode;
  applyVisual();
  saveSettings();
}

function setParallel(value) {
  uiParallel = !!value;
  applyVisual();
  saveSettings();
}

function setFullTest(value) {
  uiFullTest = !!value;
  applyVisual();
  saveSettings();
}

function setTestSched(value) {
  uiTestSched = (value === 'daily') ? 'daily' : 'interval';
  applyVisual();
  saveSettings();
}

async function saveScheduleTimes(scope) {
  const prefix = (scope === 'test') ? 'test_' : 'run_';
  const body = {
    crawl_time: document.getElementById(prefix + 'crawl_time').value || '',
    upload_time: document.getElementById(prefix + 'upload_time').value || '',
  };
  const r = await api('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) { setCtlStatus('保存定时时间失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('定时时间已保存', false);
  if (r.data.settings) applySettings(r.data.settings);
}

async function saveUploadDays(el) {
  const raw = el && el.value !== undefined
    ? el.value
    : document.getElementById('run_upload_days').value;
  const days = parseInt(raw, 10);
  if (isNaN(days) || days < 0) {
    setCtlStatus('上传间隔必须是不小于 0 的整数', true);
    return;
  }
  const r = await api('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ upload_interval_days: days }),
  });
  if (!r.ok) { setCtlStatus('保存上传间隔失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus(
    days === 0
      ? '上传间隔已设为 0：每次爬取结束后立即上传全部未上传文件'
      : '上传间隔已设为每 ' + days + ' 天统一上传',
    false
  );
  if (r.data.settings) applySettings(r.data.settings);
}

async function savePdfScale() {
  const value = parseFloat(document.getElementById('test_pdf_scale_bit').value);
  if (isNaN(value)) { setCtlStatus('缩放必须是数字', true); return; }
  const r = await api('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ pdf_scale_bit: value }),
  });
  if (!r.ok) { setCtlStatus('保存缩放失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('校纪检 PDF 缩放已保存', false);
  if (r.data.settings) applySettings(r.data.settings);
}

function renderConfigInfo(info) {
  const el = document.getElementById('run_config_view');
  if (!el) return;
  el.innerHTML = Object.keys(info).map(key =>
    '<div class="cfg-row"><span class="cfg-key">' + esc(key) + '</span>'
    + '<span class="cfg-val">' + esc(String(info[key])) + '</span></div>'
  ).join('');
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
    + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

// 「下一次上传」：间隔设为 0 时是随爬取立即上传，没有固定时间点
function nextUploadLabel(s) {
  if (s && s.upload_strategy === 'interval' && s.upload_interval_days === 0) {
    return '随爬取立即上传';
  }
  return fmtTime(s ? s.next_daily_upload_at : null);
}

// 中纪检上次各区间爬到哪：正向边界 / 倒爬起点与游标 / 最大有效编号
function ccdiRangeInfo(iw) {
  if (!iw || !iw.last_id) return '';
  const tags = [];
  const fwd = iw.forward_last_valid || 0;
  const bmin = iw.backward_last_valid || 0;
  const bmax = iw.backward_max_valid || 0;
  if (fwd) tags.push('<span class="tag tag-range-fwd">正爬最大有效 ' + fwd + '</span>');
  if (bmin) tags.push('<span class="tag tag-range-back">倒爬最小有效 ' + bmin + '</span>');
  if (bmax) tags.push('<span class="tag tag-range-back">倒爬最大有效 ' + bmax + '</span>');
  const maxValid = iw.last_valid_id || 0;
  if (maxValid) {
    tags.push('<span class="tag tag-range-max">最大有效编号 ' + maxValid + '</span>');
  }
  if (!tags.length) return '';
  return '<div class="range-tags"><span class="tag tag-range-label">上次区间</span>'
    + tags.join('') + '</div>';
}

let uiFailedOpen = false;
let lastSettings = {};

function toggleFailedIds() {
  uiFailedOpen = !uiFailedOpen;
  const el = document.getElementById('failed_card');
  if (el) el.innerHTML = failedCardHtml(lastSettings);
}

// 右下列：失败编号总数 + 可展开的具体编号 + 立即重试
function failedCardHtml(settings) {
  const ids = settings.failed_ids || [];
  let html = '<div class="side-title">失败编号</div>';
  if (!ids.length) {
    return html + '<div class="pc-msg">当前没有失败编号</div>';
  }
  html += '<div class="pc-msg">共 <b>' + ids.length + '</b> 个待重试</div>'
    + '<div class="range-tags">'
    + '<span class="tag tag-failed">' + ids.length + ' 个</span>'
    + (settings.next_failed_retry_at
        ? '<span class="tag tag-time next">下次 ' + esc(fmtTime(settings.next_failed_retry_at)) + '</span>'
        : '<span class="tag tag-time">第 ' + (settings.failed_retry_round || 0) + '/3 轮</span>')
    + '</div>';
  html += '<div class="failed-actions">'
    + '<button class="mini-btn" onclick="toggleFailedIds()">'
    + (uiFailedOpen ? '收起编号' : '展开编号') + '</button>'
    + '<button class="mini-btn" onclick="retryFailed()"'
    + (settings.crawl_running ? ' disabled' : '') + '>立即重试</button>'
    + '</div>';
  if (uiFailedOpen) {
    html += '<div class="failed-ids">'
      + ids.map(i => '<span class="tag tag-failed">' + i + '</span>').join('')
      + '</div>';
  }
  return html;
}

// 时间标签：next 用于“下一次”，默认用于“上一次”
function timeTag(label, value, variant) {
  return '<span class="tag tag-time' + (variant ? ' ' + variant : '') + '">'
    + esc(label) + '：' + esc(value || '—') + '</span>';
}

// 分钟数回填到「数值 + 单位」控件（能被 60 整除时用小时显示）
function applyIntervalDisplay(inputId, unitId, minutes) {
  const input = document.getElementById(inputId);
  const unit = document.getElementById(unitId);
  if (!input || !unit) return;
  if (document.activeElement === input) return;
  const m = parseInt(minutes, 10) || 0;
  if (m > 0 && m % 60 === 0) {
    input.value = m / 60;
    unit.value = '60';
  } else {
    input.value = m;
    unit.value = '1';
  }
}

function intervalMinutes(inputId, unitId) {
  const value = parseInt(document.getElementById(inputId).value, 10);
  const unit = parseInt(document.getElementById(unitId).value, 10) || 1;
  if (isNaN(value) || value < 0) return 0;
  return value * unit;
}

async function saveTestIntervals() {
  const body = {
    crawl_interval_minutes: intervalMinutes('test_crawl_interval', 'test_crawl_unit'),
    upload_interval_minutes: intervalMinutes('test_upload_interval', 'test_upload_unit'),
  };
  const r = await api('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) { setCtlStatus('保存间隔失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('间隔设置已保存', false);
  if (r.data.settings) applySettings(r.data.settings);
}

async function setOutputDir(prefix) {
  const val = document.getElementById(prefix + '_output_dir').value.trim();
  const r = await api('/api/output_dir', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ output_dir: val }),
  });
  if (!r.ok) { setCtlStatus('修改保存位置失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('保存位置已更新', false);
  if (r.data.settings) applySettings(r.data.settings);
}

async function startCrawl() {
  let source = 'all';
  if (uiMode === 'test') {
    source = document.getElementById('test_source').value;
  }
  const r = await api('/api/crawl', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ source }),
  });
  if (!r.ok) { setCtlStatus('启动爬取失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('爬取任务已启动', false);
}

async function startUpload() {
  const r = await api('/api/upload', { method: 'POST' });
  if (!r.ok) { setCtlStatus('启动上传失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('上传任务已启动', false);
}

async function toggleRun() {
  if (uiRunning && uiPaused) {
    const r = await api('/api/resume', { method: 'POST' });
    if (!r.ok) { setCtlStatus('继续失败：' + (r.data.error || r.status), true); return; }
    setCtlStatus(r.data.message || '任务已继续', false);
    return;
  }
  if (uiRunning) {
    const r = await api('/api/pause', { method: 'POST' });
    if (!r.ok) { setCtlStatus('暂停失败：' + (r.data.error || r.status), true); return; }
    setCtlStatus(r.data.message || '任务已暂停', false);
    return;
  }
  await startCrawl();
}

async function stopTop() {
  const r = await api('/api/stop', { method: 'POST' });
  if (!r.ok) { setCtlStatus('终止失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus(r.data.message || '已终止所有任务', false);
}

async function retryFailed() {
  const r = await api('/api/retry_failed', { method: 'POST' });
  if (!r.ok) {
    setCtlStatus('重试失败编号失败：' + (r.data.error || r.status), true);
    return;
  }
  setCtlStatus('已开始重试 ' + (r.data.count || '') + ' 个失败编号', false);
}

async function stopSource() {
  await stopTop();
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function tagNumbers(text) {
  const value = String(text ?? '');
  const pattern = /\\d+(?:\\s*\\/\\s*\\d+)?/g;
  let html = '';
  let last = 0;
  let match;
  while ((match = pattern.exec(value)) !== null) {
    html += esc(value.slice(last, match.index));
    html += '<span class="tag inline-num">' + esc(match[0]) + '</span>';
    last = match.index + match[0].length;
  }
  return html + esc(value.slice(last));
}

function rememberPageDetail(details) {
  const key = details.dataset.detailKey || '';
  if (!key) return;
  if (details.open) expandedPageDetails.add(key);
  else expandedPageDetails.delete(key);
}

function pageIdChips(ids, cls, unmatchedIds) {
  const values = (ids || []).map(String);
  if (!values.length) return '<span class="muted">—</span>';
  return values.map(id => {
    const missing = unmatchedIds && unmatchedIds.has(id);
    return '<span class="id-chip ' + cls + (missing ? ' unmatched' : '') + '">'
      + esc(id) + '</span>';
  }).join('');
}

function renderAgentStatus(data) {
  agentFullApiKey = data.api_key || '';
  agentMaskedApiKey = data.api_key_masked || '';
  setInputValue('agent_base_url', data.base_url || '');
  // 不论运行模式还是测试模式，API Key 都完整显示，方便直接核对/复制
  setInputValue('agent_api_key', agentFullApiKey);
  setInputValue('agent_name_ccdi', data.dataset_name_ccdi || '');
  setInputValue('agent_name_bit', data.dataset_name_bit || '');

  const errEl = document.getElementById('agent_error');
  const dsEl = document.getElementById('agent_datasets');
  renderAgentTaskCard(data.upload_state || {}, data.parse_settings || {});
  if (!data.ok) {
    errEl.textContent = '连接失败：' + (data.error || '未知错误');
    errEl.style.display = 'block';
    renderAgentRetryBoxes(data.upload_state || {});
    return;
  }
  errEl.style.display = 'none';
  dsEl.innerHTML = (data.datasets || []).map(ds => {
    const source = ds.name === '中纪检' ? 'ccdi' : (ds.name === '校纪检' ? 'bit' : '');
    if (!ds.found) {
      return '<div class="card agent-dataset-card"><div class="lib-title">' + esc(ds.name) + '</div>'
        + '<div class="muted">未找到对应知识库</div>'
        + (source ? '<div id="agent_retry_' + source + '"></div>' : '')
        + '</div>';
    }
    const extra = (ds.counted !== undefined && ds.total !== ds.counted)
      ? ' <span class="muted">（状态统计 ' + ds.counted + '）</span>' : '';
    return '<div class="card agent-dataset-card">'
      + '<div class="lib-title">' + esc(ds.name) + '</div>'
      + '<div class="stat">' + ds.total + '</div>'
      + '<div class="muted">文件总数' + extra + '</div>'
      + '<div class="lib-id">库ID：' + esc(ds.dataset_id) + '</div>'
      + '<div class="stat-row">'
      + '<span class="pill done">已完成 ' + ds.done + '</span>'
      + '<span class="pill running">解析中 ' + ds.running + '</span>'
      + '<span class="pill pending">上传中 ' + ds.pending + '</span>'
      + '<span class="pill failed">失败 ' + ds.failed + '</span>'
      + '</div>'
      + (source ? '<div id="agent_retry_' + source + '"></div>' : '')
      + '</div>';
  }).join('') || '<div class="muted">暂无数据</div>';
  renderAgentRetryBoxes(data.upload_state || {});
}

function toggleYearRow(row) {
  const year = row.getAttribute('data-year');
  const detail = row.nextElementSibling;
  if (!detail || !detail.classList.contains('year-page-detail')) return;
  const open = detail.style.display === 'none';
  detail.style.display = open ? 'table-row' : 'none';
  if (open) expandedYears.add(year); else expandedYears.delete(year);
  const arrow = row.querySelector('.year-arrow');
  if (arrow) arrow.textContent = open ? '▾' : '▸';
}

function yearProgressPanel(state) {
  const rows = (state.year_rows || []).slice().sort((a, b) =>
    (parseInt(b.year, 10) || 0) - (parseInt(a.year, 10) || 0)
  );
  if (!rows.length) return '';
  const statusText = {
    pending: '待扫描',
    running: '补录中',
    done: '完成',
    partial: '部分完成',
    error: '异常',
    interrupted: '已中断',
    skipped_no_missing: '无需补录'
  };
  const doneCount = rows.filter(row => row.status === 'done' || row.status === 'skipped_no_missing').length;
  const currentYear = String(state.year || '');
  const webYearsById = new Map();
  const webYearOccurrences = new Map();
  const unmatchedWebIds = new Set();
  rows.forEach(row => {
    const year = String(row.year || '');
    (row.page_results || []).forEach(page => {
      (page.web_ids || []).forEach(id => {
        const key = String(id);
        if (!webYearsById.has(key)) webYearsById.set(key, new Set());
        webYearsById.get(key).add(year);
        const occurrenceKey = year + ':' + key;
        webYearOccurrences.set(
          occurrenceKey,
          (webYearOccurrences.get(occurrenceKey) || 0) + 1
        );
      });
      (page.unmatched_ids || []).forEach(id => unmatchedWebIds.add(String(id)));
    });
    (row.unmatched_web_ids || []).forEach(id => unmatchedWebIds.add(String(id)));
  });
  const repeatedWebIds = [...webYearsById.entries()]
    .filter(([, years]) => years.size > 1)
    .map(([id]) => id);
  const sameYearRepeatedIds = [...webYearOccurrences.entries()]
    .filter(([, count]) => count > 1)
    .map(([key]) => key.split(':').slice(1).join(':'));
  let html = '<details class="year-panel"'
    + (uiYearPanelOpen ? ' open' : '')
    + ' ontoggle="uiYearPanelOpen=this.open">'
    + '<summary><span>中纪检 · 年份日期补全进度</span>'
    + '<span class="muted">完成 ' + doneCount + '/' + rows.length + '</span>'
    + (currentYear ? '<span class="tag running">当前 ' + esc(currentYear) + ' 年</span>' : '')
    + '<span class="tag ' + (repeatedWebIds.length ? 'tag-failed' : 'tag-yes') + '">'
    + '跨年重复' + repeatedWebIds.length + '项</span>'
    + '<span class="tag ' + (sameYearRepeatedIds.length ? 'tag-failed' : 'tag-yes') + '">'
    + '同年重复' + sameYearRepeatedIds.length + '项</span>'
    + '<span class="tag ' + (unmatchedWebIds.size ? 'tag-failed' : 'tag-yes') + '">'
    + '未匹配' + unmatchedWebIds.size + '项</span>'
    + '</summary>'
    + '<div class="year-table-wrap"><table class="year-table"><colgroup>'
    + '<col class="year-column-col"><col style="width:130px"><col class="page-position-col">'
    + '<col style="width:92px"><col></colgroup><thead><tr>'
    + '<th class="year-column">年份</th><th>已扫描 / 共</th>'
    + '<th class="page-position">当前页 / 总页数</th><th>状态</th>'
    + '<th>说明</th></tr></thead><tbody>';
  rows.forEach(row => {
    const total = Number(row.year_total_count || 0);
    const scanned = Number(row.year_scanned || 0);
    const pct = total > 0 ? Math.min(100, Math.round(100 * scanned / total)) : 0;
    const status = row.status || 'pending';
    const safeStatus = ['pending','running','done','partial','interrupted'].includes(status)
      ? status : (status === 'skipped_no_missing' ? 'done' : 'skipped');
    const pages = (row.pages || []).map(p =>
      '第' + p.page + '页 ' + (p.crawled || 0) + '/' + (p.expected || 0)
    ).join('、');
    const reason = pages || row.message || '';
    const pageResults = row.page_results || (row.pages || []).map(p => ({
      page: p.page,
      crawled: p.crawled,
      expected: p.expected,
      complete: false
    }));
    const pageTableRows = pageResults.length
      ? pageResults.map(page => {
          const webIds = page.web_ids || [];
          const localIds = page.matched_local_ids || [];
          const missingIds = page.unmatched_ids || [];
          const detailKey = String(row.year) + ':' + String(page.page);
          const missingSet = new Set(missingIds.map(String));
          return '<tr><td class="page-index">第 <span class="tag inline-num">'
            + esc(page.page) + '</span> 页</td>'
            + '<td class="page-counts">' + esc(page.crawled || 0)
            + ' / ' + esc(page.expected || 0) + '</td>'
            + '<td class="page-status"><span class="tag ' + (page.complete ? 'tag-yes' : 'tag-failed') + '">'
            + (page.complete ? '完成' : '未完成') + '</span></td>'
            + '<td class="page-ids"><details class="page-id-details"'
            + ' data-detail-key="' + esc(detailKey) + '"'
            + (expandedPageDetails.has(detailKey) ? ' open' : '')
            + ' ontoggle="rememberPageDetail(this)">'
            + '<summary title="展开或收起编号明细">'
            + '<span>网页 <span class="tag tag-src-ccdi">' + webIds.length + '</span></span>'
            + '<span>匹配 <span class="tag tag-yes">' + localIds.length + '</span></span>'
            + '<span>未匹配 <span class="tag ' + (missingIds.length ? 'tag-failed' : 'tag-yes') + '">'
            + missingIds.length + '</span></span></summary>'
            + '<div class="page-id-box web"><span class="box-label">网页编号</span>'
            + pageIdChips(webIds, 'web', missingSet) + '</div>'
            + '<div class="page-id-box local"><span class="box-label">本地对应编号</span>'
            + pageIdChips(localIds.map(id => 'ccdi:' + id), 'local', null) + '</div>'
            + '</details></td></tr>';
        }).join('')
      : '<tr><td colspan="4" class="muted">尚未读取页面明细</td></tr>';
    const unmatchedWeb = (row.unmatched_web_ids || []);
    const yearWebIds = new Set(pageResults.flatMap(p => (p.web_ids || []).map(String)));
    const yearLocalIds = new Set(pageResults.flatMap(p => (p.matched_local_ids || []).map(String)));
    const yearUnmatchedIds = new Set([
      ...pageResults.flatMap(p => (p.unmatched_ids || []).map(String)),
      ...unmatchedWeb.map(String)
    ]);
    const webIdTotal = yearWebIds.size;
    const localIdTotal = yearLocalIds.size;
    const unmatchedIdTotal = yearUnmatchedIds.size;
    const matchSummary = '<div class="bf-tags" style="margin-top:7px">'
      + '<span class="tag">网页编号 ' + webIdTotal + '</span>'
      + '<span class="tag tag-yes">本地匹配 ' + localIdTotal + '</span>'
      + '<span class="tag ' + (unmatchedIdTotal ? 'tag-failed' : 'tag-yes') + '">未匹配 '
      + unmatchedIdTotal + '</span>'
      + '<span class="tag ' + (unmatchedIdTotal ? 'tag-failed' : 'tag-yes') + '">'
      + (unmatchedIdTotal ? '未全部匹配' : '全部匹配') + '</span></div>';
    const unmatchedHtml = unmatchedWeb.length
      ? '<div class="bf-tags" style="margin-top:7px"><span class="tag tag-failed">未匹配网页编号 '
        + unmatchedWeb.length + '</span>'
        + unmatchedWeb.map(id => '<span class="tag tag-failed">' + esc(id) + '</span>').join('')
        + '</div>'
      : '';
    const descriptionTags = '<div class="bf-tags">'
      + '<span class="tag ' + (unmatchedIdTotal ? 'tag-failed' : 'tag-yes') + '">'
      + '未匹配' + unmatchedIdTotal + '项</span></div>';
    const descriptionHtml = reason
      ? '<div class="muted" style="margin-top:5px">' + tagNumbers(reason) + '</div>'
      : '';
    html += '<tr class="year-main-row ' + (String(row.year) === currentYear ? 'current' : '')
      + '" data-year="' + esc(row.year) + '" onclick="toggleYearRow(this)">'
      + '<td class="year-column"><span class="year-arrow">'
      + (expandedYears.has(String(row.year)) ? '▾' : '▸')
      + '</span> <b>' + esc(row.year) + '</b></td>'
      + '<td class="num">' + scanned + ' / ' + (total || '—')
      + '<div class="year-progress-mini"><span style="width:' + pct + '%"></span></div></td>'
      + '<td class="num page-position">' + (row.page || '—') + ' / ' + (row.page_count || '—') + '</td>'
      + '<td><span class="tag tag-cat-' + safeStatus + '">'
      + esc(statusText[status] || status) + '</span></td>'
      + '<td class="year-description">' + descriptionTags + descriptionHtml + '</td>'
      + '</tr>'
      + '<tr class="year-page-detail" data-year-detail="' + esc(row.year) + '" style="display:'
      + (expandedYears.has(String(row.year)) ? 'table-row' : 'none') + '">'
      + '<td colspan="5"><table class="year-page-table"><colgroup>'
      + '<col style="width:140px"><col style="width:112px"><col style="width:96px"><col>'
      + '</colgroup><thead><tr>'
      + '<th class="page-index">页码</th><th class="page-counts">爬取数量 / 本页数量</th>'
      + '<th class="page-status">是否完成</th><th class="page-ids">编号明细</th>'
      + '</tr></thead><tbody>'
      + pageTableRows + '</tbody></table>'
      + matchSummary
      + unmatchedHtml + '</td></tr>';
  });
  return html + '</tbody></table></div></details>';
}

function renderAgentTaskCard(uploadState, parseSettings) {
  const el = document.getElementById('agent_task_card');
  if (!el) return;
  const prog = uploadState.progress || {};
  const queue = uploadState.parse_queue || {};
  const migration = uploadState.migration || {};
  const indexStatus = uploadState.version_index || {};
  const indexSources = indexStatus.sources || {};
  const backfillStatus = uploadState.backfill_status || {};
  const dateProgress = uploadState.date_progress || {};
  const maintenanceFlags = uploadState.maintenance_flags || {};
  const retry = queue.retry || [];
  const active = queue.active || [];
  const combinedDateTotal = Number((dateProgress.ccdi || {}).total || 0)
    + Number((dateProgress.bit || {}).total || 0);
  const combinedDateDone = Number((dateProgress.ccdi || {}).dated || 0)
    + Number((dateProgress.bit || {}).dated || 0);
  const total = combinedDateTotal || prog.total || 0;
  const current = combinedDateTotal ? combinedDateDone : (prog.current || 0);
  const pct = total > 0 ? Math.min(100, Math.round(100 * current / total)) : 0;
  let html = '<div class="lib-title">上传与解析</div>'
    + '<div class="stat">' + current + (total ? ' / ' + total : '') + '</div>'
    + '<div class="muted">' + esc(prog.message || '暂无上传任务') + '</div>'
    + '<div class="bar"><span style="width:' + pct + '%"></span></div>'
    + '<div class="stat-row">'
    + '<span class="pill running">解析中 ' + active.length + '</span>'
    + '<span class="pill failed">待重试 ' + retry.length + '</span>'
    + (queue.wave ? '<span class="pill pending">波次 ' + queue.wave + '</span>' : '')
    + '</div>'
    + '<div class="muted" style="margin-top:7px">并发上限 '
    + esc(parseSettings.max_parsing_documents || 500)
    + ' · 超时 ' + esc(parseSettings.parse_timeout_minutes || 30) + ' 分钟</div>';
  const dateCard = (source, title) => {
    const state = backfillStatus[source] || {};
    const sourceIndex = indexSources[source] || {};
    const stats = dateProgress[source] || {};
    const totalDocs = stats.total || state.total || 0;
    // 日期数量以 documents 中真实写入日期的数量为准；扫描页数只用于年份表。
    const currentDocs = Number(stats.dated || 0);
    const percent = totalDocs > 0 ? Math.min(100, Math.round(100 * currentDocs / totalDocs)) : 0;
    const status = state.status || (stats.missing ? 'pending' : 'done');
    const statusLabel = {pending:'待补录', running:'补录中', done:'已完成', error:'补录异常'}[status] || status;
    const cls = status === 'done' ? 'done' : (status === 'error' ? 'failed' : (status === 'running' ? 'running' : 'pending'));
    const sourceIndexReady = !!sourceIndex.ready;
    const sourceFresh = !!maintenanceFlags[source];
    const matchAudit = state.match_audit || {};
    const sourceError = sourceIndex.error || (status === 'error' ? state.message : '');
    const throttleWarning = state.throttle_message
      ? '<div class="bf-error">限流警告：' + esc(state.throttle_message) + '</div>'
      : '';
    const unmatchedWarning = Number(state.unmatched_local_count || 0) > 0
      ? '<div class="bf-error">本地编号未在网页检索结果中匹配：'
        + esc(state.unmatched_local_count) + ' 条'
        + (Array.isArray(state.unmatched_local_ids) && state.unmatched_local_ids.length
          ? '<details class="page-id-details"><summary>展开本地编号</summary>'
            + '<div class="bf-msg">' + esc(state.unmatched_local_ids.join('、')) + '</div>'
            + '</details>'
          : '')
        + '</div>'
      : '';
    const matchAuditTags = source === 'ccdi' && matchAudit.unique_web_count !== undefined
      ? '<div class="bf-tags">'
        + '<span class="tag">网页去重 ' + esc(matchAudit.unique_web_count) + '</span>'
        + '<span class="tag ' + (Number(matchAudit.repeated_count || 0) ? 'tag-failed' : 'tag-yes') + '">'
        + '跨年重复 ' + esc(matchAudit.repeated_count || 0) + '</span>'
        + '<span class="tag ' + (Number(matchAudit.same_year_repeated_count || 0) ? 'tag-failed' : 'tag-yes') + '">'
        + '同年重复 ' + esc(matchAudit.same_year_repeated_count || 0) + '</span>'
        + '<span class="tag ' + (Number(matchAudit.unmatched_count || 0) ? 'tag-failed' : 'tag-yes') + '">'
        + '未匹配 ' + esc(matchAudit.unmatched_count || 0) + '</span>'
        + '<span class="tag ' + (Number(matchAudit.matched_missing_date_count || 0) ? 'tag-failed' : 'tag-yes') + '">'
        + '匹配但缺日期 ' + esc(matchAudit.matched_missing_date_count || 0) + '</span>'
        + '<span class="tag ' + (Number(matchAudit.matched_wrong_year_count || 0) ? 'tag-failed' : 'tag-yes') + '">'
        + '年份不符 ' + esc(matchAudit.matched_wrong_year_count || 0) + '</span></div>'
      : '';
    const tags = [];
    if (source !== 'ccdi') {
      if (state.page && state.page_count) {
        tags.push('目录第 ' + esc(state.page) + '/' + esc(state.page_count) + ' 页');
      }
      if (state.total_declared) tags.push('目录共 ' + esc(state.total_declared) + ' 条');
    }
    if (state.updated_at && source !== 'ccdi') tags.push('更新 ' + esc(fmtTime(state.updated_at)));
    if (Number(stats.filename_dated || 0) > 0) {
      tags.push('文件名补日期 ' + esc(stats.filename_dated));
    }
    const indexTags = [];
    if (sourceIndex.ready) indexTags.push('索引已就绪');
    if (sourceIndex.versions !== undefined) {
      indexTags.push('版本族 ' + esc(sourceIndex.versions));
    }
    if (sourceIndex.built_at) indexTags.push('建立于 ' + esc(fmtTime(sourceIndex.built_at)));
    if (sourceIndex.missing_dates) {
      indexTags.push('缺日期 ' + esc(sourceIndex.missing_dates));
    }
    return '<div class="backfill-mini">'
      + '<div class="bf-head"><span class="bf-title">' + esc(title) + '</span>'
      + '<span class="pill ' + cls + '">' + esc(statusLabel) + '</span></div>'
      + '<div class="bf-section">'
      + '<div class="bf-section-title"><span class="tag tag-cat">日期补全</span></div>'
      + '<div class="bf-num-line"><span class="bf-num">' + currentDocs + ' / ' + totalDocs + '</span>'
      + '<span class="muted">已补 / 总数</span></div>'
      + '<div class="bar"><span style="width:' + percent + '%"></span></div>'
      + (tags.length ? '<div class="bf-tags">' + tags.map(x => '<span class="tag">' + x + '</span>').join('') + '</div>' : '')
      + '<div class="bf-msg">' + esc(state.message || ((stats.missing || 0) + ' 条缺日期'))
      + (state.updated_at && source === 'ccdi'
          ? ' · 更新 ' + esc(fmtTime(state.updated_at)) : '') + '</div>'
      + throttleWarning
      + unmatchedWarning
      + matchAuditTags
      + '</div>'
      + '<div class="bf-divider"></div>'
      + '<div class="bf-section">'
      + '<div class="bf-section-title"><span class="tag tag-retry-pending">索引建立</span>'
      + '<span class="pill ' + (sourceIndexReady ? 'done' : 'pending') + '">'
      + (sourceIndexReady ? '已建立' : '待建立') + '</span>'
      + '<span class="tag ' + (sourceFresh ? 'tag-yes' : 'tag-no') + '">时效 '
      + (sourceFresh ? 'true' : 'false') + '</span></div>'
      + '<div class="bar"><span style="width:' + (sourceIndexReady ? 100 : 0) + '%"></span></div>'
      + '<div class="bf-tags">' + indexTags.map(x => '<span class="tag">' + x + '</span>').join('') + '</div>'
      + '<div class="bf-msg">' + esc(sourceIndex.reason || (sourceIndexReady ? '索引可用于上传' : '日期补齐后自动建立')) + '</div>'
      + '</div>'
      + (sourceError ? '<div class="bf-error">报错：' + esc(sourceError) + '</div>' : '')
      + '</div>';
  };
  html += '<div class="backfill-grid">'
    + dateCard('ccdi', '中纪检')
    + dateCard('bit', '校纪检')
    + '</div>';
  html += yearProgressPanel(backfillStatus.ccdi || {});
  if (migration.status) {
    const label = {running:'迁库中', uploading:'重建完成 · 正在重传', done:'迁库完成', error:'迁库失败', interrupted:'迁库中断'}[migration.status] || migration.status;
    const cls = migration.status === 'done' ? 'done' : (migration.status === 'error' ? 'failed' : 'pending');
    html += '<div class="stat-row"><span class="pill ' + cls + '">' + esc(label) + '</span></div>';
    if (migration.error) html += '<div class="muted" style="color:#b91c1c">' + esc(migration.error) + '</div>';
  }
  if (el.dataset.taskSig === html) {
    renderAgentRetryBoxes(uploadState);
    return;
  }
  el.dataset.taskSig = html;
  const oldYearTable = el.querySelector('.year-table-wrap');
  const oldYearScrollTop = oldYearTable ? oldYearTable.scrollTop : 0;
  const oldWindowScrollY = window.scrollY;
  el.innerHTML = html;
  renderAgentRetryBoxes(uploadState);
  requestAnimationFrame(() => {
    const newYearTable = el.querySelector('.year-table-wrap');
    if (newYearTable && oldYearScrollTop > 0) {
      newYearTable.scrollTop = oldYearScrollTop;
    }
    if (Math.abs(window.scrollY - oldWindowScrollY) > 2) {
      window.scrollTo(0, oldWindowScrollY);
    }
  });
}

function renderAgentRetryBoxes(uploadState) {
  const rows = (uploadState.parse_queue || {}).retry || [];
  for (const source of ['ccdi', 'bit']) {
    const el = document.getElementById('agent_retry_' + source);
    if (!el) continue;
    const items = rows.filter(x => x.source === source);
    const signature = items.map(x => String(x.document_id || '')).join('|');
    if (el.dataset.sig === signature) continue;
    const oldDetails = el.querySelector('details');
    const wasOpen = oldDetails ? oldDetails.open : false;
    el.dataset.sig = signature;
    if (!items.length) {
      el.innerHTML = '';
      continue;
    }
    el.innerHTML = '<details class="retry-box"' + (wasOpen ? ' open' : '') + '>'
      + '<summary>解析超时/失败待重试 ' + items.length + ' 个</summary>'
      + '<div class="task-id-list">'
      + items.map(x => '<span class="tag tag-failed" title="'
          + esc(x.file_name || x.reason || '') + '">'
          + esc(x.document_id || '') + '</span>').join('')
      + '</div></details>';
  }
}

async function refreshAgentStatus() {
  const errEl = document.getElementById('agent_error');
  const msgEl = document.getElementById('agent_msg');
  errEl.style.display = 'none';
  if (msgEl) {
    msgEl.style.display = 'block';
    msgEl.style.color = '#888';
    msgEl.textContent = '刷新中…';
  }
  try {
    const res = await fetch('/api/agent_status');
    const data = await res.json();
    renderAgentStatus(data);
    if (msgEl) {
      if (data.ok) {
        msgEl.style.color = '#16a34a';
        msgEl.textContent = '刷新成功 · ' + new Date().toLocaleTimeString();
      } else {
        msgEl.style.display = 'none';
      }
    }
  } catch (e) {
    errEl.textContent = '请求失败：' + String(e);
    errEl.style.display = 'block';
    if (msgEl) msgEl.style.display = 'none';
  }
}

async function saveAgentConfig() {
  const apiVal = document.getElementById('agent_api_key').value.trim();
  const apiToSave = (uiMode !== 'test' && apiVal === agentMaskedApiKey) ? agentFullApiKey : apiVal;
  const body = {
    base_url: document.getElementById('agent_base_url').value.trim(),
    api_key: apiToSave,
    dataset_name_ccdi: document.getElementById('agent_name_ccdi').value.trim(),
    dataset_name_bit: document.getElementById('agent_name_bit').value.trim(),
  };
  const r = await api('/api/agent_config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) { setCtlStatus('保存学科智能体配置失败：' + (r.data.error || r.status), true); return; }
  setCtlStatus('学科智能体配置已保存', false);
  await refreshAgentStatus();
}

function openMigrationDialog() {
  if (uiRunning || uiStateBusy) {
    setCtlStatus('当前有任务运行，完成或终止后才能迁库', true);
    return;
  }
  document.getElementById('migration_dialog').classList.add('open');
}

function closeMigrationDialog() {
  document.getElementById('migration_dialog').classList.remove('open');
}

async function confirmMigration() {
  const btn = document.getElementById('migration_confirm_btn');
  btn.disabled = true;
  btn.textContent = '正在启动…';
  try {
    const r = await api('/api/migrate', { method: 'POST' });
    if (!r.ok) {
      setCtlStatus('启动迁库失败：' + (r.data.error || r.status), true);
      return;
    }
    closeMigrationDialog();
    setCtlStatus(r.data.message || '迁库任务已启动', false);
  } finally {
    btn.disabled = false;
    btn.textContent = '再次确认并迁库';
  }
}

// 网址过长时只显示末尾，避免撑破卡片
function shortUrl(u) {
  const text = String(u || '');
  return text.length > 52 ? '…' + text.slice(-52) : text;
}

// 状态码小标签：2xx 绿、3xx 蓝、4xx/5xx 红
function httpTag(code) {
  const c = parseInt(code, 10) || 0;
  if (!c) return '';
  const cls = (c >= 200 && c < 300) ? 'ok' : ((c >= 300 && c < 400) ? 'redirect' : (c >= 400 ? 'bad' : ''));
  return '<span class="tag tag-http ' + cls + '">' + c + '</span> ';
}

function progressCard(title, p, lastCrawl, lastUpload, extra) {
  const variant = title === '中纪检' ? ' progress-ccdi' : (title === '校纪检' ? ' progress-bit' : '');
  const running = p.status === 'running';
  const total = p.total || 0;
  const hasTotal = total > 0;
  const num = hasTotal
    ? (p.current + ' / ' + total + ' 条')
    : ('已处理 ' + p.current + ' 条');
  const bar = hasTotal
    ? '<div class="bar"><span style="width:' + p.percent + '%"></span></div>'
    : (running
        ? '<div class="bar indeterminate"><span></span></div>'
        : '<div class="bar"></div>');
  const stopBtn = running
    ? '<button class="icon-btn danger pc-stop" onclick="stopSource()" title="终止本任务">■</button>'
    : '';
  const note = p.note ? '<div class="note">' + esc(p.note) + '</div>' : '';
  // 本轮活动量：即使序号暂时不动（比如在扫描已爬过的分类），也能看出程序在推进
  const scanned = p.scanned || 0;
  const loaded = p.loaded || 0;
  const activity = scanned > 0
    ? '<div class="pc-msg">本轮已扫描 ' + scanned + ' 条 · 打开详情页 ' + loaded
      + ' 个 · 新增 ' + (p.new_count || 0) + ' 个</div>'
    : '';
  // 当前正在访问的网址 + HTTP 状态码，并附上「上一个页面」（被拦时能看到是从哪一页跳过去的）
  let visit = '';
  if (p.last_url) {
    visit = '<div class="pc-msg url-line" title="' + esc(p.last_url) + '">'
      + ((p.pending && running) ? '<span class="tag running">请求中</span> ' : httpTag(p.last_status))
      + esc(shortUrl(p.last_url)) + '</div>';
    if (p.prev_url) {
      visit += '<div class="pc-msg url-line prev" title="' + esc(p.prev_url) + '">'
        + '上一个页面 ' + httpTag(p.prev_status) + esc(shortUrl(p.prev_url)) + '</div>';
    }
  }
  return '<div class="card progress-card' + variant + (p.status === 'error' ? ' is-error' : '') + '">'
    + stopBtn
    + '<div class="pc-title">' + esc(title) + '</div>'
    + '<span class="tag ' + p.status + '">'
    + ({running: '运行中', idle: '空闲', error: '异常中断'}[p.status] || p.status) + '</span>'
    + '<div class="pc-num">' + num + '</div>'
    + bar
    + (/^(完成，本次新增 0 个（目录总数仍为|无更新（|爬取任务空闲；日期补录进度见学科智能体卡片)/.test(p.message || '')
        ? '<div class="pc-msg"><span class="tag tag-noupdate">无更新</span></div>'
        : '<div class="pc-msg">' + esc(p.message || '') + '</div>')
    + activity
    + visit
    + note
    + (extra || '')
    + '<div class="stat-row" style="margin-top:10px">'
    + timeTag('上次爬取', lastCrawl)
    + timeTag('上次上传', lastUpload)
    + '</div>'
    + '</div>';
}

// 中纪检子分类进度：整行可折叠表格（共 x 条 / 已爬 y 条、状态、跳过原因与重试时间）
function categoryPanel(s) {
  const rows = s.categories_ccdi || [];
  if (!rows.length) return '';
  const statusText = { pending: '待爬', running: '爬取中', done: '完成', skipped: '跳过', interrupted: '已中断', partial: '部分完成' };
  const reasons = {
    error_page: '站点限流',
    empty: '未取到列表',
    nav_error: '页面打开失败',
    page_nav_error: '翻页中断',
    verification: '人机验证',
    verification_timeout: '验证超时',
  };
  const done = rows.filter(r => r.status === 'done').length;
  const skipped = rows.filter(r => r.status === 'skipped' || r.status === 'interrupted').length;
  const current = s.current_category || '';
  const retryAt = s.skipped_retry_at ? fmtTime(s.skipped_retry_at) : '';
  let html = '<div class="card"><details class="cat-panel" open>'
    + '<summary><span class="cat-summary-title">中纪检 · 子分类进度</span>'
    + '<span class="cat-summary-meta">'
    + (current ? '当前：' + esc(current) + ' · ' : '')
    + '完成 ' + done + '/' + rows.length
    + (skipped ? ' · 跳过 ' + skipped : '')
    + '</span></summary>'
    + '<div class="cat-scroll"><table class="cat-table"><thead><tr>'
    + '<th style="width:30%">子分类</th><th style="width:22%">已爬 / 共</th>'
    + '<th style="width:16%">状态</th><th>重试安排</th></tr></thead><tbody>';
  rows.forEach(r => {
    const st = statusText[r.status] ? r.status : 'pending';
    const total = (r.total == null || r.total === '') ? '—' : (r.total_estimated ? '~' + r.total : r.total);
    const reason = reasons[r.reason] || r.reason || '';
    const statusLabel = (st === 'skipped' && reason) ? '跳过·' + reason : statusText[st];
    let retry = '<span class="muted">—</span>';
    if (st === 'skipped' || st === 'interrupted') {
      if (retryAt) {
        retry = '<span class="tag tag-retry">已安排重试 · ' + esc(retryAt) + '</span>';
      } else if (s.progress.ccdi && s.progress.ccdi.status === 'running') {
        // 定时重试要等本轮爬取结束后才安排，这里如实说明，避免误以为不会重试
        retry = '<span class="tag tag-retry-pending">爬取结束后安排重试</span>';
      } else {
        retry = '<span class="tag tag-no-retry">未安排重试</span>';
      }
    }
    const pct = (r.total && r.total > 0) ? Math.min(100, Math.round(100 * r.crawled / r.total)) : 0;
    html += '<tr class="' + (r.name === current ? 'current' : '') + '">'
      + '<td title="' + esc(r.name) + '">' + esc(r.name) + '</td>'
      + '<td class="num">' + r.crawled + ' / ' + total
      + (r.status === 'running' ? '<div class="progress-mini"><span style="width:' + pct + '%"></span></div>' : '')
      + '</td>'
      + '<td><span class="tag tag-cat-' + st + '"'
      + (r.derived ? ' title="此前已完成（本轮没有重新扫描）"' : '') + '>'
      + esc(statusLabel) + '</span></td>'
      + '<td>' + retry + '</td></tr>';
  });
  html += '</tbody></table></div></details></div>';
  return html;
}

function renderUploadProgress(p, running, uploadSig) {
  const box = document.getElementById('top_upload_progress');
  const barWrap = document.getElementById('top_upload_bar_wrap');
  const bar = document.getElementById('top_upload_bar');
  const text = document.getElementById('top_upload_text');
  if (!box) return;

  const clearTimer = () => {
    if (uploadHideTimer) { clearTimeout(uploadHideTimer); uploadHideTimer = null; }
  };
  const startHide = () => {
    clearTimer();
    uploadHideTimer = setTimeout(() => {
      box.style.display = 'none';
      uploadHideTimer = null;
    }, 10000);
  };

  if (running) {
    clearTimer();
    box.style.display = 'block';
    barWrap.style.display = 'block';
    const total = p.total || 0;
    const cur = p.current || 0;
    const pct = total > 0 ? Math.min(100, Math.round(100 * cur / total)) : 0;
    bar.style.width = pct + '%';
    text.textContent = p.message || ('上传中 ' + cur + '/' + (total || '—'));
    uploadSessionActive = true;
    lastUploadSig = uploadSig;
    return;
  }

  // 上传已结束：本轮看到过运行状态，或上传时间戳发生变化（上传在两轮轮询之间完成）
  const finishedBySig =
    lastUploadSig !== null && uploadSig !== null && uploadSig !== lastUploadSig;
  lastUploadSig = uploadSig;
  if (uploadSessionActive || finishedBySig) {
    uploadSessionActive = false;
    box.style.display = 'block';
    barWrap.style.display = 'none';
    bar.style.width = '100%';
    text.textContent = p.message || '上传完成';
    startHide();
  }
}

function renderAlerts(alerts) {
  const el = document.getElementById('alerts');
  if (!alerts || alerts.length === 0) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = alerts.map(a => {
    const cls = a.level === 'error' ? 'alert alert-error'
      : (a.level === 'info' ? 'alert alert-info' : 'alert alert-warning');
    return '<div class="' + cls + '" data-alert-key="' + esc(a.key) + '">'
      + '<span>' + esc(a.message) + '</span>'
      + '<button onclick="dismissAlert(this)">关闭</button>'
      + '</div>';
  }).join('');
}

async function dismissAlert(btn) {
  const box = btn.closest('[data-alert-key]');
  const key = box ? box.getAttribute('data-alert-key') : '';
  btn.disabled = true;
  await api('/api/clear_alert', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ key: key }),
  });
  if (box) box.remove();
}

const SOURCE_LABELS = { ccdi: '中纪检', bit: '校纪检' };

function sourceTag(src) {
  const label = SOURCE_LABELS[src] || (src || '—');
  const cls = src === 'ccdi' ? 'tag-src-ccdi' : (src === 'bit' ? 'tag-src-bit' : 'tag-no');
  return '<span class="tag ' + cls + '">' + esc(label) + '</span>';
}

function updateSortArrows() {
  for (const key of ['source', 'title', 'category_path', 'crawled_at', 'uploaded']) {
    const arrow = document.getElementById('arrow_' + key);
    if (!arrow) continue;
    const th = arrow.closest('th');
    if (filesSort === key) {
      arrow.textContent = (filesDir === 'asc') ? ' ▲' : ' ▼';
      if (th) th.classList.add('active');
    } else {
      arrow.textContent = ' ↕';
      if (th) th.classList.remove('active');
    }
  }
}

function renderFiles(data) {
  filesPages = data.pages || 1;
  filesPage = data.page || 1;
  const tbody = document.getElementById('files');
  if (!data.files || data.files.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5">暂无数据</td></tr>';
  } else {
    tbody.innerHTML = data.files.map(f => '<tr>'
      + '<td>' + sourceTag(f.source) + '</td>'
      + '<td>' + esc(displayTitle(f.title))
      + (f.version_count > 1
          ? ' <span class="tag ' + (f.is_latest ? 'badge-latest' : 'tag-no') + '">'
            + f.version_count + ' 个版本' + (f.is_latest ? ' · 最新' : ' · 历史') + '</span>'
          : '')
      + (f.date_source === 'filename'
          ? ' <span class="tag tag-no">文件名补日期</span>' : '')
      + '</td>'
      + '<td>' + (f.category_path ? '<span class="tag tag-cat">' + esc(f.category_path) + '</span>' : '') + '</td>'
      + '<td>' + esc(f.crawled_at || '') + '</td>'
      + '<td>' + (f.uploaded_at
          ? '<span class="tag tag-yes">是</span>'
          : '<span class="tag tag-no">否</span>') + '</td>'
      + '</tr>').join('');
  }
  document.getElementById('files_page_info').textContent =
    '第 ' + filesPage + ' / ' + filesPages + ' 页 · 共 ' + (data.total || 0) + ' 条';
  document.getElementById('files_prev').disabled = filesPage <= 1;
  document.getElementById('files_next').disabled = filesPage >= filesPages;
  renderPager();
  updateSortArrows();
  renderVersionFamilies(data.multi_versions || []);
}

function renderVersionFamilies(families) {
  allVersionFamilies = families || [];
  renderVersionFamilyPage();
}

function filteredVersionFamilies() {
  const query = versionQuery.trim().toLowerCase();
  return allVersionFamilies.filter(family => {
    if (versionSource !== 'all' && family.source !== versionSource) return false;
    if (!query) return true;
    const versions = family.versions || [];
    const haystack = [
      family.title || '',
      ...versions.flatMap(v => [v.title || '', v.link_suffix || '', v.location || ''])
    ].join(' ').toLowerCase();
    return haystack.includes(query);
  });
}

function renderVersionFamilyPage() {
  const el = document.getElementById('version_families');
  if (!el) return;
  const families = filteredVersionFamilies();
  const pages = Math.max(1, Math.ceil(families.length / versionPageSize));
  versionPage = Math.min(Math.max(1, versionPage), pages);
  const start = (versionPage - 1) * versionPageSize;
  const pageRows = families.slice(start, start + versionPageSize);
  if (!families.length) {
    el.innerHTML = '<div class="muted">当前没有多版本文档。</div>';
  } else {
    el.innerHTML = '<div class="muted" style="margin-bottom:6px">共 ' + families.length
      + ' 个标题族；展开可查看全部本地版本、链接后缀和位置。</div>'
      + pageRows.map((family, idx) => {
      const versions = family.versions || [];
      return '<details class="version-family"' + (versionPage === 1 && idx === 0 ? ' open' : '') + '>'
        + '<summary><b>' + esc(displayTitle(family.title)) + '</b>'
        + sourceTag(family.source)
        + '<span class="tag badge-latest">最新版本</span>'
        + '<span class="muted">共 ' + versions.length + ' 个版本</span></summary>'
        + '<div class="version-row version-head"><div>链接后缀</div><div>标题</div><div>位置</div><div>发布日期</div></div>'
        + versions.map(v =>
          '<div class="version-row' + (v.is_latest ? ' latest' : '') + '">'
          + '<div><span class="tag tag-time">' + esc(v.link_suffix || '—') + '</span></div>'
          + '<div>' + esc(displayTitle(v.title || '')) + '</div>'
          + '<div title="' + esc(v.location || '') + '">' + esc(v.location ? shortUrl(v.location) : '—') + '</div>'
          + '<div>' + esc(v.publish_date || '日期未知')
          + (v.date_source === 'filename'
              ? ' <span class="tag tag-no">文件名补年份</span>' : '')
          + (v.is_latest ? ' <span class="tag badge-latest">最新</span>' : '')
          + '</div></div>'
        ).join('')
        + '</details>';
      }).join('');
  }
  const pager = document.getElementById('version_pager');
  if (pager) {
    const wanted = new Set([1, pages, versionPage - 2, versionPage - 1, versionPage, versionPage + 1, versionPage + 2]);
    const nums = [...wanted].filter(n => n >= 1 && n <= pages).sort((a, b) => a - b);
    let html = '';
    let prev = 0;
    nums.forEach(n => {
      if (prev && n - prev > 1) html += '<span class="gap">…</span>';
      html += n === versionPage
        ? '<button class="active" disabled>' + n + '</button>'
        : '<button onclick="setVersionPage(' + n + ')">' + n + '</button>';
      prev = n;
    });
    pager.innerHTML = html;
  }
  const info = document.getElementById('version_page_info');
  if (info) info.textContent = '第 ' + versionPage + ' / ' + pages + ' 页 · 共 ' + families.length + ' 个标题族';
  const jump = document.getElementById('version_jump');
  if (jump) {
    jump.max = pages;
    jump.value = versionPage;
  }
  document.querySelectorAll('#version_source_tabs .tab').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.source === versionSource);
  });
}

function toggleVersionsCard() {
  versionCardOpen = !versionCardOpen;
  const body = document.getElementById('version_card_body');
  const btn = document.getElementById('version_toggle_btn');
  if (body) body.style.display = versionCardOpen ? 'block' : 'none';
  if (btn) btn.textContent = versionCardOpen ? '▼' : '▶';
}

function changeVersionSearch(value) {
  versionQuery = value || '';
  versionPage = 1;
  renderVersionFamilyPage();
}

function setVersionSource(value) {
  versionSource = ['ccdi', 'bit'].includes(value) ? value : 'all';
  versionPage = 1;
  renderVersionFamilyPage();
}

function changeVersionPageSize(value) {
  versionPageSize = Math.max(1, parseInt(value, 10) || 10);
  versionPage = 1;
  renderVersionFamilyPage();
}

function setVersionPage(page) {
  const pages = Math.max(1, Math.ceil(filteredVersionFamilies().length / versionPageSize));
  versionPage = Math.min(Math.max(1, parseInt(page, 10) || 1), pages);
  renderVersionFamilyPage();
}

function jumpVersionPage(value) {
  setVersionPage(value);
}

// 历史记录里只显示标题本身，去掉可能带上的「子分类[12] 」/「[12] 」前缀
function displayTitle(title) {
  return String(title || '').replace(/^\\S{0,20}?\\[\\d+\\]\\s+/, '').trim() || String(title || '');
}

// 页码按钮：首页、末页、当前页 ±2，其余用省略号
function renderPager() {
  const box = document.getElementById('files_pager');
  if (!box) return;
  const pages = filesPages || 1;
  const cur = filesPage || 1;
  const wanted = new Set([1, pages, cur - 2, cur - 1, cur, cur + 1, cur + 2]);
  const nums = [...wanted].filter(n => n >= 1 && n <= pages).sort((a, b) => a - b);
  let html = '';
  let prev = 0;
  nums.forEach(n => {
    if (prev && n - prev > 1) html += '<span class="gap">…</span>';
    html += n === cur
      ? '<button class="active" disabled>' + n + '</button>'
      : '<button onclick="setFilesPage(' + n + ')">' + n + '</button>';
    prev = n;
  });
  box.innerHTML = html;
}

async function loadFiles() {
  try {
    const res = await fetch(
      '/api/files?page=' + filesPage + '&page_size=' + filesPageSize
      + '&sort=' + filesSort + '&dir=' + filesDir
    );
    renderFiles(await res.json());
  } catch (e) {
    /* 忽略瞬时错误，下一轮刷新会重试 */
  }
}

function setFilesPage(p) {
  if (p < 1 || p > filesPages) return;
  filesPage = p;
  loadFiles();
}

function setFilesPageSize(n) {
  filesPageSize = parseInt(n, 10) || 20;
  filesPage = 1;
  loadFiles();
}

function sortFilesBy(key) {
  if (filesSort === key) {
    filesDir = (filesDir === 'asc') ? 'desc' : 'asc';
  } else {
    filesSort = key;
    filesDir = 'asc';
  }
  filesPage = 1;
  loadFiles();
}

async function refresh() {
  let data;
  try {
    const res = await fetch('/api/status');
    data = await res.json();
  } catch (e) {
    return;  // 本轮拿不到状态就跳过，下一次轮询继续
  }
  try {
    const pr = (data.summary && data.summary.progress) || {};
    uiStateBusy = ['ccdi', 'bit', 'upload'].some(
      k => (pr[k] || {}).status === 'running'
    ) || !!(data.settings && (data.settings.migration_running || data.settings.index_building));
  } catch (e) {
    uiStateBusy = false;
  }
  // 每段各自兜底：任何一段出错都不会让进度卡片停止刷新
  try { if (data.settings) applySettings(data.settings); }
  catch (e) { console.error('applySettings 失败', e); }
  try { renderAlerts(data.alerts); }
  catch (e) { console.error('renderAlerts 失败', e); }
  const uploadRunning = data.settings ? !!data.settings.upload_running : false;
  const crawlRunning = data.settings ? !!data.settings.crawl_running : false;
  const migrationRunning = data.settings ? !!data.settings.migration_running : false;
  if (prevUploadRunning && !uploadRunning) {
    refreshAgentStatus();
  }
  const runningNow = uploadRunning || crawlRunning || migrationRunning;
  if (runningNow || prevRunningAny) {
    loadFiles();
  }
  prevUploadRunning = uploadRunning;
  prevRunningAny = runningNow;
  const s = data.summary;
  try {
    renderAgentTaskCard(
      {
        progress: s.progress.upload,
        parse_queue: s.parse_queue || {},
        migration: s.dataset_migration || {},
        version_index: (data.settings && data.settings.version_index_status) || {},
        backfill_status: s.backfill_status || {},
        date_progress: s.date_progress || {},
        maintenance_flags: (data.settings && data.settings.maintenance_flags) || {}
      },
      data.settings.parse_settings || {}
    );
  } catch (e) { console.error('renderAgentTaskCard 失败', e); }
  lastSettings = data.settings || {};
  const firstRun = data.settings ? !!data.settings.first_run_pending : false;
  const schedulerOff = data.settings && data.settings.scheduler_enabled === false;
  try {
    renderUploadProgress(
      s.progress.upload,
      uploadRunning || migrationRunning,
      JSON.stringify(s.last_upload_time || {})
    );
  } catch (e) { console.error('renderUploadProgress 失败', e); }
  const curCat = s.current_category
    ? '<div class="pc-msg" style="margin-top:6px">当前分类：' + esc(s.current_category) + '</div>'
    : '';
  const throttleNext = (data.settings && data.settings.next_throttle_retry_at)
    ? '<div class="pc-msg" style="margin-top:6px">限流自动重试：'
      + esc(fmtTime(data.settings.next_throttle_retry_at)) + '</div>'
    : '';
  document.getElementById('summary').innerHTML =
    progressCard('中纪检', s.progress.ccdi, s.last_crawl_time.ccdi, s.last_upload_time.ccdi,
      curCat + throttleNext)
    + progressCard('校纪检', s.progress.bit, s.last_crawl_time.bit, s.last_upload_time.bit)
    + `<div class="card progress-card">
      <div class="pc-title">今日 / 总量</div>
      <div class="pc-msg" style="margin-top:2px">` +
      (firstRun
        ? '<span class="tag tag-first">首次执行</span>'
        : '<span class="tag tag-daily">日常执行</span>') +
      (schedulerOff ? ' <span class="tag tag-no">定时任务未启用</span>' : '') +
      `</div>
      <div class="pc-num">今日新增 ${s.today_new}</div>
      <div class="pc-msg">文档总数 ${s.total_docs}（中纪检 ${s.count_ccdi} · 校纪检 ${s.count_bit}）</div>
      <div class="pc-msg">上传任务：${s.progress.upload.message || '—'}</div>
    </div>`;
  const panel = document.getElementById('category_panel');
  if (panel) renderCategoryPanel(panel, categoryPanel(s));
  const rangeCard = document.getElementById('range_card');
  if (rangeCard) {
    rangeCard.innerHTML = '<div class="side-title">上次区间 / 有效编号</div>'
      + (ccdiRangeInfo(s.id_walk_ccdi) || '<div class="pc-msg">暂无记录</div>');
  }
  const failedCard = document.getElementById('failed_card');
  if (failedCard) failedCard.innerHTML = failedCardHtml(data.settings || {});
}

// 子分类面板每秒都会刷新：内容没变就不重建；要重建时保留滚动位置与折叠状态，
// 否则用户往下滑的时候会被拉回开头。
function renderCategoryPanel(panel, html) {
  const oldScroll = panel.querySelector('.cat-scroll');
  const oldTop = oldScroll ? oldScroll.scrollTop : 0;
  const oldDetails = panel.querySelector('details');
  const wasOpen = oldDetails ? oldDetails.open : true;
  if (panel.dataset.sig === html) return;
  panel.dataset.sig = html;
  panel.innerHTML = html;
  const scroll = panel.querySelector('.cat-scroll');
  if (scroll && oldTop > 0) scroll.scrollTop = oldTop;
  const details = panel.querySelector('details');
  if (details && !wasOpen) details.open = false;
}
// 任务运行中 1 秒刷新一次（进度条更跟手），空闲时 5 秒
async function refreshLoop() {
  try { await refresh(); } catch (e) { /* 忽略瞬时错误 */ }
  setTimeout(refreshLoop, uiRunning ? 1000 : 5000);
}
refreshLoop();
refreshAgentStatus();
loadFiles();
setInterval(refreshAgentStatus, 30000);
</script>
</body>
</html>
"""


def create_monitor_app(controller) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(
            HTML_TEMPLATE, output_dir=str(controller.output_dir)
        )

    @app.get("/api/status")
    @_safe_api("status")
    def api_status():
        state = controller.state
        # 终端里跑的单次任务会写同一个状态文件，这里先热重载以保证 UI 实时同步
        state.reload()
        snap = state.snapshot()
        docs = list(snap.get("documents", {}).values())
        today = date.today().isoformat()
        daily = snap.get("daily_new_count", {})

        def prog_percent(prog: dict) -> float:
            total = prog.get("total") or 0
            cur = prog.get("current") or 0
            if total <= 0:
                return 0.0
            return min(100.0, round(100.0 * cur / total, 1))

        progress = snap.get("progress", {})
        for key in ("ccdi", "bit", "upload"):
            progress.setdefault(
                key, {"status": "idle", "current": 0, "total": 0, "message": ""}
            )
            progress[key]["percent"] = prog_percent(progress[key])

        settings = controller.get_settings()

        return jsonify(
            {
                "summary": {
                    "last_crawl_time": snap.get("last_crawl_time", {}),
                    "last_upload_time": snap.get("last_upload_time", {}),
                    "today_new": daily.get(today, 0),
                    "total_docs": len(docs),
                    "count_ccdi": sum(1 for d in docs if d.get("source") == "ccdi"),
                    "count_bit": sum(1 for d in docs if d.get("source") == "bit"),
                    "progress": progress,
                    # 中纪检被跳过的子分类（界面标识 + 自动重试时间）
                    "skipped_ccdi": controller.skipped_categories(),
                    "skipped_retry_at": controller.next_skipped_retry_at(),
                    "categories_ccdi": _ccdi_category_rows(snap),
                    "current_category": progress["ccdi"].get("category", ""),
                    # 中纪检上次各区间爬到哪（正向边界 / 倒爬起点与游标 / 最大有效编号）
                    "id_walk_ccdi": (snap.get("id_walk", {}) or {}).get("ccdi", {}) or {},
                    "parse_queue": snap.get("parse_queue", {}),
                    "dataset_migration": snap.get("dataset_migration", {}),
                    "backfill_status": snap.get("backfill_status", {}),
                    "date_progress": controller.date_source_stats(snap),
                    "version_index": snap.get("version_index_meta", {}),
                },
                "settings": settings,
                "alerts": state.list_alerts(),
            }
        )

    @app.get("/api/files")
    @_safe_api("files")
    def api_files():
        """爬取文件列表：支持分页、按列排序。"""
        controller.state.reload()
        docs = controller.state.list_documents()
        sort_key = request.args.get("sort", "crawled_at")
        direction = "asc" if request.args.get("dir") == "asc" else "desc"
        try:
            page = max(1, int(request.args.get("page", 1)))
            page_size = max(1, min(500, int(request.args.get("page_size", 20))))
        except (TypeError, ValueError):
            page, page_size = 1, 20

        keyfuncs = {
            "source": lambda d: d.get("source") or "",
            "title": lambda d: d.get("title") or "",
            "category_path": lambda d: d.get("category_path") or "",
            "crawled_at": lambda d: d.get("crawled_at") or "",
            "uploaded": lambda d: 1 if d.get("uploaded_at") else 0,
        }
        keyfunc = keyfuncs.get(sort_key, keyfuncs["crawled_at"])
        docs.sort(key=keyfunc, reverse=(direction == "desc"))
        version_index = controller.state.snapshot().get("version_index", {})
        family_by_doc: dict[str, dict] = {}
        for family in version_index.values():
            versions = family.get("versions", []) if isinstance(family, dict) else []
            for version in versions:
                doc_key = str(version.get("document_key") or "")
                if doc_key:
                    family_by_doc[doc_key] = family

        total = len(docs)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        start = (page - 1) * page_size
        rows = []
        for doc in docs[start : start + page_size]:
            family = family_by_doc.get(str(doc.get("doc_key") or ""), {})
            versions = family.get("versions") or []
            rows.append(
                {
                    "source": doc.get("source"),
                    "title": doc.get("title"),
                    "category_path": doc.get("category_path"),
                    "crawled_at": doc.get("crawled_at"),
                    "publish_date": doc.get("publish_date"),
                    "date_source": doc.get("date_source"),
                    "uploaded_at": doc.get("uploaded_at"),
                    "url": doc.get("url"),
                    "version_id": doc.get("version_id"),
                    "version_family_key": doc.get("version_family_key"),
                    "version_count": len(versions),
                    "is_latest": bool(
                        family.get("latest_version_id")
                        and family.get("latest_version_id") == doc.get("version_id")
                    ),
                }
            )
        return jsonify(
            {
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": pages,
                "sort": sort_key,
                "dir": direction,
                "files": rows,
                "multi_versions": controller.state.version_families(multi_only=True),
            }
        )

    @app.post("/api/settings")
    def api_settings():
        payload = request.get_json(silent=True) or {}
        try:
            settings = controller.set_settings(
                mode=payload.get("mode"),
                test_count=payload.get("test_count"),
                test_source=payload.get("test_source"),
                full_test=payload.get("full_test"),
                test_schedule_mode=payload.get("test_schedule_mode"),
                upload_mode=payload.get("upload_mode"),
                parallel=payload.get("parallel"),
                crawl_interval_minutes=payload.get("crawl_interval_minutes"),
                upload_interval_minutes=payload.get("upload_interval_minutes"),
                crawl_time=payload.get("crawl_time"),
                upload_time=payload.get("upload_time"),
                pdf_scale_bit=payload.get("pdf_scale_bit"),
                upload_interval_days=payload.get("upload_interval_days"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"settings": settings})

    @app.post("/api/output_dir")
    def api_output_dir():
        payload = request.get_json(silent=True) or {}
        try:
            settings = controller.set_output_dir(payload.get("output_dir", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"settings": settings})

    @app.post("/api/crawl")
    def api_crawl():
        payload = request.get_json(silent=True) or {}
        source = payload.get("source", "all")
        if source not in ("all", "ccdi", "bit"):
            return jsonify({"error": "source 必须为 all/ccdi/bit"}), 400
        try:
            return jsonify(controller.start_crawl(source))
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/upload")
    def api_upload():
        try:
            return jsonify(controller.start_upload())
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/migrate")
    def api_migrate():
        try:
            return jsonify(controller.start_dataset_migration())
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/stop")
    @_safe_api("stop")
    def api_stop():
        return jsonify(controller.stop_all())

    @app.post("/api/retry_failed")
    @_safe_api("retry_failed")
    def api_retry_failed():
        try:
            return jsonify(controller.start_failed_retry_now())
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/pause")
    def api_pause():
        return jsonify(controller.pause())

    @app.post("/api/resume")
    def api_resume():
        return jsonify(controller.resume())

    @app.post("/api/clear_alert")
    def api_clear_alert():
        payload = request.get_json(silent=True) or {}
        key = payload.get("key")
        if key:
            controller.state.clear_alert(str(key))
        return jsonify({"cleared": bool(key)})

    @app.get("/api/agent_status")
    @_safe_api("agent_status")
    def api_agent_status():
        return jsonify(controller.agent_status())

    @app.post("/api/agent_config")
    def api_agent_config():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(
                controller.set_agent_config(
                    base_url=payload.get("base_url", ""),
                    api_key=payload.get("api_key", ""),
                    dataset_name_ccdi=payload.get("dataset_name_ccdi", ""),
                    dataset_name_bit=payload.get("dataset_name_bit", ""),
                )
            )
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

    return app


def run_monitor_server(controller, host: str, port: int) -> None:
    """监控 Web 服务：跑在独立线程里，崩了自动重建，永远不影响爬取/上传。"""
    app = create_monitor_app(controller)
    while True:
        try:
            app.run(host=host, port=port, threaded=True, use_reloader=False)
            logger.warning("监控 UI 服务已退出，30 秒后重新启动")
        except SystemExit:
            raise
        except OSError as exc:
            logger.error(
                "监控 UI 启动失败：%s:%s 无法监听（%s）。"
                "很可能已有一个旧进程占着这个端口（用 `ss -ltnp | grep %s` 查一下），"
                "先结束旧进程；本进程会继续按定时任务运行，30 秒后自动重试监听。",
                host,
                port,
                exc,
                port,
            )
        except Exception:  # noqa: BLE001
            logger.exception("监控 UI 异常退出，30 秒后重启（爬取/上传不受影响）")
        time.sleep(30)
