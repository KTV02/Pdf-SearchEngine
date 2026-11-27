import os
import tempfile
import json
import logging
import datetime
import re
import threading
import uuid
import requests
import logging.handlers

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

MAIL_TOKEN = os.environ.get("MAIL_TOKEN")

headers = {
    "X-Mail-Token": MAIL_TOKEN
}


MAX_ATTACHMENT_SIZE_MB = 15
RETRY_DELAY = 10
PAUSE_DELAY = 60
TASKS_FILE = os.path.join(STATUS_DIR, "tasks.json")


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

def sanitize_filename(filename):
    return re.sub(r'[\\/*?:"<>|\r\n]+', "_", filename).strip()

def is_attachment_too_large(payload):
    return len(payload) / (1024 * 1024) > MAX_ATTACHMENT_SIZE_MB

def save_last_uid(email, uid):
    path = os.path.join(STATUS_DIR, f"{email}_uid.json")
    fd, pending = tempfile.mkstemp(dir=STATUS_DIR, prefix=".uid-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"last_uid": uid}, f)
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)

def load_last_uid(email):
    path = os.path.join(STATUS_DIR, f"{email}_uid.json")
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)["last_uid"]
    return None

def connect(config):
    from imapclient import IMAPClient

    logger.info(f"🔐 Connecting to IMAP server {config['imap_server']} as {config['email']}")
    client = IMAPClient(config['imap_server'], port=config['imap_port'], ssl=True)
    client.login(config['email'], config['password'])
    return client

def upload_file_to_index(path, index_name):
    try:
        with open(path, 'rb') as f:
            res = requests.post(ADD_DOCUMENT_ENDPOINT, headers=headers, files={"file": f},
                                data={"index_name": index_name}, timeout=(5, 120))
        logger.info(f"📤 Upload response: {res.status_code} {res.text}")
        return res.status_code in [200, 202, 409]
    except Exception as e:
        logger.error(f"❌ Upload failed for {path}: {e}")
        return False

def process_message(client, uid, config, stop_event):
    if stop_event.is_set():
        return False

    logger.info(f"📧 Fetching message UID {uid}")
    try:
        from pyzmail import PyzMessage

        raw = client.fetch([uid], ['BODY[]'])
        msg = PyzMessage.factory(raw[uid][b'BODY[]'])

        subject = msg.get_subject()
        logger.info(f"   ↳ Subject: {subject}")

        for part in msg.mailparts:
            if part.filename:
                payload = part.get_payload()
                if is_attachment_too_large(payload):
                    logger.warning(f"🚫 Skipping large attachment: {part.filename}")
                    continue

                directory = os.path.join(DOWNLOAD_BASE_DIR, sanitize_filename(config["email"]))
                os.makedirs(directory, exist_ok=True)
                path = os.path.join(directory, sanitize_filename(part.filename))
                with open(path, 'wb') as f:
                    f.write(payload)

                if not upload_file_to_index(path, config["index_name"]):
                    logger.warning("Attachment was not accepted for UID %s", uid)
                    return False
                logger.info(f"✅ Uploaded {path}")
        return True
    except Exception as e:
        logger.error(f"💥 Failed to process UID {uid}: {e}")
        return False

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

def _start_task_thread(task_id, mode, config_path, config, start_time=None, save=False):
    stop_event = threading.Event()

    def run():
        logger.info(f"🎯 Starting {mode} task {task_id}")
        try:
            if mode == "monitor":
                monitor_inbox(config, stop_event)
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

            _start_task_thread(task_id, mode, config_path, config, start_time=start_time, save=False)

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

def process_full_inbox(config, stop_event):
    try:
        with connect(config) as client:
            folders = client.list_folders()
            logger.info(f"📁 Found folders: {[f[2] for f in folders]}")
            for folder in folders:
                if stop_event.is_set():
                    logger.info("🛑 Full inbox scan cancelled.")
                    return
                folder_name = folder[2]
                client.select_folder(folder_name)
                for year in range(datetime.datetime.now().year - 20, datetime.datetime.now().year + 1):
                    start = f"01-Jan-{year}"
                    end = f"01-Jan-{year + 1}"
                    uids = client.search(['SINCE', start, 'BEFORE', end])
                    logger.info(f"🔍 {folder_name} {start} to {end}: {len(uids)} emails")
                    for uid in uids:
                        if stop_event.is_set():
                            logger.info("🛑 Task cancelled during full inbox processing.")
                            return
                        process_message(client, uid, config, stop_event)
    except Exception as e:
        logger.error(f"💥 Error in full inbox processing: {e}", exc_info=True)

def monitor_inbox(config, stop_event):
    email = config["email"]
    last_uid = load_last_uid(email)

    while not stop_event.is_set():
        try:
            with connect(config) as client:
                client.select_folder("INBOX")
                if last_uid is None:
                    # A new monitor starts with the latest existing message. Persist
                    # the starting point so an upload failure cannot move it forward.
                    uids = client.search(["ALL"])
                    last_uid = max(uids) - 1 if uids else 0
                    save_last_uid(email, last_uid)
                else:
                    uids = client.search(["UID", f"{last_uid + 1}:*"])

                for uid in sorted(uid for uid in uids if uid > last_uid):
                    if stop_event.is_set():
                        return
                    if not process_message(client, uid, config, stop_event):
                        raise RuntimeError(f"Mail import paused at UID {uid}")
                    save_last_uid(email, uid)
                    last_uid = uid
            stop_event.wait(PAUSE_DELAY)
        except Exception as e:
            logger.warning(f"🔄 Error during monitoring: {e}")
            stop_event.wait(RETRY_DELAY)


# ✅ Automatically reload tasks on startup
load_tasks()
