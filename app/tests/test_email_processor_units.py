import contextlib
import datetime
import json
import stat
import threading
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest


MAIL_CONFIG = {"task_id": "test-job", "email": "reader@example.com", "index_name": "test_index"}


class Mailbox:
    def __init__(self, messages, uidvalidity=23):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.fetched = []

    def select_folder(self, name):
        assert name == "INBOX"
        return {b"UIDVALIDITY": self.uidvalidity}

    def search(self, criteria):
        if criteria == ["ALL"]:
            return list(self.messages)
        assert criteria[0] == "UID"
        first_uid = int(criteria[1].split(":")[0])
        return [uid for uid in self.messages if uid >= first_uid]

    def fetch(self, uids, fields):
        assert fields == ["BODY[]"]
        self.fetched.extend(uids)
        return {uid: {b"BODY[]": self.messages[uid]} for uid in uids if uid in self.messages}


class WaitRecorder(threading.Event):
    def __init__(self, stop_after):
        super().__init__()
        self.stop_after = stop_after
        self.delays = []

    def wait(self, seconds):
        self.delays.append(seconds)
        if len(self.delays) >= self.stop_after:
            self.set()
        return self.is_set()


def message(*payloads):
    mail = EmailMessage()
    mail.set_content("Synthetic message")
    for position, payload in enumerate(payloads):
        mail.add_attachment(payload, maintype="application", subtype="pdf", filename=f"doc{position}.pdf")
    return mail


def monitor_checkpoint(module, config=MAIL_CONFIG):
    key = module._checkpoint_key(config, "monitor", "INBOX")
    return json.loads((Path(module.STATUS_DIR) / f"mail-{key}.json").read_text())


def test_upload_file_to_index_accepts_queued_backend_response(email_processor_module, tmp_path, monkeypatch):
    upload_file = tmp_path / "doc.pdf"
    upload_file.write_bytes(b"%PDF-1.4\n")
    captured = {}

    def fake_post(url, *, headers, files, data, timeout, allow_redirects):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        assert files["file"].read() == b"%PDF-1.4\n"
        assert allow_redirects is False
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(email_processor_module.requests, "post", fake_post)

    assert email_processor_module.upload_file_to_index(str(upload_file), "test_index") is True
    assert captured["headers"]["X-Mail-Token"] == "mail-token"
    assert captured["data"]["index_name"] == "test_index"


@pytest.mark.parametrize("status", [200, 302, 400, 401, 403, 404, 409, 429, 503])
def test_upload_failures_that_require_retry(email_processor_module, monkeypatch, tmp_path, status):
    module = email_processor_module
    upload = tmp_path / "doc.pdf"
    upload.write_bytes(b"synthetic PDF")
    response = SimpleNamespace(status_code=status, json=lambda: {"error": "Synthetic response"})
    monkeypatch.setattr(module.requests, "post", lambda *a, **kw: response)

    with pytest.raises(module.RetryMail, match=f"HTTP {status}"):
        module.upload_file_to_index(upload, "test_index")


def test_upload_transport_failure_is_retryable(email_processor_module, monkeypatch, tmp_path):
    module = email_processor_module
    upload = tmp_path / "doc.pdf"
    upload.write_bytes(b"synthetic PDF")

    def post(*args, **kwargs):
        raise module.requests.Timeout("Synthetic response details")

    monkeypatch.setattr(module.requests, "post", post)
    with pytest.raises(module.RetryMail, match="^backend request failed$"):
        module.upload_file_to_index(upload, "test_index")


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
    module = email_processor_module
    search_calls = []
    uploaded = []
    current_year = datetime.datetime.now().year
    messages = {
        ("INBOX", f"01-Jan-{current_year - 20}"): {9: message(b"early date, higher UID").as_bytes()},
        ("INBOX", f"01-Jan-{current_year}"): {7: message(b"later date, lower UID").as_bytes()},
        ("Archive", f"01-Jan-{current_year}"): {7: message(b"same UID in another folder").as_bytes()},
    }

    class FullMailbox(Mailbox):
        def list_folders(self):
            return [((), "/", "INBOX"), ((), "/", "Archive")]

        def select_folder(self, folder_name):
            self.folder_name = folder_name
            return {b"UIDVALIDITY": 23}

        def search(self, criteria):
            search_calls.append((self.folder_name, criteria))
            self.messages = messages.get((self.folder_name, criteria[1]), {})
            return list(self.messages)

    def upload(path, index):
        uploaded.append(Path(path).read_bytes())
        return True

    monkeypatch.setattr(module, "connect", lambda config: contextlib.nullcontext(FullMailbox({})))
    monkeypatch.setattr(module, "upload_file_to_index", upload)
    module.process_full_inbox(MAIL_CONFIG, threading.Event())
    module.process_full_inbox(MAIL_CONFIG, threading.Event())

    expected_searches = [
        (folder, ["SINCE", f"01-Jan-{year}", "BEFORE", f"01-Jan-{year + 1}"])
        for folder in ("INBOX", "Archive")
        for year in range(current_year - 20, current_year + 1)
    ]
    assert search_calls == expected_searches * 2
    assert uploaded == [b"early date, higher UID", b"later date, lower UID", b"same UID in another folder"]


def test_monitor_retries_pending_uid_without_resending_accepted_parts(email_processor_module, monkeypatch):
    module = email_processor_module
    mailbox = Mailbox({6: message(b"older").as_bytes(), 7: message(b"first", b"second").as_bytes()})
    attempted = []

    def post(url, *, files, **kwargs):
        payload = files["file"].read()
        attempted.append(payload)
        if payload == b"second" and attempted.count(payload) == 1:
            mailbox.messages[8] = message(b"newer").as_bytes()
            return SimpleNamespace(status_code=503)
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(module, "connect", lambda config: contextlib.nullcontext(mailbox))
    monkeypatch.setattr(module.requests, "post", post)
    event = WaitRecorder(stop_after=2)
    module.monitor_inbox(MAIL_CONFIG, event)

    assert event.delays == [module.RETRY_DELAY, module.PAUSE_DELAY]
    assert mailbox.fetched == [7, 7, 8]
    assert attempted == [b"first", b"second", b"second", b"newer"]
    assert monitor_checkpoint(module)["last_uid"] == 8


@pytest.mark.parametrize("file_rejection", [400, 413, 415, 422])
def test_monitor_resumes_from_disk_and_records_permanent_failures(
    email_processor_module, monkeypatch, file_rejection,
):
    module = email_processor_module
    mail = message(b"first", b"blocked", b"invalid", b"last")
    mail.get_payload()[3].set_payload("a%")  # Invalid base64 in the third attachment.
    mailbox = Mailbox({7: mail.as_bytes()})
    attempted = []

    def post(url, *, files, **kwargs):
        payload = files["file"].read()
        attempted.append(payload)
        if payload == b"blocked":
            status = 403 if attempted.count(payload) == 1 else file_rejection
            return SimpleNamespace(status_code=status, json=lambda: {
                "code": "invalid_file", "error": "Synthetic private response",
            })
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(module, "connect", lambda config: contextlib.nullcontext(mailbox))
    monkeypatch.setattr(module.requests, "post", post)
    interrupted = WaitRecorder(stop_after=1)
    module.monitor_inbox(MAIL_CONFIG, interrupted)
    assert interrupted.delays == [module.RETRY_DELAY]
    assert monitor_checkpoint(module) == {"last_uid": 6, "pending_uid": 7, "last_part": 2, "uidvalidity": 23}

    mailbox.messages[8] = b"Content-Type: multipart/mixed\r\n\r\nSynthetic malformed multipart message"
    mailbox.messages[9] = message(b"newer").as_bytes()
    resumed = WaitRecorder(stop_after=1)
    module.monitor_inbox(MAIL_CONFIG, resumed, restore_legacy=True)

    assert attempted == [b"first", b"blocked", b"blocked", b"last", b"newer"]
    assert resumed.delays == [module.PAUSE_DELAY]
    assert monitor_checkpoint(module)["last_uid"] == 9
    reports = sorted((Path(module.STATUS_DIR) / "mail-failures").glob("*.json"))
    assert [json.loads(report.read_text()) for report in reports] == [
        {"uid": 7, "part": 3, "reason": f"backend rejected the file (HTTP {file_rejection})"},
        {"uid": 7, "part": 4, "reason": "attachment MIME payload is empty or invalid"},
        {"uid": 8, "part": None, "reason": "message has malformed MIME"},
    ]
    assert all(stat.S_IMODE(report.stat().st_mode) == 0o600 for report in reports)


@pytest.mark.parametrize("failed_write", ["part", "uid"])
def test_checkpoint_write_retries_are_capped_and_do_not_resend(
    email_processor_module, monkeypatch, failed_write,
):
    module = email_processor_module
    mailbox = Mailbox({7: message(b"accepted").as_bytes()})
    attempted = []
    remaining_failures = 7
    real_write = module._write_json

    def write(path, state):
        nonlocal remaining_failures
        is_target = state["last_part"] == 2 if failed_write == "part" else state["last_uid"] == 7
        if remaining_failures and is_target:
            remaining_failures -= 1
            raise OSError("Synthetic storage outage")
        real_write(path, state)

    def upload(path, index):
        attempted.append(Path(path).read_bytes())
        return True

    monkeypatch.setattr(module, "_write_json", write)
    monkeypatch.setattr(module, "connect", lambda config: contextlib.nullcontext(mailbox))
    monkeypatch.setattr(module, "upload_file_to_index", upload)
    event = WaitRecorder(stop_after=8)
    module.monitor_inbox(MAIL_CONFIG, event)

    assert event.delays == [10, 20, 40, 80, 160, 300, 300, module.PAUSE_DELAY]
    assert attempted == [b"accepted"]
    assert monitor_checkpoint(module)["last_uid"] == 7


def test_legacy_checkpoint_migrates_only_for_a_restored_monitor(email_processor_module, monkeypatch, tmp_path):
    module = email_processor_module
    mailbox = Mailbox({4: message(b"older").as_bytes(), 5: message(b"missed").as_bytes(),
                       6: message(b"latest").as_bytes()})
    legacy = Path(module.STATUS_DIR) / f"{MAIL_CONFIG['email']}_uid.json"
    legacy.write_text('{"last_uid": 4}')
    uploaded = []

    def upload(path, index):
        uploaded.append((index, Path(path).read_bytes()))
        return True

    monkeypatch.setattr(module, "connect", lambda config: contextlib.nullcontext(mailbox))
    monkeypatch.setattr(module, "upload_file_to_index", upload)
    new_config = {**MAIL_CONFIG, "task_id": "new-job", "index_name": "second_index"}
    module.monitor_inbox(new_config, WaitRecorder(stop_after=1))

    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    old_config_path = tmp_path / "old-config.json"
    old_config_path.write_text(json.dumps({k: v for k, v in MAIL_CONFIG.items() if k != "task_id"}))
    Path(module.TASKS_FILE).write_text(json.dumps({"tasks": [{
        "config_path": str(old_config_path), "task_id": MAIL_CONFIG["task_id"], "mode": "monitor",
    }]}))
    monkeypatch.setattr(module, "threading", SimpleNamespace(
        Thread=InlineThread, Event=lambda: WaitRecorder(stop_after=1),
    ))
    module.load_tasks()

    assert uploaded == [("second_index", b"latest"), ("test_index", b"missed"), ("test_index", b"latest")]
    assert monitor_checkpoint(module)["last_uid"] == 6
    assert monitor_checkpoint(module, new_config)["last_uid"] == 6
    assert legacy.read_text() == '{"last_uid": 4}'


def test_monitor_pauses_if_imap_reuses_uids(email_processor_module, monkeypatch):
    module = email_processor_module
    mailbox = Mailbox({7: message(b"first").as_bytes()})
    uploaded = []

    def upload(path, index):
        uploaded.append(Path(path).read_bytes())
        return True

    monkeypatch.setattr(module, "connect", lambda config: contextlib.nullcontext(mailbox))
    monkeypatch.setattr(module, "upload_file_to_index", upload)
    module.monitor_inbox(MAIL_CONFIG, WaitRecorder(stop_after=1))

    mailbox.uidvalidity += 1
    mailbox.messages[8] = message(b"new mailbox").as_bytes()
    event = WaitRecorder(stop_after=1)
    module.monitor_inbox(MAIL_CONFIG, event)

    assert uploaded == [b"first"]
    assert event.delays == [module.RETRY_DELAY]
    assert monitor_checkpoint(module)["last_uid"] == 7


def test_process_message_uses_bounded_unique_upload_names(email_processor_module, monkeypatch):
    module = email_processor_module
    names = []
    mail = message()
    for filename in ("Résumé.PDF", "Resume.pdf", "見積書.pdf", "見積書.pdf", "É" * 300 + ".ZIP", None):
        mail.add_attachment(b"synthetic", maintype="application", subtype="pdf", filename=filename)
    mailbox = Mailbox({7: mail.as_bytes()})

    def upload(path, index):
        names.append(Path(path).name)
        assert Path(path).read_bytes() == b"synthetic"
        return True

    monkeypatch.setattr(module, "upload_file_to_index", upload)

    assert module.process_message(mailbox, 7, MAIL_CONFIG, threading.Event()) is True
    assert len(names) == len(set(names)) == 6
    assert [Path(name).suffix for name in names] == [".pdf", ".pdf", ".pdf", ".pdf", ".zip", ".pdf"]
    assert names[:2] == ["Resume.pdf", "Resume_3.pdf"]
    assert all(name.startswith("attachment_") for name in (names[2], names[3], names[5]))
    assert all(name.isascii() and len(name) <= 111 for name in names)
    assert list(Path(module.DOWNLOAD_BASE_DIR).iterdir()) == []
