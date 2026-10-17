#!/usr/bin/env python3
"""
RAGFlow 知识库管理脚本
- 创建知识库
- 上传文档
- 触发解析
- 查询状态
"""

import argparse
import sys
import time
from pathlib import Path

import requests

BASE_URL = "http://10.108.25.212:9380/api/v1"
API_KEY = "ragflow-QwZDlmZDg0ZWZhYTExZjA4MDU4YWEzOG"

SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".md", ".mdx",
    ".csv", ".xlsx", ".xls", ".ppt", ".pptx",
    ".jpeg", ".jpg", ".png", ".tif", ".gif",
}

HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _check_response(resp, action):
    """检查 API 响应，失败时打印错误并退出。"""
    data = resp.json()
    if data.get("code") != 0:
        print(f"[ERROR] {action} 失败: {data.get('message', '未知错误')}")
        sys.exit(1)
    return data


# ── 1. 创建知识库 ──────────────────────────────────────────────

def create_dataset(name, chunk_method="naive",
                   description=None, chunk_token_num=512):
    """创建一个新的知识库并返回 dataset_id。"""
    payload = {
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

    resp = requests.post(f"{BASE_URL}/datasets", headers=HEADERS,
                         json=payload)
    data = _check_response(resp, "创建知识库")
    dataset_id = data["data"]["id"]
    print(f"[OK] 知识库已创建: {name}  (id={dataset_id})")
    return dataset_id


# ── 2. 上传文档 ─────────────────────────────────────────────────

def upload_documents(dataset_id, file_paths):
    """上传文件列表到指定知识库，返回 document_id 列表。"""
    files = []
    for p in file_paths:
        path = Path(p)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            print(f"[SKIP] 不支持的文件类型: {path.name}")
            continue
        if not path.exists():
            print(f"[SKIP] 文件不存在: {path}")
            continue
        files.append(("file", (path.name, open(path, "rb"))))

    if not files:
        print("[ERROR] 没有可上传的文件")
        sys.exit(1)

    resp = requests.post(
        f"{BASE_URL}/datasets/{dataset_id}/documents",
        headers=HEADERS,
        files=files,
    )
    # 关闭文件句柄
    for _, (_, fobj) in files:
        fobj.close()

    data = _check_response(resp, "上传文档")
    doc_ids = [doc["id"] for doc in data["data"]]
    print(f"[OK] 已上传 {len(doc_ids)} 个文档")
    for doc in data["data"]:
        print(f"     - {doc['name']}  (id={doc['id']})")
    return doc_ids


def upload_directory(dataset_id, dir_path):
    """递归扫描目录中所有支持的文件并上传。"""
    dir_path = Path(dir_path)
    if not dir_path.is_dir():
        print(f"[ERROR] 目录不存在: {dir_path}")
        sys.exit(1)

    all_files = [
        str(f) for f in dir_path.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not all_files:
        print(f"[ERROR] 目录中没有支持的文件: {dir_path}")
        sys.exit(1)

    print(f"[INFO] 在 {dir_path} 中找到 {len(all_files)} 个文件")
    return upload_documents(dataset_id, all_files)


# ── 3. 触发解析 ─────────────────────────────────────────────────

def start_parsing(dataset_id, document_ids):
    """触发文档的异步解析。"""
    resp = requests.post(
        f"{BASE_URL}/datasets/{dataset_id}/chunks",
        headers=HEADERS,
        json={"document_ids": document_ids},
    )
    _check_response(resp, "触发解析")
    print(f"[OK] 已触发 {len(document_ids)} 个文档的解析")


# ── 4. 查询解析状态 ─────────────────────────────────────────────

def check_status(dataset_id, wait=False, interval=5):
    """查询文档解析状态，wait=True 时轮询直到全部完成。"""
    while True:
        resp = requests.get(
            f"{BASE_URL}/datasets/{dataset_id}/documents",
            headers=HEADERS,
            params={"page": 1, "page_size": 100},
        )
        data = _check_response(resp, "查询状态")
        docs = data["data"]

        all_done = True
        print(f"\n{'文件名':<40} {'状态':<10} {'进度':<8} {'分块数'}")
        print("-" * 75)
        for doc in docs:
            status = doc.get("run", "UNKNOWN")
            progress = doc.get("progress", 0)
            chunks = doc.get("chunk_count", 0)
            name = doc.get("name", "?")
            if len(name) > 38:
                name = name[:35] + "..."
            print(f"{name:<40} {status:<10} {progress*100:>5.1f}%  {chunks:>6}")
            if status in ("UNSTART", "RUNNING"):
                all_done = False

        if not wait or all_done:
            break
        print(f"\n[INFO] 解析进行中，{interval}秒后重新查询...")
        time.sleep(interval)

    if wait:
        print("\n[OK] 所有文档解析完成！")


# ── 5. 列出知识库 ──────────────────────────────────────────────

def list_datasets():
    """列出所有已有的知识库。"""
    resp = requests.get(f"{BASE_URL}/datasets", headers=HEADERS,
                        params={"page": 1, "page_size": 100})
    data = _check_response(resp, "列出知识库")

    print(f"\n{'知识库名称':<30} {'文档数':<8} {'分块数':<10} {'ID'}")
    print("-" * 90)
    for ds in data["data"]:
        name = ds["name"]
        if len(name) > 28:
            name = name[:25] + "..."
        print(f"{name:<30} {ds.get('document_count',0):<8} "
              f"{ds.get('chunk_count',0):<10} {ds['id']}")


# ── CLI ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="RAGFlow 知识库管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 列出所有知识库
  python ragflow_kb_manager.py list

  # 创建知识库 + 上传文件 + 自动解析
  python ragflow_kb_manager.py create --name "测试知识库" --files doc1.pdf doc2.txt

  # 创建知识库 + 上传整个目录
  python ragflow_kb_manager.py create --name "项目文档" --dir ./documents/

  # 向已有知识库上传文件并解析
  python ragflow_kb_manager.py upload --dataset-id <ID> --files a.pdf b.docx

  # 查看知识库中文档的解析状态
  python ragflow_kb_manager.py status --dataset-id <ID>

  # 查看状态并等待解析完成
  python ragflow_kb_manager.py status --dataset-id <ID> --wait
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="列出所有知识库")

    # create
    p_create = sub.add_parser("create", help="创建知识库并可选上传文件")
    p_create.add_argument("--name", required=True, help="知识库名称")
    p_create.add_argument("--description", default=None, help="知识库描述")
    p_create.add_argument("--chunk-method", default="naive",
                          choices=["naive", "book", "qa", "table", "paper",
                                   "laws", "manual", "presentation", "one"],
                          help="分块方法 (默认 naive)")
    p_create.add_argument("--chunk-token-num", type=int, default=512,
                          help="每块 token 数 (默认 512)")
    p_create.add_argument("--files", nargs="+", help="要上传的文件列表")
    p_create.add_argument("--dir", help="要上传的目录（递归扫描）")
    p_create.add_argument("--no-parse", action="store_true",
                          help="上传后不自动解析")
    p_create.add_argument("--wait", action="store_true",
                          help="等待解析完成")

    # upload
    p_upload = sub.add_parser("upload", help="向已有知识库上传文件")
    p_upload.add_argument("--dataset-id", required=True, help="知识库 ID")
    p_upload.add_argument("--files", nargs="+", help="要上传的文件列表")
    p_upload.add_argument("--dir", help="要上传的目录")
    p_upload.add_argument("--no-parse", action="store_true",
                          help="上传后不自动解析")
    p_upload.add_argument("--wait", action="store_true",
                          help="等待解析完成")

    # status
    p_status = sub.add_parser("status", help="查看文档解析状态")
    p_status.add_argument("--dataset-id", required=True, help="知识库 ID")
    p_status.add_argument("--wait", action="store_true",
                          help="轮询等待解析完成")

    args = parser.parse_args()

    if args.command == "list":
        list_datasets()
        return

    if args.command == "create":
        dataset_id = create_dataset(
            name=args.name,
            chunk_method=args.chunk_method,
            description=args.description,
            chunk_token_num=args.chunk_token_num,
        )
        doc_ids = []
        if args.dir:
            doc_ids = upload_directory(dataset_id, args.dir)
        elif args.files:
            doc_ids = upload_documents(dataset_id, args.files)

        if doc_ids and not args.no_parse:
            start_parsing(dataset_id, doc_ids)
            if args.wait:
                check_status(dataset_id, wait=True)
            else:
                print("[TIP] 使用以下命令查看解析进度:")
                print(f"  python ragflow_kb_manager.py status --dataset-id {dataset_id} --wait")
        return

    if args.command == "upload":
        doc_ids = []
        if args.dir:
            doc_ids = upload_directory(args.dataset_id, args.dir)
        elif args.files:
            doc_ids = upload_documents(args.dataset_id, args.files)
        else:
            print("[ERROR] 请指定 --files 或 --dir")
            sys.exit(1)

        if doc_ids and not args.no_parse:
            start_parsing(args.dataset_id, doc_ids)
            if args.wait:
                check_status(args.dataset_id, wait=True)
        return

    if args.command == "status":
        check_status(args.dataset_id, wait=args.wait)


if __name__ == "__main__":
    main()
