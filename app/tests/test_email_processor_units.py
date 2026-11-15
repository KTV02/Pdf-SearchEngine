import datetime
import json
import threading


def test_upload_file_to_index_accepts_queued_backend_response(email_processor_module, tmp_path, monkeypatch):
    upload_file = tmp_path / "doc.pdf"
    upload_file.write_bytes(b"%PDF-1.4\n")
    captured = {}

    class FakeResponse:
        status_code = 202
        text = "queued"

    def fake_post(url, headers=None, files=None, data=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        return FakeResponse()

    monkeypatch.setattr(email_processor_module.requests, "post", fake_post)

    assert email_processor_module.upload_file_to_index(str(upload_file), "test_index") is True
    assert captured["headers"]["X-Mail-Token"] == "mail-token"
    assert captured["data"]["index_name"] == "test_index"


def test_list_tasks_redacts_password(email_processor_module, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "email": "person@example.com",
        "password": "secret",
        "imap_server": "imap.example.com",
        "imap_port": 993,
        "index_name": "test_index",
    }))
    email_processor_module.task_configs["task-1"] = {
        "config_path": str(config_path),
        "mode": "monitor",
        "start_time": "2026-06-06T10:00:00",
    }

    tasks = email_processor_module.list_tasks("test_index")

    assert len(tasks) == 1
    assert tasks[0]["config"]["email"] == "person@example.com"
    assert "password" not in tasks[0]["config"]


def test_process_full_inbox_uses_exclusive_year_end(email_processor_module, monkeypatch):
    search_calls = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def list_folders(self):
            return [((), "/", "INBOX")]

        def select_folder(self, folder_name):
            self.folder_name = folder_name

        def search(self, criteria):
            search_calls.append(criteria)
            return []

    monkeypatch.setattr(email_processor_module, "connect", lambda config: FakeClient())

    email_processor_module.process_full_inbox({
        "email": "person@example.com",
        "password": "secret",
        "imap_server": "imap.example.com",
        "imap_port": 993,
        "index_name": "test_index",
    }, threading.Event())

    current_year = datetime.datetime.now().year
    assert len(search_calls) == 21
    assert search_calls[0] == ["SINCE", f"01-Jan-{current_year - 20}", "BEFORE", f"01-Jan-{current_year - 19}"]
    assert search_calls[-1] == ["SINCE", f"01-Jan-{current_year}", "BEFORE", f"01-Jan-{current_year + 1}"]
