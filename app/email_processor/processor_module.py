import datetime
import hashlib
import json
import logging
import logging.handlers
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from contextlib import suppress
from email.errors import InvalidBase64CharactersDefect, InvalidBase64LengthDefect, MessageError

import requests
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError
from pyzmail import PyzMessage

# Config constants
DOWNLOAD_BASE_DIR = os.environ.get("DOWNLOAD_BASE_DIR", "/app/email_processor/downloads")
STATUS_DIR = os.environ.get("STATUS_DIR", "/app/email_processor/processor_state")
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# Ensure directories
os.makedirs(DOWNLOAD_BASE_DIR, exist_ok=True)
os.makedirs(STATUS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)



# SET UP LOGGING
log_file = os.path.join(LOG_DIR, "email_processor.log")


#communication with backend securely
ADD_DOCUMENT_ENDPOINT = "http://backend:5050/add-document"

MAIL_TOKEN = os.environ.get("MAIL_TOKEN", "")


MAX_ATTACHMENT_SIZE_MB = 15
RETRY_DELAY = 10
MAX_RETRY_DELAY = 300
PAUSE_DELAY = 60
TASKS_FILE = os.path.join(STATUS_DIR, "tasks.json")
MIME_ERRORS = (MessageError, ValueError, TypeError, LookupError, AttributeError, RecursionError)


# In-memory task tracking
active_tasks = {}   # task_id → thread
task_configs = {}   # task_id → (config_path, mode)
cancel_flags = {}   # task_id → threading.Event


# Logging config
logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# ---------------- Utility Functions ----------------

class RetryMail(Exception):
    """An outage or configuration issue that can be retried."""


class RejectAttachment(Exception):
    """A permanent failure for this attachment."""


MAIL_RETRY_ERRORS = (RetryMail, IMAPClientError, OSError)


def attachment_filename(filename, content_type, number, names):
    # Match the backend's ASCII-only secure_filename and preserve the extension.
    basename = unicodedata.normalize("NFKC", filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    basename = basename.strip().rstrip(". ")
    stem, dot, extension = basename.rpartition(".")
    if not dot or not re.fullmatch(r"[a-zA-Z0-9]{1,10}", extension):
        stem = basename
        extension = {"application/pdf": "pdf", "application/zip": "zip",
                     "application/x-zip-compressed": "zip"}.get(content_type, "bin")
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "_", stem)[:100].strip("._-") or f"attachment_{number}"
    extension = extension.lower()
    name = f"{stem}.{extension}"
    collision = number
    while name.lower() in names:
        suffix = f"_{collision}"
        name = f"{stem[:100 - len(suffix)]}{suffix}.{extension}"
        collision += 1
    names.add(name.lower())
    return name


def is_attachment_too_large(payload):
    return len(payload) / (1024 * 1024) > MAX_ATTACHMENT_SIZE_MB


def _write_json(path, data):
    directory = os.path.dirname(path)
    fd, pending = tempfile.mkstemp(dir=directory, prefix=".mail-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(pending, path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(pending)


def _checkpoint_key(config, *scope):
    identity = [config.get(field) for field in ("task_id", "index_name", "email", "imap_server", "imap_port")]
    return hashlib.sha256(json.dumps([*identity, *scope]).encode("ascii")).hexdigest()


def _valid_uid(value, minimum=1):
    return type(value) is int and minimum <= value <= 2**32 - 1


def _legacy_uid(email):
    # Match a filename already present in the state directory, with no lookup
    # path derived from the configured address.
    try:
        with os.scandir(STATUS_DIR) as entries:
            for entry in entries:
                if entry.name != f"{email}_uid.json" or not entry.is_file(follow_symlinks=False):
                    continue
                fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, encoding="utf-8") as f:
                    uid = json.load(f)["last_uid"]
                if not _valid_uid(uid, minimum=0):
                    raise ValueError("invalid legacy UID")
                return uid
    except (OSError, ValueError, KeyError, TypeError):
        raise RetryMail("legacy mailbox checkpoint is unreadable or invalid") from None
    return None


class MailCheckpoint:
    def __init__(self, key, legacy_email=None):
        self.key = key
        self.name = f"mail-{key}.json"
        self.path = os.path.join(STATUS_DIR, self.name)
        self.state = {"last_uid": None, "pending_uid": None, "last_part": 0, "uidvalidity": None}
        self.dirty = False
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, encoding="utf-8") as f:
                saved = json.load(f)
            self._validate(saved)
        except FileNotFoundError:
            if legacy_email is not None:
                self.state["last_uid"] = _legacy_uid(legacy_email)
                self.dirty = self.state["last_uid"] is not None
        except (OSError, ValueError):
            raise RetryMail(f"checkpoint {self.name} is unreadable or invalid") from None
        else:
            self.state = saved

    def _validate(self, data):
        if not isinstance(data, dict) or not self.state.keys() <= data.keys():
            raise ValueError("invalid checkpoint")
        last_uid, pending_uid, last_part, uidvalidity = (data[field] for field in self.state)
        valid_cursor = last_uid is None or _valid_uid(last_uid, minimum=0)
        valid_pending = pending_uid is None or (
            valid_cursor and _valid_uid(pending_uid) and pending_uid > (last_uid or 0)
        )
        valid_part = type(last_part) is int and last_part >= 0 and (pending_uid is not None or last_part == 0)
        valid_epoch = uidvalidity is None or _valid_uid(uidvalidity)
        if not all((valid_cursor, valid_pending, valid_part, valid_epoch)):
            raise ValueError("invalid checkpoint")

    def bind_uidvalidity(self, uidvalidity):
        saved = self.state["uidvalidity"]
        if saved is not None and saved != uidvalidity:
            raise RetryMail(f"checkpoint {self.name}: mailbox UIDVALIDITY is missing or changed")
        if saved is None and uidvalidity is not None:
            if not _valid_uid(uidvalidity):
                raise RetryMail("IMAP reported an invalid UIDVALIDITY")
            self.state["uidvalidity"] = uidvalidity
            if self.state["last_uid"] is not None or self.state["pending_uid"] is not None:
                self.update(uidvalidity=uidvalidity)

    def update(self, **changes):
        self.state.update(changes)
        self.dirty = True
        self.flush()

    def flush(self):
        if self.dirty:
            try:
                _write_json(self.path, self.state)
            except OSError:
                # Carry the update across retries and flush it before any more uploads.
                raise RetryMail(f"checkpoint {self.name} could not be written") from None
            self.dirty = False


def _record_rejection(config, checkpoint, uid, part, reason):
    key = checkpoint.key if checkpoint else _checkpoint_key(config, "single-message")
    uidvalidity = checkpoint.state["uidvalidity"] if checkpoint else None
    name = f"{key}-{uidvalidity or 0:d}-{uid:d}-{part or 0:d}.json"
    directory = os.path.join(STATUS_DIR, "mail-failures")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        _write_json(os.path.join(directory, name), {"uid": uid, "part": part, "reason": reason})
    except OSError:
        raise RetryMail("could not record a permanent mail failure") from None
    logger.warning("Mail task %s UID %s part %s skipped: %s", config.get("task_id"), uid, part, reason)


def connect(config):
    try:
        server, address, password = (config[field] for field in ("imap_server", "email", "password"))
        if not all(isinstance(value, str) for value in (server, address, password)):
            raise ValueError("invalid IMAP settings")
        client = IMAPClient(server, port=config["imap_port"], ssl=True, timeout=30)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RetryMail("IMAP connection settings are invalid") from None
    try:
        client.login(address, password)
    except Exception:
        with suppress(OSError, IMAPClientError):
            client.shutdown()
        raise
    return client


def upload_file_to_index(path, index_name):
    if not MAIL_TOKEN:
        raise RetryMail("MAIL_TOKEN is missing")
    if not isinstance(index_name, str) or not index_name:
        raise RetryMail("target index is missing or invalid")
    with open(path, "rb") as f:
        try:
            response = requests.post(ADD_DOCUMENT_ENDPOINT, headers={"X-Mail-Token": MAIL_TOKEN},
                                     files={"file": f}, data={"index_name": index_name},
                                     timeout=(5, 120), allow_redirects=False)
        except requests.RequestException:
            raise RetryMail("backend request failed") from None
    status = response.status_code
    if status == 202:
        return True
    invalid_file = status in {413, 415, 422}
    if status == 400:
        with suppress(ValueError):
            details = response.json()
            invalid_file = isinstance(details, dict) and details.get("code") == "invalid_file"
    if invalid_file:
        raise RejectAttachment(f"backend rejected the file (HTTP {status})")
    if status in {401, 403}:
        raise RetryMail(f"backend authorization denied (HTTP {status})")
    raise RetryMail(f"backend did not acknowledge the upload (HTTP {status})")


def _attachment_payload(part):
    try:
        payload = part.get_payload()
    except MIME_ERRORS:
        raise RejectAttachment("attachment MIME could not be decoded") from None
    if (not isinstance(payload, bytes) or not payload
            or any(isinstance(defect, (InvalidBase64CharactersDefect, InvalidBase64LengthDefect))
                   for defect in part.part.defects)):
        raise RejectAttachment("attachment MIME payload is empty or invalid")
    if is_attachment_too_large(payload):
        raise RejectAttachment(f"attachment exceeds {MAX_ATTACHMENT_SIZE_MB} MiB")
    return payload


def process_message(client, uid, config, stop_event, checkpoint=None):
    if stop_event.is_set():
        return False

    raw = client.fetch([uid], ["BODY[]"])
    if uid not in raw:
        _record_rejection(config, checkpoint, uid, None, "message is no longer available from IMAP")
        return True
    try:
        body = raw[uid][b"BODY[]"]
    except KeyError:
        raise RetryMail("IMAP did not return the message body") from None
    try:
        message = PyzMessage.factory(body)
    except MIME_ERRORS:
        _record_rejection(config, checkpoint, uid, None, "message MIME could not be parsed")
        return True
    if message.defects or not body:
        _record_rejection(config, checkpoint, uid, None, "message has malformed MIME")

    last_part = checkpoint.state["last_part"] if checkpoint else 0
    names = set()
    for number, part in enumerate(message.mailparts, start=1):
        if stop_event.is_set():
            return False
        if not (part.filename or part.disposition == "attachment"):
            continue
        name = attachment_filename(part.filename, part.type, number, names)
        if number <= last_part:
            continue
        try:
            payload = _attachment_payload(part)
            with tempfile.TemporaryDirectory(dir=DOWNLOAD_BASE_DIR) as directory:
                path = os.path.join(directory, name)
                with open(path, 'wb') as f:
                    f.write(payload)
                if stop_event.is_set():
                    return False
                if not upload_file_to_index(path, config.get("index_name")):
                    raise RetryMail("backend did not acknowledge the upload")
                if checkpoint:
                    checkpoint.update(last_part=number)
        except RejectAttachment as exc:
            _record_rejection(config, checkpoint, uid, number, str(exc))
            if checkpoint:
                checkpoint.update(last_part=number)
        except RetryMail as exc:
            raise RetryMail(f"part {number}: {exc}") from None
    return True

# ---------------- Task Control ----------------

def save_tasks():
    os.makedirs(STATUS_DIR, exist_ok=True)
    with open(TASKS_FILE, 'w') as f:
        json.dump({
            "tasks": [
                {
                    "task_id": tid,
                    "config_path": meta["config_path"],
                    "mode": meta["mode"],
                    "start_time": meta.get("start_time")
                }
                for tid, meta in task_configs.items()
            ]
        }, f, indent=2)
    logger.info("💾 Saved task state to processor_state/tasks.json")

def _start_task_thread(task_id, mode, config_path, config, start_time=None, save=False, restored=False):
    stop_event = threading.Event()
    config = {**config, "task_id": task_id}

    def run():
        logger.info(f"🎯 Starting {mode} task {task_id}")
        try:
            if mode == "monitor":
                monitor_inbox(config, stop_event, restore_legacy=restored)
            elif mode == "digitalize":
                process_full_inbox(config, stop_event)
            else:
                logger.error(f"❌ Unknown task mode: {mode}")
        except Exception as e:
            logger.exception(f"💥 Exception in {mode} task {task_id}: {e}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    active_tasks[task_id] = (thread, stop_event)
    cancel_flags[task_id] = stop_event
    task_configs[task_id] = {
        "config_path": config_path,
        "mode": mode,
        "start_time": start_time or datetime.datetime.now().isoformat()
    }

    if save:
        save_tasks()

    return task_id

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        logger.info("🕵️ No tasks.json found to load.")
        return

    try:
        with open(TASKS_FILE, 'r') as f:
            data = json.load(f)

        for entry in data.get("tasks", []):
            task_id = entry.get("task_id")
            config_path = entry.get("config_path")
            mode = entry.get("mode")
            start_time = entry.get("start_time")

            if not all([task_id, config_path, mode]):
                logger.warning(f"⚠️ Skipping invalid task entry: {entry}")
                continue

            if not os.path.exists(config_path):
                logger.warning(f"⚠️ Config file {config_path} not found for task {task_id}, skipping.")
                continue

            with open(config_path) as f:
                config = json.load(f)

            _start_task_thread(task_id, mode, config_path, config, start_time=start_time, restored=True)

        logger.info(f"✅ Loaded {len(active_tasks)} task(s) from saved state.")

    except Exception as e:
        logger.error(f"💥 Failed to load tasks: {e}")

def start_task(mode, config_path, task_id=None, save=True):
    with open(config_path) as f:
        config = json.load(f)

    task_id = task_id or str(uuid.uuid4())
    return _start_task_thread(task_id, mode, config_path, config, save=save)

def stop_task(task_id, index_name=None):
    if index_name:
        meta = task_configs.get(task_id)
        if not meta:
            raise LookupError("Email task not found for this index")
        try:
            with open(meta["config_path"], encoding="utf-8") as f:
                stored_index = json.load(f)["index_name"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LookupError("Email task not found for this index") from exc
        if stored_index != index_name:
            raise LookupError("Email task not found for this index")

    event = cancel_flags.get(task_id)
    if event:
        event.set()
        logger.info(f"🛑 Cancellation signal sent for task {task_id}")
    else:
        logger.warning(f"⚠️ No stop event found for task {task_id}")

    task_configs.pop(task_id, None)
    cancel_flags.pop(task_id, None)
    active_tasks.pop(task_id, None)
    save_tasks()
    return f"Task {task_id} stopped."

def list_tasks(index_name=None):
    tasks = []
    for tid, meta in task_configs.items():
        try:
            config_path = meta["config_path"]
            mode = meta["mode"]
            start_time = meta.get("start_time")

            with open(config_path) as f:
                config = json.load(f)

            if not index_name or config.get("index_name") == index_name:
                safe_config = dict(config)
                safe_config.pop("password", None)
                tasks.append({
                    "task_id": tid,
                    "mode": mode,
                    "start_time": start_time,
                    "config": safe_config
                })

        except Exception as e:
            logger.warning(f"⚠️ Could not load config for task {tid}: {e}")
    return tasks

# ---------------- Modes ----------------

def _retry_reason(error):
    if isinstance(error, RetryMail):
        return str(error)
    if isinstance(error, LoginError):
        return "IMAP login failed"
    if isinstance(error, IMAPClientError):
        return "IMAP operation failed"
    return "mail storage or connection unavailable"


def _run_inbox(config, stop_event, run_once):
    delay = RETRY_DELAY
    while not stop_event.is_set():
        try:
            if run_once():
                return
            delay = RETRY_DELAY
            stop_event.wait(PAUSE_DELAY)
        except MAIL_RETRY_ERRORS as exc:
            logger.warning("Mail task %s paused: %s. Retrying in %ss",
                           config.get("task_id"), _retry_reason(exc), delay)
            stop_event.wait(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)


def _process_uids(client, uids, config, stop_event, checkpoint):
    last_uid = checkpoint.state["last_uid"] or 0
    pending_uid = checkpoint.state["pending_uid"]
    uids = sorted({uid for uid in uids if uid > (pending_uid or last_uid)})
    if pending_uid is not None:
        # Try a pending UID even when it has since been expunged from search results.
        uids.insert(0, pending_uid)
    for uid in uids:
        if stop_event.is_set():
            return
        try:
            if checkpoint.state["pending_uid"] is None:
                checkpoint.update(pending_uid=uid)
            if not process_message(client, uid, config, stop_event, checkpoint):
                return
            checkpoint.update(last_uid=uid, pending_uid=None, last_part=0)
        except MAIL_RETRY_ERRORS as exc:
            raise RetryMail(f"{checkpoint.name} UID {uid}: {_retry_reason(exc)}") from None


def process_full_inbox(config, stop_event):
    checkpoints = {}

    def scan():
        for checkpoint in checkpoints.values():
            checkpoint.flush()
        with connect(config) as client:
            current_year = datetime.datetime.now().year
            for flags, _, folder in client.list_folders():
                if stop_event.is_set():
                    return True
                if b"\\NOSELECT" in (flag.upper() for flag in flags):
                    continue
                selected = client.select_folder(folder) or {}
                for year in range(current_year - 20, current_year + 1):
                    if stop_event.is_set():
                        return True
                    # Internal dates need not be in UID order, so each search window has its own cursor.
                    scope = (folder, year)
                    if scope not in checkpoints:
                        checkpoints[scope] = MailCheckpoint(_checkpoint_key(config, "digitalize", *scope))
                    checkpoint = checkpoints[scope]
                    checkpoint.bind_uidvalidity(selected.get(b"UIDVALIDITY"))
                    start = f"01-Jan-{year}"
                    end = f"01-Jan-{year + 1}"
                    uids = client.search(["SINCE", start, "BEFORE", end])
                    _process_uids(client, uids, config, stop_event, checkpoint)
        return True

    _run_inbox(config, stop_event, scan)


def monitor_inbox(config, stop_event, restore_legacy=False):
    checkpoint = None

    def poll():
        nonlocal checkpoint
        if checkpoint is None:
            legacy_email = config.get("email") if restore_legacy else None
            checkpoint = MailCheckpoint(_checkpoint_key(config, "monitor", "INBOX"), legacy_email)
            logger.info("Mail task %s uses checkpoint %s", config.get("task_id"), checkpoint.name)
        checkpoint.flush()
        with connect(config) as client:
            selected = client.select_folder("INBOX") or {}
            checkpoint.bind_uidvalidity(selected.get(b"UIDVALIDITY"))
            last_uid = checkpoint.state["last_uid"]
            if last_uid is None:
                # A new monitor starts with the latest existing message.
                uids = client.search(["ALL"])
                checkpoint.update(last_uid=max(uids) - 1 if uids else 0)
            else:
                uids = client.search(["UID", f"{last_uid + 1}:*"])
            _process_uids(client, uids, config, stop_event, checkpoint)
        return stop_event.is_set()

    _run_inbox(config, stop_event, poll)


# ✅ Automatically reload tasks on startup
load_tasks()
