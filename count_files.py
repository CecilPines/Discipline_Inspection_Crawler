#!/usr/bin/env python3
"""统计 data 目录里 中纪检 / 校纪检 的文件数量。

用法：
    .venv/bin/python count_files.py                 # 读 config.yaml 的 output_dir
    .venv/bin/python count_files.py --dir data/regulations
    .venv/bin/python count_files.py --all-files     # 连非 PDF 一起数
    .venv/bin/python count_files.py --detail        # 中纪检按子分类细分

只读脚本，不会修改任何文件。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SOURCES = ("中纪检", "校纪检")


def load_output_dir() -> Path:
    """从 config.yaml 读 output_dir；读不到就用 data/regulations。"""
    cfg_path = PROJECT_ROOT / "config.yaml"
    try:
        import yaml

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        raw = str(data.get("output_dir") or "").strip()
        if raw:
            path = Path(raw)
            return path if path.is_absolute() else (PROJECT_ROOT / path)
    except Exception:
        pass
    return PROJECT_ROOT / "data" / "regulations"


def find_source_dir(base: Path, name: str) -> Path | None:
    """在 base 下找 中纪检 / 校纪检 目录（兼容把 data 直接指过来）。"""
    direct = base / name
    if direct.is_dir():
        return direct
    for candidate in sorted(base.rglob(name)):
        if candidate.is_dir():
            return candidate
    return None


def count_files(folder: Path, *, pdf_only: bool = True) -> int:
    total = 0
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if pdf_only and path.suffix.lower() != ".pdf":
            continue
        total += 1
    return total


def count_direct_files(folder: Path, *, pdf_only: bool = True) -> int:
    """只数这个目录本身的文件（不递归）。"""
    total = 0
    for path in folder.iterdir():
        if path.is_file() and (not pdf_only or path.suffix.lower() == ".pdf"):
            total += 1
    return total


def registered_counts(*bases: Path) -> dict[str, int] | None:
    """顺带读一下状态文件里登记的条数（找不到文件则返回 None）。"""
    for base in bases:
        state_path = base / "crawler_state.json"
        if not state_path.exists():
            continue
        try:
            snap = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        counts = {"ccdi": 0, "bit": 0}
        for meta in (snap.get("documents") or {}).values():
            source = meta.get("source")
            if source in counts:
                counts[source] += 1
        return counts
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="统计中纪检 / 校纪检的文件数量")
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="输出目录（默认读 config.yaml 的 output_dir，再退回 data/regulations）",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="统计所有文件（默认只数 .pdf）",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="中纪检按子分类细分列出",
    )
    args = parser.parse_args()

    base = (args.dir or load_output_dir()).expanduser().resolve()
    pdf_only = not args.all_files
    unit = "个文件" if args.all_files else "个 PDF"

    print(f"输出目录：{base}")
    if not base.is_dir():
        print(f"目录不存在：{base}")
        return

    found: dict[str, Path | None] = {}
    totals: dict[str, int] = {}
    for name in SOURCES:
        folder = find_source_dir(base, name)
        found[name] = folder
        totals[name] = count_files(folder, pdf_only=pdf_only) if folder else 0

    for name in SOURCES:
        folder = found[name]
        if folder is None:
            print(f"{name}：未找到目录（{base / name}）")
            continue
        print(f"{name}：{totals[name]} {unit}")

    print(f"合计：{sum(totals.values())} {unit}")

    parent_dirs = {p.parent for p in found.values() if p is not None}
    reg = registered_counts(base, *sorted(parent_dirs))
    if reg is not None:
        print(
            f"（状态文件登记：中纪检 {reg['ccdi']} · 校纪检 {reg['bit']}）"
        )
        for name, key in (("中纪检", "ccdi"), ("校纪检", "bit")):
            diff = totals.get(name, 0) - reg[key]
            if diff:
                print(f"  {name}：磁盘比登记多 {diff} 个" if diff > 0 else f"  {name}：磁盘比登记少 {-diff} 个")

    if args.detail:
        for name in SOURCES:
            folder = found.get(name)
            if folder is None or not folder.is_dir():
                continue
            print(f"\n{name} 目录明细（主分类 / 子分类）：")
            mains = sorted(p for p in folder.iterdir() if p.is_dir())
            direct = count_direct_files(folder, pdf_only=pdf_only)
            for main in mains:
                print(f"  {main.name}：{count_files(main, pdf_only=pdf_only)} {unit}")
                for sub in sorted(p for p in main.iterdir() if p.is_dir()):
                    print(f"    {sub.name}：{count_files(sub, pdf_only=pdf_only)} {unit}")
                sub_direct = count_direct_files(main, pdf_only=pdf_only)
                if sub_direct:
                    print(f"    （直属文件）：{sub_direct} {unit}")
            if direct:
                print(f"  （直属文件）：{direct} {unit}")
            if not mains and not direct:
                print("  （空目录）")


if __name__ == "__main__":
    main()
