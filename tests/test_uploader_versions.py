from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler_state import CrawlerState
from ragflow_kb_uploader import (
    RagflowKbUploader,
    migrate_dataset_libraries,
    upload_pending_for_source,
)


class FakeUploader:
    def __init__(self, *, complete: bool = True):
        self.complete = complete
        self.uploaded: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._status: dict[str, str] = {}

    def upload_documents_detailed(self, dataset_id, paths):
        docs = []
        for index, path in enumerate(paths):
            path_obj = Path(path)
            doc_id = f"doc-{len(self.uploaded) + index + 1}"
            self.uploaded.append(path_obj.name)
            docs.append({"id": doc_id, "name": path_obj.name})
        return docs

    def start_parsing(self, dataset_id, document_ids):
        self.started.extend(document_ids)
        for doc_id in document_ids:
            self._status[doc_id] = "DONE" if self.complete else "RUNNING"

    def stop_parsing(self, dataset_id, document_ids):
        self.stopped.extend(document_ids)
        for doc_id in document_ids:
            self._status[doc_id] = "CANCEL"

    def list_document_objects(self, dataset_id):
        return [
            {"id": doc_id, "run": status, "name": doc_id}
            for doc_id, status in self._status.items()
        ]

    def delete_documents(self, dataset_id, document_ids):
        return None


class NoUploadResponseUploader(FakeUploader):
    def __init__(self):
        super().__init__(complete=True)
        self.remote_documents: dict[str, dict[str, str]] = {}

    def upload_documents_detailed(self, dataset_id, paths):
        for path in paths:
            path_obj = Path(path)
            doc_id = f"remote-{len(self.remote_documents) + 1}"
            self.remote_documents[doc_id] = {
                "id": doc_id,
                "name": path_obj.name,
            }
            self.uploaded.append(path_obj.name)
        # 模拟上传成功，但服务端响应中没有返回文档 ID。
        return []

    def list_document_objects(self, dataset_id):
        return [
            {**doc, "run": self._status.get(doc_id, "UNSTART")}
            for doc_id, doc in self.remote_documents.items()
        ]


class FakeMigrationUploader(FakeUploader):
    def __init__(self):
        super().__init__(complete=True)
        self.datasets = {
            "old-ccdi": {"id": "old-ccdi", "name": "中纪检"},
            "old-bit": {"id": "old-bit", "name": "校纪检"},
        }
        self.deleted: list[str] = []
        self.next_dataset = 0

    def list_datasets(self):
        return list(self.datasets.values())

    def delete_datasets(self, dataset_ids):
        self.deleted.extend(dataset_ids)
        for dataset_id in dataset_ids:
            self.datasets.pop(dataset_id, None)

    def create_dataset(self, name, **kwargs):
        self.next_dataset += 1
        dataset_id = f"new-{self.next_dataset}"
        self.datasets[dataset_id] = {"id": dataset_id, "name": name}
        return dataset_id


class DirtyDatasetUploader(RagflowKbUploader):
    def __init__(self):
        super().__init__("https://ragflow.example", "test-key")
        self.datasets = {"dirty": {"id": "dirty", "name": "中纪检"}}
        self.delete_calls = 0
        self.deleted_documents: list[str] = []

    def list_datasets(self):
        return list(self.datasets.values())

    def delete_datasets(self, dataset_ids):
        self.delete_calls += 1
        if self.delete_calls == 1:
            raise RuntimeError(
                "删除知识库 失败: DocumentDoesNotExist("
                "document matching query does not exist)"
            )
        for dataset_id in dataset_ids:
            self.datasets.pop(dataset_id, None)

    def list_document_objects(self, dataset_id):
        return [
            {"id": "doc-1", "name": "旧文档"},
            {"id": "doc-2", "name": "另一旧文档"},
        ]

    def delete_documents(self, dataset_id, document_ids):
        self.deleted_documents.extend(document_ids)


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self.payload


class UploadVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = CrawlerState(self.root / "crawler_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_and_register(self, key, title, date, payload):
        path = self.root / f"{key.replace(':', '-')}_{payload}.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        self.state.register_document(
            key,
            source="ccdi",
            title=title,
            url=f"https://example/{key}",
            pdf_path=str(path),
            category_path="测试",
            publish_date=date,
        )

    def test_upload_only_latest_and_in_parse_windows(self):
        self._write_and_register("ccdi:1", "测试条例（2010年修改）", "2010", "a")
        self._write_and_register("ccdi:2", "测试条例", "2024-01-01", "b")
        self._write_and_register("ccdi:3", "另一规定", "2022-02-02", "c")
        fake = FakeUploader(complete=True)
        uploaded = upload_pending_for_source(
            "ccdi",
            self.state,
            fake,
            "dataset-new",
            min_age_days=0,
            max_age_days=999,
            immediate=True,
            max_parsing_documents=1,
            parse_timeout_seconds=1,
            parse_poll_seconds=1,
            parse_retry_rounds=0,
            upload_batch_size=1,
        )
        self.assertEqual(uploaded, 2)
        self.assertEqual(len(fake.started), 2)
        self.assertNotIn("ccdi-1_a.pdf", fake.uploaded)
        snap = self.state.snapshot()
        self.assertTrue(snap["documents"]["ccdi:2"]["uploaded_at"])
        self.assertEqual(
            snap["documents"]["ccdi:2"]["ragflow_document_id"], "doc-1"
            if fake.started[0] == "doc-1" else snap["documents"]["ccdi:2"]["ragflow_document_id"]
        )

    def test_timeout_cancels_and_queues_retry(self):
        self._write_and_register("ccdi:9", "超时法规", "2023-03-03", "timeout")
        fake = FakeUploader(complete=False)
        upload_pending_for_source(
            "ccdi",
            self.state,
            fake,
            "dataset-new",
            min_age_days=0,
            max_age_days=999,
            immediate=True,
            max_parsing_documents=1,
            parse_timeout_seconds=1,
            parse_poll_seconds=1,
            parse_retry_rounds=0,
        )
        queue = self.state.get_parse_queue()
        self.assertEqual(len(fake.stopped), 1)
        self.assertEqual(len(queue["retry"]), 1)
        self.assertEqual(queue["retry"][0]["doc_key"], "ccdi:9")

    def test_upload_without_response_ids_falls_back_to_library_listing(self):
        self._write_and_register("ccdi:8", "无响应编号法规", "2024-04-05", "noid")
        fake = NoUploadResponseUploader()

        uploaded = upload_pending_for_source(
            "ccdi",
            self.state,
            fake,
            "dataset-new",
            min_age_days=0,
            max_age_days=999,
            immediate=True,
            max_parsing_documents=10,
            parse_timeout_seconds=1,
            parse_poll_seconds=1,
            parse_retry_rounds=0,
        )

        self.assertEqual(uploaded, 1)
        self.assertEqual(fake.started, ["remote-1"])
        meta = self.state.snapshot()["documents"]["ccdi:8"]
        self.assertEqual(meta["ragflow_document_id"], "remote-1")
        self.assertEqual(meta["parse_status"], "DONE")

    def test_migration_rebuilds_and_updates_upload_fields(self):
        self._write_and_register("ccdi:10", "迁库测试", "2025-05-06", "migrate")
        fake = FakeMigrationUploader()
        config = {
            "ragflow": {
                "dataset_name_ccdi": "中纪检",
                "dataset_name_bit": "校纪检",
                "dataset_id_ccdi": "old-ccdi",
                "dataset_id_bit": "old-bit",
                "chunk_method": "laws",
                "chunk_token_num": 512,
            },
            "upload": {
                "max_parsing_documents": 500,
                "parse_timeout_minutes": 1,
                "parse_poll_seconds": 1,
                "parse_retry_rounds": 0,
                "upload_batch_size": 1,
            },
        }
        persisted: list[tuple[str, str]] = []
        result = migrate_dataset_libraries(
            self.state,
            config,
            fake,
            dataset_created_callback=lambda a, b: persisted.append((a, b)),
        )
        self.assertEqual(set(fake.deleted), {"old-ccdi", "old-bit"})
        self.assertEqual(persisted, [("new-1", "new-2")])
        self.assertEqual(fake.started, ["doc-1"])
        doc = self.state.snapshot()["documents"]["ccdi:10"]
        self.assertEqual(doc["upload_dataset_id"], result["dataset_id_ccdi"])
        self.assertTrue(doc["uploaded_at"])

    def test_dirty_dataset_document_records_are_cleaned_before_retry(self):
        uploader = DirtyDatasetUploader()

        result = uploader.delete_dataset_safely("dirty")

        self.assertTrue(result["recovered"])
        self.assertEqual(result["documents_deleted"], 2)
        self.assertEqual(uploader.deleted_documents, ["doc-1", "doc-2"])
        self.assertNotIn("dirty", uploader.datasets)
        self.assertEqual(uploader.delete_calls, 2)

    def test_list_datasets_retries_transient_2003_error(self):
        uploader = RagflowKbUploader("https://ragflow.example", "test-key")
        responses = [
            JsonResponse({"code": 2003, "message": "2003"}),
            JsonResponse({"code": 0, "data": []}),
        ]

        with patch(
            "ragflow_kb_uploader.requests.get", side_effect=responses
        ) as request_mock, patch("ragflow_kb_uploader.time.sleep"):
            result = uploader.list_datasets()

        self.assertEqual(result, [])
        self.assertEqual(request_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
