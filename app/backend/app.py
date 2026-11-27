# pdfsearcheninge core that hosts backend and frontend for digitalizing pdfs from different sources
from flask import Flask, request, jsonify, send_file, Response, send_from_directory, stream_with_context,g
from whoosh import index
from whoosh.fields import Schema, TEXT, ID
from whoosh.query import Every
from whoosh.query import Or, FuzzyTerm, Term
from whoosh.qparser import MultifieldParser
from whoosh.analysis import StemmingAnalyzer
from PyPDF2 import PdfReader, PdfWriter
from pdf2image import convert_from_path
import os
import tempfile
from werkzeug.utils import secure_filename
import hashlib
import hmac
import json
import shutil
import zipfile
import re
import logging
from contextlib import contextmanager
from functools import lru_cache, wraps
from werkzeug.exceptions import HTTPException
from werkzeug.wsgi import ClosingIterator
from datetime import datetime, timedelta
from werkzeug.datastructures import MultiDict
import subprocess
from convert_folder import convert_pdf_to_searchable
import uuid
import img2pdf
from PIL import Image
import socket
import fcntl
import struct
import base64
from urllib.parse import quote, unquote, urlencode
from pathlib import Path, PurePosixPath
from markupsafe import escape
import requests
import threading
import math
from collections import Counter
from task_queue import TaskQueueManager
from job_coordinator import NodeRedJobCoordinator
from request_identity import (
    RUNNING_IN_DOCKER,
    browser_identity,
    configure_request_identity,
    has_proxy_headers,
    normalize_login,
)
from flask import has_request_context
import clamd
import time

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart






# setup------------------------------------------------------------------------------
# Flask App Initialization
app = Flask(__name__, static_folder="frontend")
from browser_security import install_browser_security
install_browser_security(app)
configure_request_identity(app)



#FILEPATHS-----------------
STORAGE_CONFIG_PATH = os.environ.get("STORAGE_CONFIG_PATH", "/app/index_folder/storage_config.json")


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_startup_storage_config():
    try:
        if os.path.exists(STORAGE_CONFIG_PATH):
            with open(STORAGE_CONFIG_PATH, "r") as f:
                raw_config = json.load(f)
            if isinstance(raw_config, dict):
                return raw_config
    except Exception:
        # Logging is configured below; keep startup config fallback quiet here.
        pass
    return {}


STARTUP_STORAGE_CONFIG = load_startup_storage_config()
STARTUP_STORAGE_PATHS = STARTUP_STORAGE_CONFIG.get("storage", {}) if isinstance(STARTUP_STORAGE_CONFIG.get("storage"), dict) else {}


def configured_storage_path(key, env_name, default):
    return os.environ.get(env_name) or STARTUP_STORAGE_PATHS.get(key) or default


# Define custom storage folders. Environment variables override storage_config.json.
TMP_FOLDER = configured_storage_path("tmp_folder", "TMP_FOLDER", "/app/tmp")
LOG_FOLDER = configured_storage_path("log_folder", "LOG_FOLDER", "/app/logs")  # Container path (mounted from host)
PDF_FOLDER = configured_storage_path("pdf_folder", "PDF_FOLDER", "/app/ocr/converted")
DELETED_FOLDER = configured_storage_path("deleted_folder", "DELETED_FOLDER", "/app/ocr/deleted")
INDEX_FOLDER = configured_storage_path("index_folder", "INDEX_FOLDER", "/app/index_folder")
EMAIL_CONFIGS = configured_storage_path("email_configs", "EMAIL_CONFIGS", "/app/email_configs")
UPLOAD_FOLDER = os.path.join(PDF_FOLDER, "uploads")
HASH_INDEX_PATH = os.path.join(INDEX_FOLDER, "hash_index.json")
BACKUP_TARGET_FOLDER = os.environ.get(
    "BACKUP_TARGET_FOLDER",
    (STARTUP_STORAGE_CONFIG.get("backup") or {}).get("target_folder", "/app/backups")
)

MAIL_PROCESSOR_URL = os.environ.get("MAIL_PROCESSOR_URL", "http://mail-processor:5000")  # Docker container name




# Keep the legacy roles file available to administrators and backups.
ROLE_CONFIG_PATH = os.environ.get("ROLE_CONFIG_PATH", "/app/backend/roles.json")
ACCESS_CONFIG_PATH = os.environ.get("ACCESS_CONFIG_PATH", "/app/backend/access_config.json")
access_config_lock = threading.Lock()

#---------------------------------------------------filepathsDONE


# Ensure Directories Exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TMP_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)
os.makedirs(DELETED_FOLDER, exist_ok=True)
os.makedirs(INDEX_FOLDER, exist_ok=True)
os.makedirs(EMAIL_CONFIGS, exist_ok=True)

# CLEAN EXISTING LOG HANDLERS (to prevent duplicates)
logging.root.handlers.clear()


# SET UP LOGGING
log_file = os.path.join(LOG_FOLDER, "server.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]",
    handlers=[
        logging.StreamHandler(),  # Logs to console
        logging.FileHandler(log_file, mode="a"),  # Logs to file
    ]
)

logger = logging.getLogger(__name__)
logger.info("Logging system initialized.")

NOISY_WERKZEUG_ACCESS_PATTERNS = (
    '"GET /task-status/',
    '"GET /tasks',
    '"GET /job-coordinator/status',
    '"GET /health',
)


class NoisyWerkzeugEndpointFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return not any(pattern in message for pattern in NOISY_WERKZEUG_ACCESS_PATTERNS)


logging.getLogger("werkzeug").addFilter(NoisyWerkzeugEndpointFilter())
job_coordinator = NodeRedJobCoordinator.from_env(logger=logger)
JOB_COORDINATOR_STATE_PATH = os.environ.get(
    "JOB_COORDINATOR_STATE_PATH",
    os.path.join(INDEX_FOLDER, "job_coordinator_state.json")
)
coordinator_state_lock = threading.Lock()

#custom exception
class DuplicateDocumentError(Exception):
    def __init__(self, message):
        super().__init__(message)


class CoordinatorPauseRequested(Exception):
    def __init__(self, status):
        self.status = status or {}
        self.coordinator_pause_requested = True
        reason = self.status.get("cancel_reason") or self.status.get("reason") or "Coordinator requested pause"
        super().__init__(reason)


#For scheduling lock tasks like add document or delete (need exclusive write access)
task_queue = TaskQueueManager()
# Simple task status tracking
task_status = {}  # task_id → {status, message, result}
request_client_lock = threading.Lock()
request_clients = {}


# setupdone-------------------------------------------------------------------------------------

MAIL_TOKEN = os.environ.get("MAIL_TOKEN", "")
PUBLIC_MAIL_TOKENS = {"", "default_mail_token", "local-mail-token", "replace-with-a-private-random-value"}


def get_env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"Invalid integer for {name}: {value!r}; using {default}")
        return default


def get_env_float(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"Invalid number for {name}: {value!r}; using {default}")
        return default


CLAMAV_HOST = os.environ.get("CLAMAV_HOST", "clamav")
CLAMAV_PORT = get_env_int("CLAMAV_PORT", 3310)
CLAMAV_WAIT_SECONDS = get_env_int("CLAMAV_WAIT_SECONDS", 300)
CLAMAV_RETRY_SECONDS = get_env_float("CLAMAV_RETRY_SECONDS", 2.0)
REQUEST_CLIENT_IDLE_SECONDS = max(30, get_env_int("REQUEST_CLIENT_IDLE_SECONDS", 180))
LOW_DATA_PREVIEW_DPI = get_env_int("LOW_DATA_PREVIEW_DPI", 55)
LOW_DATA_PREVIEW_JPEG_QUALITY = get_env_int("LOW_DATA_PREVIEW_JPEG_QUALITY", 30)
PREVIEW_REQUESTS = threading.BoundedSemaphore(12)
PDF_PAGE_WORK = threading.BoundedSemaphore(2)
LOW_DATA_RENDER_LOCK = threading.Lock()
DOCUMENT_PATH_CACHE_LOCK = threading.Lock()
LOW_DATA_PREVIEW_CACHE_FOLDER = os.path.join(TMP_FOLDER, "preview-cache")
os.makedirs(LOW_DATA_PREVIEW_CACHE_FOLDER, exist_ok=True)

INDEX_CONFIG_FILENAME = "index_config.json"
DOCUMENT_METADATA_FILENAME = "document_metadata.json"
VALID_INDEX_MODES = {"standalone", "cloud", "hybrid"}
UPLOADS_VIRTUAL_FOLDER = "Uploaded documents"
CLOUD_INTERNAL_FOLDER = ".pdfsearchengine"
DEFAULT_CLOUD_PROVIDER = "mounted_folder"
ONEDRIVE_CLOUD_PROVIDER = "onedrive"
GOOGLE_DRIVE_CLOUD_PROVIDER = "google_drive"
DROPBOX_CLOUD_PROVIDER = "dropbox"
CLOUD_INDEX_JOB_TYPE = "cloud_index"
VALID_CLOUD_PROVIDERS = {
    DEFAULT_CLOUD_PROVIDER,
    ONEDRIVE_CLOUD_PROVIDER,
    GOOGLE_DRIVE_CLOUD_PROVIDER,
    DROPBOX_CLOUD_PROVIDER,
}
DEFAULT_CLOUD_SYNC_INTERVAL_SECONDS = get_env_int("CLOUD_SYNC_INTERVAL_SECONDS", 300)
MIN_CLOUD_SYNC_INTERVAL_SECONDS = 30
CLOUD_SYNC_SCHEDULER_POLL_SECONDS = get_env_int("CLOUD_SYNC_SCHEDULER_POLL_SECONDS", 30)
MANAGED_CLOUD_FOLDER = os.environ.get("MANAGED_CLOUD_FOLDER", "/app/cloud/managed")
CLOUD_CONNECTIONS_PATH = os.environ.get(
    "CLOUD_CONNECTIONS_PATH",
    os.path.join(INDEX_FOLDER, "cloud_connections.json"),
)
CLOUD_PROVIDER_CONFIG_PATH = os.environ.get(
    "CLOUD_PROVIDER_CONFIG_PATH",
    os.path.join(INDEX_FOLDER, "cloud_provider_config.json"),
)
MICROSOFT_GRAPH_CLIENT_ID = os.environ.get("MICROSOFT_GRAPH_CLIENT_ID") or os.environ.get("ONEDRIVE_CLIENT_ID", "")
MICROSOFT_GRAPH_TENANT = os.environ.get("MICROSOFT_GRAPH_TENANT", "common")
MICROSOFT_GRAPH_SCOPES = os.environ.get("MICROSOFT_GRAPH_SCOPES", "offline_access User.Read Files.ReadWrite")
MICROSOFT_LOGIN_BASE_URL = "https://login.microsoftonline.com"
MICROSOFT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MICROSOFT_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MICROSOFT_GRAPH_SIMPLE_UPLOAD_LIMIT = 250 * 1024 * 1024
MICROSOFT_GRAPH_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024
MICROSOFT_GRAPH_REQUEST_TIMEOUT_SECONDS = get_env_int("MICROSOFT_GRAPH_REQUEST_TIMEOUT_SECONDS", 60)
GOOGLE_DRIVE_CLIENT_ID = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "")
GOOGLE_DRIVE_CLIENT_SECRET = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")
GOOGLE_DRIVE_SCOPES = os.environ.get(
    "GOOGLE_DRIVE_SCOPES",
    "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile",
)
GOOGLE_DRIVE_REDIRECT_URI = os.environ.get("GOOGLE_DRIVE_REDIRECT_URI", "")
GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_DRIVE_BASE_URL = "https://www.googleapis.com/drive/v3"
GOOGLE_DRIVE_UPLOAD_BASE_URL = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_DRIVE_REQUEST_TIMEOUT_SECONDS = get_env_int("GOOGLE_DRIVE_REQUEST_TIMEOUT_SECONDS", 60)
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY", "")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET", "")
DROPBOX_SCOPES = os.environ.get(
    "DROPBOX_SCOPES",
    "account_info.read files.metadata.read files.content.read files.content.write",
)
DROPBOX_REDIRECT_URI = os.environ.get("DROPBOX_REDIRECT_URI", "")
DROPBOX_AUTH_URL = "https://www.dropbox.com/oauth2/authorize"
DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
DROPBOX_API_BASE_URL = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT_BASE_URL = "https://content.dropboxapi.com/2"
DROPBOX_REQUEST_TIMEOUT_SECONDS = get_env_int("DROPBOX_REQUEST_TIMEOUT_SECONDS", 60)
DROPBOX_SIMPLE_UPLOAD_LIMIT = 150 * 1024 * 1024
DROPBOX_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
os.makedirs(MANAGED_CLOUD_FOLDER, exist_ok=True)


def validate_index_name(index_name):
    if not isinstance(index_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", index_name):
        raise ValueError("Invalid index name")
    return index_name


def utc_now_iso():
    return datetime.utcnow().isoformat() + "Z"


def path_within(root_path, candidate_path):
    if not root_path or not candidate_path:
        return False
    try:
        root_real = os.path.realpath(os.path.normpath(root_path))
        candidate_real = os.path.realpath(os.path.normpath(candidate_path))
        return os.path.commonpath([root_real, candidate_real]) == root_real
    except (OSError, ValueError):
        return False


def normalize_virtual_path(path_value):
    parts = []
    for part in str(path_value or "").replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        parts.append(part)
    return "/".join(parts)


def get_index_folder_path(index_name):
    return os.path.join(INDEX_FOLDER, validate_index_name(index_name))


def get_index_pdf_folder(index_name):
    return os.path.join(PDF_FOLDER, validate_index_name(index_name))


def get_index_config_path(index_name):
    return os.path.join(get_index_folder_path(index_name), INDEX_CONFIG_FILENAME)


def get_document_metadata_path(index_name):
    return os.path.join(get_index_folder_path(index_name), DOCUMENT_METADATA_FILENAME)


def default_index_config(index_name):
    return {
        "version": 1,
        "index_name": validate_index_name(index_name),
        "mode": "standalone",
        "cloud": {
            "provider": DEFAULT_CLOUD_PROVIDER,
            "root_path": "",
            "connection_id": "",
            "remote_path": "",
            "managed_root_path": "",
            "display_root_name": "",
            "preview_folder": "",
            "sync_enabled": True,
            "sync_interval_seconds": DEFAULT_CLOUD_SYNC_INTERVAL_SECONDS,
            "delete_standalone_duplicates": True,
            "last_sync_at": None,
            "last_sync_status": None,
            "last_sync_message": None,
            "last_sync_task_id": None,
        },
        "created_at": None,
        "updated_at": None,
    }


def normalize_cloud_config(raw_config=None, existing_config=None):
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    existing_config = existing_config if isinstance(existing_config, dict) else {}
    defaults = default_index_config("default")["cloud"]
    merged = dict(defaults)
    merged.update(existing_config)
    merged.update(raw_config)

    provider = str(merged.get("provider") or DEFAULT_CLOUD_PROVIDER).strip().lower()
    if provider not in VALID_CLOUD_PROVIDERS:
        provider = DEFAULT_CLOUD_PROVIDER

    return {
        "provider": provider,
        "root_path": str(merged.get("root_path") or "").strip(),
        "connection_id": str(merged.get("connection_id") or "").strip(),
        "remote_path": normalize_virtual_path(merged.get("remote_path") or ""),
        "managed_root_path": str(merged.get("managed_root_path") or "").strip(),
        "display_root_name": str(merged.get("display_root_name") or "").strip(),
        "preview_folder": str(merged.get("preview_folder") or "").strip(),
        "sync_enabled": parse_bool(merged.get("sync_enabled"), True),
        "sync_interval_seconds": max(
            MIN_CLOUD_SYNC_INTERVAL_SECONDS,
            coerce_positive_int(
                merged.get("sync_interval_seconds"),
                DEFAULT_CLOUD_SYNC_INTERVAL_SECONDS,
            ),
        ),
        "delete_standalone_duplicates": parse_bool(
            merged.get("delete_standalone_duplicates"),
            True,
        ),
        "last_sync_at": merged.get("last_sync_at"),
        "last_sync_status": merged.get("last_sync_status"),
        "last_sync_message": merged.get("last_sync_message"),
        "last_sync_task_id": merged.get("last_sync_task_id"),
    }


def normalize_index_config(index_name, raw_config=None, existing_config=None):
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    existing_config = existing_config if isinstance(existing_config, dict) else {}
    defaults = default_index_config(index_name)
    merged = dict(defaults)
    merged.update(existing_config)
    merged.update(raw_config)

    mode = str(merged.get("mode") or "standalone").strip().lower()
    if mode not in VALID_INDEX_MODES:
        mode = "standalone"

    cloud_existing = existing_config.get("cloud") if isinstance(existing_config.get("cloud"), dict) else {}
    cloud_raw = raw_config.get("cloud") if isinstance(raw_config.get("cloud"), dict) else {}

    return {
        "version": 1,
        "index_name": validate_index_name(index_name),
        "mode": mode,
        "cloud": normalize_cloud_config(cloud_raw, cloud_existing),
        "created_at": merged.get("created_at"),
        "updated_at": merged.get("updated_at"),
    }


def load_index_config(index_name):
    validate_index_name(index_name)
    config_path = get_index_config_path(index_name)
    raw_config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                raw_config = json.load(f)
        except Exception:
            logger.warning("Unable to read index config for %s", index_name, exc_info=True)
    return normalize_index_config(index_name, raw_config)


def cloud_connection_store_default():
    return {
        "version": 1,
        "connections": {},
    }


def normalize_cloud_connection(raw_connection):
    if not isinstance(raw_connection, dict):
        return None

    connection_id = str(raw_connection.get("id") or "").strip()
    provider = str(raw_connection.get("provider") or "").strip().lower()
    if not connection_id or provider not in VALID_CLOUD_PROVIDERS:
        return None

    token = raw_connection.get("token") if isinstance(raw_connection.get("token"), dict) else {}
    account = raw_connection.get("account") if isinstance(raw_connection.get("account"), dict) else {}
    provider_config = raw_connection.get("provider_config") if isinstance(raw_connection.get("provider_config"), dict) else {}
    owner = raw_connection.get("owner") if isinstance(raw_connection.get("owner"), dict) else {}
    return {
        "id": connection_id,
        "provider": provider,
        "display_name": str(raw_connection.get("display_name") or "").strip(),
        "status": str(raw_connection.get("status") or "connected").strip().lower(),
        "created_at": raw_connection.get("created_at"),
        "updated_at": raw_connection.get("updated_at"),
        "owner": {
            "type": str(owner.get("type") or raw_connection.get("owner_type") or "").strip(),
            "id": str(owner.get("id") or raw_connection.get("owner_id") or "").strip(),
            "login": normalize_login(owner.get("login") or raw_connection.get("owner_login")),
        },
        "account": account,
        "token": token,
        "provider_config": provider_config,
        "last_error": raw_connection.get("last_error"),
    }


cloud_connections_lock = threading.Lock()
cloud_auth_sessions_lock = threading.Lock()
cloud_auth_sessions = {}


def load_cloud_connections():
    if not os.path.exists(CLOUD_CONNECTIONS_PATH):
        return cloud_connection_store_default()

    try:
        with open(CLOUD_CONNECTIONS_PATH, "r") as f:
            raw_payload = json.load(f)
    except Exception:
        logger.warning("Unable to read cloud connection store", exc_info=True)
        return cloud_connection_store_default()

    raw_connections = raw_payload.get("connections") if isinstance(raw_payload, dict) else {}
    if not isinstance(raw_connections, dict):
        raw_connections = {}

    connections = {}
    for connection_id, raw_connection in raw_connections.items():
        if isinstance(raw_connection, dict):
            raw_connection = dict(raw_connection)
            raw_connection.setdefault("id", connection_id)
        normalized = normalize_cloud_connection(raw_connection)
        if normalized:
            connections[normalized["id"]] = normalized

    return {
        "version": 1,
        "connections": connections,
    }


def save_cloud_connections(payload):
    payload = payload if isinstance(payload, dict) else cloud_connection_store_default()
    normalized = cloud_connection_store_default()
    for connection_id, raw_connection in (payload.get("connections") or {}).items():
        if isinstance(raw_connection, dict):
            raw_connection = dict(raw_connection)
            raw_connection.setdefault("id", connection_id)
        connection = normalize_cloud_connection(raw_connection)
        if connection:
            normalized["connections"][connection["id"]] = connection

    os.makedirs(os.path.dirname(CLOUD_CONNECTIONS_PATH), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".cloud_connections_",
        suffix=".json",
        dir=os.path.dirname(CLOUD_CONNECTIONS_PATH),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(normalized, f, indent=4)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            logger.debug("Unable to restrict cloud connection temp file permissions", exc_info=True)
        os.replace(temp_path, CLOUD_CONNECTIONS_PATH)
        try:
            os.chmod(CLOUD_CONNECTIONS_PATH, 0o600)
        except OSError:
            logger.debug("Unable to restrict cloud connection file permissions", exc_info=True)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return normalized


def get_cloud_connection(connection_id):
    connection_id = str(connection_id or "").strip()
    if not connection_id:
        return None

    with cloud_connections_lock:
        return load_cloud_connections().get("connections", {}).get(connection_id)


def upsert_cloud_connection(connection):
    normalized = normalize_cloud_connection(connection)
    if not normalized:
        raise ValueError("Invalid cloud connection")

    now = utc_now_iso()
    normalized["created_at"] = normalized.get("created_at") or now
    normalized["updated_at"] = now

    with cloud_connections_lock:
        payload = load_cloud_connections()
        payload.setdefault("connections", {})[normalized["id"]] = normalized
        save_cloud_connections(payload)

    return normalized


def remove_cloud_connection(connection_id):
    connection_id = str(connection_id or "").strip()
    if not connection_id:
        return False

    with cloud_connections_lock:
        payload = load_cloud_connections()
        removed = payload.setdefault("connections", {}).pop(connection_id, None)
        if removed:
            save_cloud_connections(payload)
            return True
    return False


def sanitize_cloud_connection(connection):
    if not connection:
        return None

    token = connection.get("token") if isinstance(connection.get("token"), dict) else {}
    account = connection.get("account") if isinstance(connection.get("account"), dict) else {}
    return {
        "id": connection.get("id"),
        "provider": connection.get("provider"),
        "display_name": connection.get("display_name") or account.get("displayName") or account.get("userPrincipalName") or "Cloud account",
        "status": connection.get("status"),
        "created_at": connection.get("created_at"),
        "updated_at": connection.get("updated_at"),
        "expires_at": token.get("expires_at"),
        "scope": token.get("scope"),
        "account": {
            "id": account.get("id"),
            "displayName": account.get("displayName"),
            "mail": account.get("mail"),
            "userPrincipalName": account.get("userPrincipalName"),
        },
        "owner": {
            "type": (connection.get("owner") or {}).get("type"),
            "id": (connection.get("owner") or {}).get("id"),
            "login": (connection.get("owner") or {}).get("login"),
        },
        "last_error": connection.get("last_error"),
    }


def sanitize_cloud_auth_session(session):
    if not session:
        return None

    public_keys = (
        "id",
        "provider",
        "display_name",
        "status",
        "created_at",
        "updated_at",
        "expires_at",
        "verification_uri",
        "verification_uri_complete",
        "authorization_url",
        "callback_url",
        "user_code",
        "message",
        "interval",
        "last_error",
    )
    payload = {key: session.get(key) for key in public_keys if key in session}
    if session.get("connection"):
        payload["connection"] = sanitize_cloud_connection(session["connection"])
    return payload


def list_cloud_connections_public():
    with cloud_connections_lock:
        connections = load_cloud_connections().get("connections", {})
    return [
        sanitize_cloud_connection(connection)
        for connection in sorted(
            connections.values(),
            key=lambda item: ((item.get("provider") or ""), (item.get("display_name") or ""), (item.get("created_at") or "")),
        )
    ]


def provider_display_name(provider):
    return {
        DEFAULT_CLOUD_PROVIDER: "Mounted or synced folder",
        ONEDRIVE_CLOUD_PROVIDER: "OneDrive",
        GOOGLE_DRIVE_CLOUD_PROVIDER: "Google Drive",
        DROPBOX_CLOUD_PROVIDER: "Dropbox",
    }.get(provider, "Cloud account")


def current_principal_owner():
    principal = get_current_principal()
    return {
        "type": principal.get("type"),
        "id": principal.get("id"),
        "login": principal.get("login"),
    }


def owner_matches_principal(owner, principal=None):
    principal = principal or get_current_principal()
    if not principal or not isinstance(owner, dict):
        return False
    owner_type = owner.get("type")
    if principal["type"] == "tailscale" and owner_type in ("tailscale", "", None):
        # Older connections may only have a login or only a login-valued ID.
        owner_login = normalize_login(owner.get("login"))
        owner_id = normalize_login(owner.get("id"))
        if owner_login and owner_id and owner_login != owner_id:
            return False
        return (owner_login or owner_id) == principal["login"]
    return owner_type == principal["type"] and owner.get("id") == principal["id"]


def connection_ids_used_by_indices(index_names):
    connection_ids = set()
    for index_name in unique_valid_index_names(index_names):
        try:
            cloud = load_index_config(index_name).get("cloud") or {}
            if cloud.get("connection_id"):
                connection_ids.add(str(cloud.get("connection_id")))
        except Exception:
            logger.debug("Unable to inspect cloud connection for index %s", index_name, exc_info=True)
    return connection_ids


def index_uses_cloud_connection(index_name, connection_id):
    try:
        cloud = load_index_config(index_name).get("cloud") or {}
    except Exception:
        logger.debug("Unable to inspect cloud connection for index %s", index_name, exc_info=True)
        return False
    return bool(connection_id) and str(cloud.get("connection_id") or "") == str(connection_id)


def visible_cloud_connections():
    principal = get_current_principal()
    with cloud_connections_lock:
        connections = load_cloud_connections().get("connections", {})

    if principal.get("is_admin"):
        return connections.values()

    allowed_connection_ids = connection_ids_used_by_indices(get_accessible_indices())
    return [
        connection
        for connection in connections.values()
        if owner_matches_principal(connection.get("owner"), principal)
        or connection.get("id") in allowed_connection_ids
    ]


def list_visible_cloud_connections_public():
    return [
        sanitize_cloud_connection(connection)
        for connection in sorted(
            visible_cloud_connections(),
            key=lambda item: ((item.get("provider") or ""), (item.get("display_name") or ""), (item.get("created_at") or "")),
        )
    ]


def ensure_can_start_cloud_connection(index_name=None):
    principal = get_current_principal()
    if principal.get("is_admin"):
        return True
    if index_name:
        ensure_index_access(index_name, action="write")
        return True
    if get_accessible_indices("write"):
        return True
    raise PermissionError("Write access to an index is required to connect cloud accounts")


def ensure_cloud_connection_access(connection_id, connection):
    if not has_request_context():
        return True
    principal = get_current_principal()
    if principal.get("is_admin"):
        return True

    if connection and owner_matches_principal(connection.get("owner"), principal):
        return True

    if connection_id in connection_ids_used_by_indices(get_accessible_indices()):
        return True

    raise PermissionError("Access denied to this cloud account")


def ensure_cloud_connection_usable_for_index(index_name, connection_id):
    if not has_request_context():
        return True
    principal = get_current_principal()
    if principal.get("is_admin"):
        return True

    connection = get_cloud_connection(connection_id)
    if connection and owner_matches_principal(connection.get("owner"), principal):
        return True

    if index_name and index_uses_cloud_connection(index_name, connection_id):
        ensure_index_access(index_name, action="write")
        return True

    raise PermissionError("Use one of your own cloud accounts for this index")


def ensure_cloud_provider_usable_for_principal(provider):
    if not has_request_context():
        return True
    if get_current_principal().get("is_admin"):
        return True
    if provider == DEFAULT_CLOUD_PROVIDER:
        raise PermissionError("Admin access is required to configure a mounted or synced host folder")
    return True


def default_onedrive_provider_config():
    return {
        "client_id": MICROSOFT_GRAPH_CLIENT_ID,
        "tenant": MICROSOFT_GRAPH_TENANT or "common",
        "scopes": MICROSOFT_GRAPH_SCOPES or "offline_access User.Read Files.ReadWrite",
        "request_timeout_seconds": MICROSOFT_GRAPH_REQUEST_TIMEOUT_SECONDS,
    }


def default_google_drive_provider_config():
    return {
        "client_id": GOOGLE_DRIVE_CLIENT_ID,
        "client_secret": GOOGLE_DRIVE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_DRIVE_REDIRECT_URI,
        "scopes": GOOGLE_DRIVE_SCOPES,
        "request_timeout_seconds": GOOGLE_DRIVE_REQUEST_TIMEOUT_SECONDS,
    }


def default_dropbox_provider_config():
    return {
        "app_key": DROPBOX_APP_KEY,
        "app_secret": DROPBOX_APP_SECRET,
        "redirect_uri": DROPBOX_REDIRECT_URI,
        "scopes": DROPBOX_SCOPES,
        "request_timeout_seconds": DROPBOX_REQUEST_TIMEOUT_SECONDS,
    }


def normalize_onedrive_provider_config(raw_config=None):
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    defaults = default_onedrive_provider_config()
    merged = dict(defaults)
    merged.update(raw_config)
    return {
        "client_id": str(merged.get("client_id") or "").strip(),
        "tenant": str(merged.get("tenant") or "common").strip() or "common",
        "scopes": " ".join(str(merged.get("scopes") or defaults["scopes"]).split()),
        "request_timeout_seconds": max(
            5,
            coerce_positive_int(
                merged.get("request_timeout_seconds"),
                defaults["request_timeout_seconds"],
            ),
        ),
    }


def normalize_google_drive_provider_config(raw_config=None, existing_config=None):
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    existing_config = existing_config if isinstance(existing_config, dict) else {}
    defaults = default_google_drive_provider_config()
    merged = dict(defaults)
    merged.update(existing_config)
    merged.update(raw_config)
    client_secret = str(merged.get("client_secret") or "").strip()
    if not client_secret and parse_bool(raw_config.get("client_secret_saved"), False):
        client_secret = str(existing_config.get("client_secret") or "").strip()
    return {
        "client_id": str(merged.get("client_id") or "").strip(),
        "client_secret": client_secret,
        "redirect_uri": str(merged.get("redirect_uri") or "").strip(),
        "scopes": " ".join(str(merged.get("scopes") or defaults["scopes"]).split()),
        "request_timeout_seconds": max(
            5,
            coerce_positive_int(
                merged.get("request_timeout_seconds"),
                defaults["request_timeout_seconds"],
            ),
        ),
    }


def normalize_dropbox_provider_config(raw_config=None, existing_config=None):
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    existing_config = existing_config if isinstance(existing_config, dict) else {}
    defaults = default_dropbox_provider_config()
    merged = dict(defaults)
    merged.update(existing_config)
    merged.update(raw_config)
    app_secret = str(merged.get("app_secret") or "").strip()
    if not app_secret and parse_bool(raw_config.get("app_secret_saved"), False):
        app_secret = str(existing_config.get("app_secret") or "").strip()
    return {
        "app_key": str(merged.get("app_key") or "").strip(),
        "app_secret": app_secret,
        "redirect_uri": str(merged.get("redirect_uri") or "").strip(),
        "scopes": " ".join(str(merged.get("scopes") or defaults["scopes"]).split()),
        "request_timeout_seconds": max(
            5,
            coerce_positive_int(
                merged.get("request_timeout_seconds"),
                defaults["request_timeout_seconds"],
            ),
        ),
    }


def default_cloud_provider_config():
    return {
        "version": 1,
        "providers": {
            ONEDRIVE_CLOUD_PROVIDER: default_onedrive_provider_config(),
            GOOGLE_DRIVE_CLOUD_PROVIDER: default_google_drive_provider_config(),
            DROPBOX_CLOUD_PROVIDER: default_dropbox_provider_config(),
        },
    }


def normalize_cloud_provider_config(raw_config=None):
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    providers = raw_config.get("providers") if isinstance(raw_config.get("providers"), dict) else {}
    defaults = default_cloud_provider_config()["providers"]
    return {
        "version": 1,
        "providers": {
            ONEDRIVE_CLOUD_PROVIDER: normalize_onedrive_provider_config(
                providers.get(ONEDRIVE_CLOUD_PROVIDER)
            ),
            GOOGLE_DRIVE_CLOUD_PROVIDER: normalize_google_drive_provider_config(
                providers.get(GOOGLE_DRIVE_CLOUD_PROVIDER),
                defaults.get(GOOGLE_DRIVE_CLOUD_PROVIDER),
            ),
            DROPBOX_CLOUD_PROVIDER: normalize_dropbox_provider_config(
                providers.get(DROPBOX_CLOUD_PROVIDER),
                defaults.get(DROPBOX_CLOUD_PROVIDER),
            ),
        },
    }


def load_cloud_provider_config():
    if not os.path.exists(CLOUD_PROVIDER_CONFIG_PATH):
        return normalize_cloud_provider_config()

    try:
        with open(CLOUD_PROVIDER_CONFIG_PATH, "r") as f:
            raw_payload = json.load(f)
    except Exception:
        logger.warning("Unable to read cloud provider config", exc_info=True)
        return normalize_cloud_provider_config()

    return normalize_cloud_provider_config(raw_payload)


def validate_onedrive_provider_config(config):
    if not config.get("client_id"):
        raise ValueError("Enter the Microsoft application/client ID before connecting OneDrive")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", config.get("tenant") or ""):
        raise ValueError("OneDrive tenant may only contain letters, numbers, dots, underscores, and hyphens")
    scopes = set(str(config.get("scopes") or "").split())
    required_scopes = {"offline_access", "User.Read"}
    missing_scopes = sorted(required_scopes - scopes)
    if missing_scopes:
        raise ValueError(f"OneDrive scopes must include: {', '.join(missing_scopes)}")
    if "Files.ReadWrite" not in scopes and "Files.ReadWrite.All" not in scopes:
        raise ValueError("OneDrive scopes must include Files.ReadWrite or Files.ReadWrite.All")


def validate_google_drive_provider_config(config):
    if not config.get("client_id"):
        raise ValueError("Enter the Google OAuth client ID before connecting Google Drive")
    scopes = set(str(config.get("scopes") or "").split())
    if "https://www.googleapis.com/auth/drive" not in scopes:
        raise ValueError("Google Drive scopes must include https://www.googleapis.com/auth/drive")


def validate_dropbox_provider_config(config):
    if not config.get("app_key"):
        raise ValueError("Enter the Dropbox app key before connecting Dropbox")
    scopes = set(str(config.get("scopes") or "").split())
    required_scopes = {"account_info.read", "files.metadata.read", "files.content.read", "files.content.write"}
    missing_scopes = sorted(required_scopes - scopes)
    if missing_scopes:
        raise ValueError(f"Dropbox scopes must include: {', '.join(missing_scopes)}")


def validate_cloud_provider_config(provider, config):
    if provider == ONEDRIVE_CLOUD_PROVIDER:
        validate_onedrive_provider_config(config)
        return
    if provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
        validate_google_drive_provider_config(config)
        return
    if provider == DROPBOX_CLOUD_PROVIDER:
        validate_dropbox_provider_config(config)
        return
    raise ValueError("Unsupported cloud provider settings")


def save_cloud_provider_config(config, validate_provider=None):
    normalized = normalize_cloud_provider_config(config)
    if validate_provider:
        validate_cloud_provider_config(validate_provider, normalized["providers"][validate_provider])

    os.makedirs(os.path.dirname(CLOUD_PROVIDER_CONFIG_PATH), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".cloud_provider_config_",
        suffix=".json",
        dir=os.path.dirname(CLOUD_PROVIDER_CONFIG_PATH),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(normalized, f, indent=4)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            logger.debug("Unable to restrict cloud provider config temp file permissions", exc_info=True)
        os.replace(temp_path, CLOUD_PROVIDER_CONFIG_PATH)
        try:
            os.chmod(CLOUD_PROVIDER_CONFIG_PATH, 0o600)
        except OSError:
            logger.debug("Unable to restrict cloud provider config file permissions", exc_info=True)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return normalized


def get_onedrive_provider_config(connection=None):
    if connection and isinstance(connection.get("provider_config"), dict):
        provider_config = normalize_onedrive_provider_config(connection.get("provider_config"))
        if provider_config.get("client_id"):
            return provider_config
    return load_cloud_provider_config()["providers"][ONEDRIVE_CLOUD_PROVIDER]


def get_google_drive_provider_config(connection=None):
    if connection and isinstance(connection.get("provider_config"), dict):
        provider_config = normalize_google_drive_provider_config(connection.get("provider_config"))
        if provider_config.get("client_id"):
            return provider_config
    return load_cloud_provider_config()["providers"][GOOGLE_DRIVE_CLOUD_PROVIDER]


def get_dropbox_provider_config(connection=None):
    if connection and isinstance(connection.get("provider_config"), dict):
        provider_config = normalize_dropbox_provider_config(connection.get("provider_config"))
        if provider_config.get("app_key"):
            return provider_config
    return load_cloud_provider_config()["providers"][DROPBOX_CLOUD_PROVIDER]


def get_provider_config(provider, connection=None):
    if provider == ONEDRIVE_CLOUD_PROVIDER:
        return get_onedrive_provider_config(connection)
    if provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
        return get_google_drive_provider_config(connection)
    if provider == DROPBOX_CLOUD_PROVIDER:
        return get_dropbox_provider_config(connection)
    return {}


def sanitize_onedrive_provider_config(config):
    config = normalize_onedrive_provider_config(config)
    return {
        "client_id": config.get("client_id"),
        "tenant": config.get("tenant"),
        "scopes": config.get("scopes"),
        "request_timeout_seconds": config.get("request_timeout_seconds"),
        "configured": bool(config.get("client_id")),
    }


def sanitize_google_drive_provider_config(config, include_secrets=False):
    config = normalize_google_drive_provider_config(config)
    return {
        "client_id": config.get("client_id"),
        "client_secret": config.get("client_secret") if include_secrets else "",
        "client_secret_saved": bool(config.get("client_secret")),
        "redirect_uri": config.get("redirect_uri"),
        "scopes": config.get("scopes"),
        "request_timeout_seconds": config.get("request_timeout_seconds"),
        "configured": bool(config.get("client_id")),
    }


def sanitize_dropbox_provider_config(config, include_secrets=False):
    config = normalize_dropbox_provider_config(config)
    return {
        "app_key": config.get("app_key"),
        "app_secret": config.get("app_secret") if include_secrets else "",
        "app_secret_saved": bool(config.get("app_secret")),
        "redirect_uri": config.get("redirect_uri"),
        "scopes": config.get("scopes"),
        "request_timeout_seconds": config.get("request_timeout_seconds"),
        "configured": bool(config.get("app_key")),
    }


def cloud_provider_settings_public(include_secrets=False):
    config = load_cloud_provider_config()
    return {
        ONEDRIVE_CLOUD_PROVIDER: sanitize_onedrive_provider_config(
            config["providers"][ONEDRIVE_CLOUD_PROVIDER]
        ),
        GOOGLE_DRIVE_CLOUD_PROVIDER: sanitize_google_drive_provider_config(
            config["providers"][GOOGLE_DRIVE_CLOUD_PROVIDER],
            include_secrets=include_secrets,
        ),
        DROPBOX_CLOUD_PROVIDER: sanitize_dropbox_provider_config(
            config["providers"][DROPBOX_CLOUD_PROVIDER],
            include_secrets=include_secrets,
        ),
    }


def cloud_provider_definitions():
    onedrive_config = get_onedrive_provider_config()
    onedrive_configured = bool(onedrive_config.get("client_id"))
    google_drive_config = get_google_drive_provider_config()
    google_drive_configured = bool(google_drive_config.get("client_id"))
    dropbox_config = get_dropbox_provider_config()
    dropbox_configured = bool(dropbox_config.get("app_key"))
    return [
        {
            "id": DEFAULT_CLOUD_PROVIDER,
            "label": provider_display_name(DEFAULT_CLOUD_PROVIDER),
            "requires_login": False,
            "configured": True,
        },
        {
            "id": ONEDRIVE_CLOUD_PROVIDER,
            "label": provider_display_name(ONEDRIVE_CLOUD_PROVIDER),
            "requires_login": True,
            "configured": onedrive_configured,
            "missing_configuration": "" if onedrive_configured else "OneDrive provider registration is not saved yet",
        },
        {
            "id": GOOGLE_DRIVE_CLOUD_PROVIDER,
            "label": provider_display_name(GOOGLE_DRIVE_CLOUD_PROVIDER),
            "requires_login": True,
            "configured": google_drive_configured,
            "missing_configuration": "" if google_drive_configured else "Google Drive provider registration is not saved yet",
        },
        {
            "id": DROPBOX_CLOUD_PROVIDER,
            "label": provider_display_name(DROPBOX_CLOUD_PROVIDER),
            "requires_login": True,
            "configured": dropbox_configured,
            "missing_configuration": "" if dropbox_configured else "Dropbox provider registration is not saved yet",
        },
    ]


def require_onedrive_client_id(provider_config=None):
    provider_config = normalize_onedrive_provider_config(provider_config or get_onedrive_provider_config())
    if not provider_config.get("client_id"):
        raise ValueError("Register the OneDrive provider in Admin -> Index Mode before connecting an account")
    return provider_config["client_id"]


def microsoft_token_url(provider_config=None):
    provider_config = normalize_onedrive_provider_config(provider_config or get_onedrive_provider_config())
    tenant = provider_config.get("tenant") or "common"
    return f"{MICROSOFT_LOGIN_BASE_URL}/{tenant}/oauth2/v2.0/token"


def microsoft_device_code_url(provider_config=None):
    provider_config = normalize_onedrive_provider_config(provider_config or get_onedrive_provider_config())
    tenant = provider_config.get("tenant") or "common"
    return f"{MICROSOFT_LOGIN_BASE_URL}/{tenant}/oauth2/v2.0/devicecode"


def graph_error_message(response):
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return error.get("message") or error.get("code") or response.text
        if isinstance(error, str):
            return error
        if isinstance(payload, dict):
            return payload.get("error_description") or payload.get("error_summary") or payload.get("message") or response.text
    except Exception:
        pass
    return response.text or f"HTTP {response.status_code}"


def utc_expires_at(seconds_from_now):
    seconds = coerce_positive_int(seconds_from_now, 3600)
    return (datetime.utcnow() + timedelta(seconds=seconds)).isoformat() + "Z"


def normalize_onedrive_token_payload(raw_token, previous_token=None, provider_config=None):
    previous_token = previous_token if isinstance(previous_token, dict) else {}
    raw_token = raw_token if isinstance(raw_token, dict) else {}
    provider_config = normalize_onedrive_provider_config(provider_config or get_onedrive_provider_config())
    refresh_token = raw_token.get("refresh_token") or previous_token.get("refresh_token")
    access_token = raw_token.get("access_token") or previous_token.get("access_token")
    if not access_token:
        raise ValueError("Microsoft did not return an access token")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": raw_token.get("token_type") or previous_token.get("token_type") or "Bearer",
        "scope": raw_token.get("scope") or previous_token.get("scope") or provider_config["scopes"],
        "expires_at": utc_expires_at(raw_token.get("expires_in") or 3600),
    }


def fetch_onedrive_account(access_token, provider_config=None):
    provider_config = normalize_onedrive_provider_config(provider_config or get_onedrive_provider_config())
    response = requests.get(
        f"{MICROSOFT_GRAPH_BASE_URL}/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Unable to read OneDrive account profile: {graph_error_message(response)}")
    payload = response.json()
    if not isinstance(payload, dict):
        return {}
    return {
        "id": payload.get("id"),
        "displayName": payload.get("displayName"),
        "mail": payload.get("mail"),
        "userPrincipalName": payload.get("userPrincipalName"),
    }


def update_cloud_auth_session(connection_id, **updates):
    with cloud_auth_sessions_lock:
        session = cloud_auth_sessions.get(connection_id)
        if not session:
            return None
        session.update(updates)
        session["updated_at"] = utc_now_iso()
        return dict(session)


def finish_cloud_auth_session(connection_id, status, **updates):
    updates["status"] = status
    return update_cloud_auth_session(connection_id, **updates)


def poll_onedrive_device_login(connection_id):
    while True:
        with cloud_auth_sessions_lock:
            session = dict(cloud_auth_sessions.get(connection_id) or {})

        if not session:
            return
        expires_at = parse_utc_datetime(session.get("expires_at"))
        if expires_at and datetime.utcnow() >= expires_at:
            finish_cloud_auth_session(connection_id, "expired", last_error="OneDrive login expired")
            return

        time.sleep(max(1, coerce_positive_int(session.get("interval"), 5)))

        try:
            provider_config = normalize_onedrive_provider_config(session.get("provider_config"))
            response = requests.post(
                microsoft_token_url(provider_config),
                data={
                    "client_id": require_onedrive_client_id(provider_config),
                    "grant_type": MICROSOFT_DEVICE_CODE_GRANT,
                    "device_code": session.get("device_code"),
                },
                timeout=provider_config["request_timeout_seconds"],
            )

            if response.status_code == 200:
                token = normalize_onedrive_token_payload(response.json(), provider_config=provider_config)
                try:
                    account = fetch_onedrive_account(token["access_token"], provider_config=provider_config)
                except Exception as account_error:
                    logger.warning("OneDrive connected, but account profile lookup failed: %s", account_error, exc_info=True)
                    account = {}

                connection = {
                    "id": connection_id,
                    "provider": ONEDRIVE_CLOUD_PROVIDER,
                    "display_name": session.get("display_name") or account.get("displayName") or account.get("userPrincipalName") or "OneDrive",
                    "status": "connected",
                    "created_at": session.get("created_at"),
                    "owner": session.get("owner"),
                    "account": account,
                    "token": token,
                    "provider_config": provider_config,
                    "last_error": None,
                }
                saved_connection = upsert_cloud_connection(connection)
                finish_cloud_auth_session(connection_id, "connected", connection=saved_connection, last_error=None)
                return

            try:
                payload = response.json()
            except Exception:
                payload = {}
            error_code = payload.get("error") if isinstance(payload, dict) else ""
            if error_code == "authorization_pending":
                update_cloud_auth_session(connection_id, status="pending")
                continue
            if error_code == "slow_down":
                update_cloud_auth_session(
                    connection_id,
                    status="pending",
                    interval=max(1, coerce_positive_int(session.get("interval"), 5)) + 5,
                )
                continue
            if error_code in {"authorization_declined", "expired_token"}:
                finish_cloud_auth_session(connection_id, "failed", last_error=payload.get("error_description") or error_code)
                return

            finish_cloud_auth_session(connection_id, "failed", last_error=payload.get("error_description") or graph_error_message(response))
            return
        except Exception as e:
            logger.warning("OneDrive login polling failed: %s", e, exc_info=True)
            finish_cloud_auth_session(connection_id, "failed", last_error=str(e))
            return


def start_cloud_auth_polling_thread(connection_id):
    thread = threading.Thread(
        target=poll_onedrive_device_login,
        args=(connection_id,),
        daemon=True,
    )
    thread.start()
    return thread


def start_onedrive_device_login(display_name=""):
    provider_config = get_onedrive_provider_config()
    require_onedrive_client_id(provider_config)
    response = requests.post(
        microsoft_device_code_url(provider_config),
        data={
            "client_id": provider_config["client_id"],
            "scope": provider_config["scopes"],
        },
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Unable to start OneDrive login: {graph_error_message(response)}")

    payload = response.json()
    connection_id = f"onedrive-{uuid.uuid4().hex}"
    created_at = utc_now_iso()
    session = {
        "id": connection_id,
        "provider": ONEDRIVE_CLOUD_PROVIDER,
        "display_name": str(display_name or "").strip() or "OneDrive",
        "status": "pending",
        "created_at": created_at,
        "updated_at": created_at,
        "expires_at": utc_expires_at(payload.get("expires_in") or 900),
        "verification_uri": payload.get("verification_uri"),
        "verification_uri_complete": payload.get("verification_uri_complete"),
        "user_code": payload.get("user_code"),
        "message": payload.get("message"),
        "interval": coerce_positive_int(payload.get("interval"), 5),
        "device_code": payload.get("device_code"),
        "owner": current_principal_owner(),
        "provider_config": provider_config,
        "last_error": None,
    }

    with cloud_auth_sessions_lock:
        cloud_auth_sessions[connection_id] = session

    start_cloud_auth_polling_thread(connection_id)
    return sanitize_cloud_auth_session(session)


def oauth_code_verifier():
    return base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")


def oauth_code_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def cloud_callback_url(provider, provider_config=None):
    provider_config = provider_config if isinstance(provider_config, dict) else {}
    configured_redirect_uri = str(provider_config.get("redirect_uri") or "").strip()
    if configured_redirect_uri:
        return configured_redirect_uri
    if not has_request_context():
        raise ValueError(f"Set a redirect URL before connecting {provider_display_name(provider)}")
    return request.url_root.rstrip("/") + f"/cloud-connections/callback/{provider}"


def find_cloud_auth_session_by_state(provider, state):
    state = str(state or "").strip()
    if not state:
        return None
    with cloud_auth_sessions_lock:
        for session in cloud_auth_sessions.values():
            if session.get("provider") == provider and session.get("state") == state:
                if not owner_matches_principal(session.get("owner")):
                    raise PermissionError("Access denied to this cloud login")
                return dict(session)
    return None


def cloud_login_result_page(title, message, ok=True):
    status = "connected" if ok else "not connected"
    return Response(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Cloud login</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 20px;line-height:1.45}"
        ".box{border:1px solid #ddd;border-radius:6px;padding:18px;background:#fafafa}"
        ".status{font-weight:700;color:%s}</style></head><body>"
        "<div class='box'><h1>%s</h1><p class='status'>%s</p><p>%s</p>"
        "<p>You can close this tab and return to PDFSearch.</p></div></body></html>"
        % (
            "#0b6b2b" if ok else "#9f1d1d",
            escape(title),
            escape(status),
            escape(message),
        ),
        mimetype="text/html",
        status=200 if ok else 400,
    )


def start_google_drive_browser_login(display_name=""):
    provider_config = get_google_drive_provider_config()
    validate_google_drive_provider_config(provider_config)
    redirect_uri = cloud_callback_url(GOOGLE_DRIVE_CLOUD_PROVIDER, provider_config)
    verifier = oauth_code_verifier()
    state = uuid.uuid4().hex
    connection_id = f"google-drive-{uuid.uuid4().hex}"
    created_at = utc_now_iso()
    params = {
        "client_id": provider_config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider_config["scopes"],
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": oauth_code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    session = {
        "id": connection_id,
        "provider": GOOGLE_DRIVE_CLOUD_PROVIDER,
        "display_name": str(display_name or "").strip() or "Google Drive",
        "status": "waiting_for_login",
        "created_at": created_at,
        "updated_at": created_at,
        "expires_at": utc_expires_at(1800),
        "authorization_url": f"{GOOGLE_OAUTH_AUTH_URL}?{urlencode(params)}",
        "callback_url": redirect_uri,
        "code_verifier": verifier,
        "state": state,
        "owner": current_principal_owner(),
        "provider_config": provider_config,
        "last_error": None,
    }
    with cloud_auth_sessions_lock:
        cloud_auth_sessions[connection_id] = session
    return sanitize_cloud_auth_session(session)


def normalize_google_drive_token_payload(raw_token, previous_token=None, provider_config=None):
    previous_token = previous_token if isinstance(previous_token, dict) else {}
    raw_token = raw_token if isinstance(raw_token, dict) else {}
    provider_config = normalize_google_drive_provider_config(provider_config or get_google_drive_provider_config())
    access_token = raw_token.get("access_token") or previous_token.get("access_token")
    refresh_token = raw_token.get("refresh_token") or previous_token.get("refresh_token")
    if not access_token:
        raise ValueError("Google did not return an access token")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": raw_token.get("token_type") or previous_token.get("token_type") or "Bearer",
        "scope": raw_token.get("scope") or previous_token.get("scope") or provider_config["scopes"],
        "expires_at": utc_expires_at(raw_token.get("expires_in") or 3600),
    }


def fetch_google_drive_account(access_token, provider_config=None):
    provider_config = normalize_google_drive_provider_config(provider_config or get_google_drive_provider_config())
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(
        GOOGLE_USERINFO_URL,
        headers=headers,
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code < 400:
        payload = response.json()
        if isinstance(payload, dict):
            return {
                "id": payload.get("id"),
                "displayName": payload.get("name"),
                "mail": payload.get("email"),
                "userPrincipalName": payload.get("email"),
            }

    response = requests.get(
        f"{GOOGLE_DRIVE_BASE_URL}/about",
        headers=headers,
        params={"fields": "user"},
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Unable to read Google Drive account profile: {graph_error_message(response)}")
    user = (response.json() or {}).get("user") or {}
    return {
        "id": user.get("permissionId"),
        "displayName": user.get("displayName"),
        "mail": user.get("emailAddress"),
        "userPrincipalName": user.get("emailAddress"),
    }


def complete_google_drive_login(state, code=None, error=None, error_description=None):
    session = find_cloud_auth_session_by_state(GOOGLE_DRIVE_CLOUD_PROVIDER, state)
    if not session:
        return cloud_login_result_page("Google Drive login", "This login session was not found or has expired.", ok=False)
    if error:
        finish_cloud_auth_session(session["id"], "failed", last_error=error_description or error)
        return cloud_login_result_page("Google Drive login", error_description or error, ok=False)
    if not code:
        finish_cloud_auth_session(session["id"], "failed", last_error="Google did not return a login code")
        return cloud_login_result_page("Google Drive login", "Google did not return a login code.", ok=False)

    provider_config = normalize_google_drive_provider_config(session.get("provider_config"))
    data = {
        "client_id": provider_config["client_id"],
        "code": code,
        "code_verifier": session.get("code_verifier"),
        "grant_type": "authorization_code",
        "redirect_uri": session.get("callback_url") or cloud_callback_url(GOOGLE_DRIVE_CLOUD_PROVIDER, provider_config),
    }
    if provider_config.get("client_secret"):
        data["client_secret"] = provider_config["client_secret"]

    response = requests.post(
        GOOGLE_OAUTH_TOKEN_URL,
        data=data,
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        message = graph_error_message(response)
        finish_cloud_auth_session(session["id"], "failed", last_error=message)
        return cloud_login_result_page("Google Drive login", message, ok=False)

    try:
        token = normalize_google_drive_token_payload(response.json(), provider_config=provider_config)
        account = fetch_google_drive_account(token["access_token"], provider_config=provider_config)
    except Exception as e:
        finish_cloud_auth_session(session["id"], "failed", last_error=str(e))
        return cloud_login_result_page("Google Drive login", str(e), ok=False)

    connection = {
        "id": session["id"],
        "provider": GOOGLE_DRIVE_CLOUD_PROVIDER,
        "display_name": session.get("display_name") or account.get("displayName") or "Google Drive",
        "status": "connected",
        "created_at": session.get("created_at"),
        "owner": session.get("owner"),
        "account": account,
        "token": token,
        "provider_config": provider_config,
        "last_error": None,
    }
    saved_connection = upsert_cloud_connection(connection)
    finish_cloud_auth_session(session["id"], "connected", connection=saved_connection, last_error=None)
    return cloud_login_result_page("Google Drive login", "Google Drive is connected.", ok=True)


def start_dropbox_browser_login(display_name=""):
    provider_config = get_dropbox_provider_config()
    validate_dropbox_provider_config(provider_config)
    redirect_uri = cloud_callback_url(DROPBOX_CLOUD_PROVIDER, provider_config)
    verifier = oauth_code_verifier()
    state = uuid.uuid4().hex
    connection_id = f"dropbox-{uuid.uuid4().hex}"
    created_at = utc_now_iso()
    params = {
        "client_id": provider_config["app_key"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider_config["scopes"],
        "token_access_type": "offline",
        "code_challenge": oauth_code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    session = {
        "id": connection_id,
        "provider": DROPBOX_CLOUD_PROVIDER,
        "display_name": str(display_name or "").strip() or "Dropbox",
        "status": "waiting_for_login",
        "created_at": created_at,
        "updated_at": created_at,
        "expires_at": utc_expires_at(1800),
        "authorization_url": f"{DROPBOX_AUTH_URL}?{urlencode(params)}",
        "callback_url": redirect_uri,
        "code_verifier": verifier,
        "state": state,
        "owner": current_principal_owner(),
        "provider_config": provider_config,
        "last_error": None,
    }
    with cloud_auth_sessions_lock:
        cloud_auth_sessions[connection_id] = session
    return sanitize_cloud_auth_session(session)


def normalize_dropbox_token_payload(raw_token, previous_token=None, provider_config=None):
    previous_token = previous_token if isinstance(previous_token, dict) else {}
    raw_token = raw_token if isinstance(raw_token, dict) else {}
    provider_config = normalize_dropbox_provider_config(provider_config or get_dropbox_provider_config())
    access_token = raw_token.get("access_token") or previous_token.get("access_token")
    refresh_token = raw_token.get("refresh_token") or previous_token.get("refresh_token")
    if not access_token:
        raise ValueError("Dropbox did not return an access token")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": raw_token.get("token_type") or previous_token.get("token_type") or "bearer",
        "scope": raw_token.get("scope") or previous_token.get("scope") or provider_config["scopes"],
        "expires_at": utc_expires_at(raw_token.get("expires_in") or 14400),
    }


def fetch_dropbox_account(access_token, provider_config=None):
    provider_config = normalize_dropbox_provider_config(provider_config or get_dropbox_provider_config())
    response = requests.post(
        f"{DROPBOX_API_BASE_URL}/users/get_current_account",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Unable to read Dropbox account profile: {graph_error_message(response)}")
    payload = response.json()
    email = payload.get("email")
    name = payload.get("name") if isinstance(payload.get("name"), dict) else {}
    display_name = name.get("display_name") or payload.get("display_name")
    return {
        "id": payload.get("account_id"),
        "displayName": display_name,
        "mail": email,
        "userPrincipalName": email,
    }


def complete_dropbox_login(state, code=None, error=None, error_description=None):
    session = find_cloud_auth_session_by_state(DROPBOX_CLOUD_PROVIDER, state)
    if not session:
        return cloud_login_result_page("Dropbox login", "This login session was not found or has expired.", ok=False)
    if error:
        finish_cloud_auth_session(session["id"], "failed", last_error=error_description or error)
        return cloud_login_result_page("Dropbox login", error_description or error, ok=False)
    if not code:
        finish_cloud_auth_session(session["id"], "failed", last_error="Dropbox did not return a login code")
        return cloud_login_result_page("Dropbox login", "Dropbox did not return a login code.", ok=False)

    provider_config = normalize_dropbox_provider_config(session.get("provider_config"))
    data = {
        "client_id": provider_config["app_key"],
        "code": code,
        "code_verifier": session.get("code_verifier"),
        "grant_type": "authorization_code",
        "redirect_uri": session.get("callback_url") or cloud_callback_url(DROPBOX_CLOUD_PROVIDER, provider_config),
    }
    if provider_config.get("app_secret"):
        data["client_secret"] = provider_config["app_secret"]

    response = requests.post(
        DROPBOX_TOKEN_URL,
        data=data,
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        message = graph_error_message(response)
        finish_cloud_auth_session(session["id"], "failed", last_error=message)
        return cloud_login_result_page("Dropbox login", message, ok=False)

    try:
        token = normalize_dropbox_token_payload(response.json(), provider_config=provider_config)
        account = fetch_dropbox_account(token["access_token"], provider_config=provider_config)
    except Exception as e:
        finish_cloud_auth_session(session["id"], "failed", last_error=str(e))
        return cloud_login_result_page("Dropbox login", str(e), ok=False)

    connection = {
        "id": session["id"],
        "provider": DROPBOX_CLOUD_PROVIDER,
        "display_name": session.get("display_name") or account.get("displayName") or "Dropbox",
        "status": "connected",
        "created_at": session.get("created_at"),
        "owner": session.get("owner"),
        "account": account,
        "token": token,
        "provider_config": provider_config,
        "last_error": None,
    }
    saved_connection = upsert_cloud_connection(connection)
    finish_cloud_auth_session(session["id"], "connected", connection=saved_connection, last_error=None)
    return cloud_login_result_page("Dropbox login", "Dropbox is connected.", ok=True)


def refresh_onedrive_token_if_needed(connection, force=False):
    connection = normalize_cloud_connection(connection)
    if not connection or connection.get("provider") != ONEDRIVE_CLOUD_PROVIDER:
        raise ValueError("Invalid OneDrive connection")
    revived_retryable_failure = False
    if connection.get("status") != "connected":
        if cloud_connection_can_retry(connection):
            connection["status"] = "connected"
            revived_retryable_failure = True
        else:
            raise ValueError(cloud_connection_not_connected_message(ONEDRIVE_CLOUD_PROVIDER, connection))

    provider_config = get_onedrive_provider_config(connection)
    token = connection.get("token") or {}
    expires_at = parse_utc_datetime(token.get("expires_at"))
    if not force and expires_at and (expires_at - datetime.utcnow()).total_seconds() > 300:
        if revived_retryable_failure:
            connection["last_error"] = None
            return upsert_cloud_connection(connection)
        return connection

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise ValueError("OneDrive connection must be reconnected because no refresh token is available")

    response = requests.post(
        microsoft_token_url(provider_config),
        data={
            "client_id": require_onedrive_client_id(provider_config),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": provider_config["scopes"],
        },
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        message = graph_error_message(response)
        connection["status"] = "connected" if cloud_response_error_is_retryable(response, message) else "failed"
        connection["last_error"] = message
        upsert_cloud_connection(connection)
        raise RuntimeError(f"Unable to refresh OneDrive connection: {connection['last_error']}")

    connection["token"] = normalize_onedrive_token_payload(
        response.json(),
        previous_token=token,
        provider_config=provider_config,
    )
    connection["provider_config"] = provider_config
    connection["status"] = "connected"
    connection["last_error"] = None
    return upsert_cloud_connection(connection)


def get_connected_onedrive_connection(connection_id):
    connection = get_cloud_connection(connection_id)
    if not connection:
        raise ValueError("Select a connected OneDrive account before saving this index mode")
    if connection.get("provider") != ONEDRIVE_CLOUD_PROVIDER:
        raise ValueError("Selected cloud connection is not a OneDrive account")
    ensure_cloud_connection_access(connection_id, connection)
    return refresh_onedrive_token_if_needed(connection)


def refresh_google_drive_token_if_needed(connection, force=False):
    connection = normalize_cloud_connection(connection)
    if not connection or connection.get("provider") != GOOGLE_DRIVE_CLOUD_PROVIDER:
        raise ValueError("Invalid Google Drive connection")
    revived_retryable_failure = False
    if connection.get("status") != "connected":
        if cloud_connection_can_retry(connection):
            connection["status"] = "connected"
            revived_retryable_failure = True
        else:
            raise ValueError(cloud_connection_not_connected_message(GOOGLE_DRIVE_CLOUD_PROVIDER, connection))

    provider_config = get_google_drive_provider_config(connection)
    token = connection.get("token") or {}
    expires_at = parse_utc_datetime(token.get("expires_at"))
    if not force and expires_at and (expires_at - datetime.utcnow()).total_seconds() > 300:
        if revived_retryable_failure:
            connection["last_error"] = None
            return upsert_cloud_connection(connection)
        return connection

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise ValueError("Google Drive connection must be reconnected because no refresh token is available")

    data = {
        "client_id": provider_config["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if provider_config.get("client_secret"):
        data["client_secret"] = provider_config["client_secret"]

    response = requests.post(
        GOOGLE_OAUTH_TOKEN_URL,
        data=data,
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        message = graph_error_message(response)
        connection["status"] = "connected" if cloud_response_error_is_retryable(response, message) else "failed"
        connection["last_error"] = message
        upsert_cloud_connection(connection)
        raise RuntimeError(f"Unable to refresh Google Drive connection: {connection['last_error']}")

    connection["token"] = normalize_google_drive_token_payload(
        response.json(),
        previous_token=token,
        provider_config=provider_config,
    )
    connection["provider_config"] = provider_config
    connection["status"] = "connected"
    connection["last_error"] = None
    return upsert_cloud_connection(connection)


def refresh_dropbox_token_if_needed(connection, force=False):
    connection = normalize_cloud_connection(connection)
    if not connection or connection.get("provider") != DROPBOX_CLOUD_PROVIDER:
        raise ValueError("Invalid Dropbox connection")
    revived_retryable_failure = False
    if connection.get("status") != "connected":
        if cloud_connection_can_retry(connection):
            connection["status"] = "connected"
            revived_retryable_failure = True
        else:
            raise ValueError(cloud_connection_not_connected_message(DROPBOX_CLOUD_PROVIDER, connection))

    provider_config = get_dropbox_provider_config(connection)
    token = connection.get("token") or {}
    expires_at = parse_utc_datetime(token.get("expires_at"))
    if not force and expires_at and (expires_at - datetime.utcnow()).total_seconds() > 300:
        if revived_retryable_failure:
            connection["last_error"] = None
            return upsert_cloud_connection(connection)
        return connection

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise ValueError("Dropbox connection must be reconnected because no refresh token is available")

    data = {
        "client_id": provider_config["app_key"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if provider_config.get("app_secret"):
        data["client_secret"] = provider_config["app_secret"]

    response = requests.post(
        DROPBOX_TOKEN_URL,
        data=data,
        timeout=provider_config["request_timeout_seconds"],
    )
    if response.status_code >= 400:
        message = graph_error_message(response)
        connection["status"] = "connected" if cloud_response_error_is_retryable(response, message) else "failed"
        connection["last_error"] = message
        upsert_cloud_connection(connection)
        raise RuntimeError(f"Unable to refresh Dropbox connection: {connection['last_error']}")

    connection["token"] = normalize_dropbox_token_payload(
        response.json(),
        previous_token=token,
        provider_config=provider_config,
    )
    connection["provider_config"] = provider_config
    connection["status"] = "connected"
    connection["last_error"] = None
    return upsert_cloud_connection(connection)


def get_connected_google_drive_connection(connection_id):
    connection = get_cloud_connection(connection_id)
    if not connection:
        raise ValueError("Select a connected Google Drive account before saving this index mode")
    if connection.get("provider") != GOOGLE_DRIVE_CLOUD_PROVIDER:
        raise ValueError("Selected cloud connection is not a Google Drive account")
    ensure_cloud_connection_access(connection_id, connection)
    return refresh_google_drive_token_if_needed(connection)


def get_connected_dropbox_connection(connection_id):
    connection = get_cloud_connection(connection_id)
    if not connection:
        raise ValueError("Select a connected Dropbox account before saving this index mode")
    if connection.get("provider") != DROPBOX_CLOUD_PROVIDER:
        raise ValueError("Selected cloud connection is not a Dropbox account")
    ensure_cloud_connection_access(connection_id, connection)
    return refresh_dropbox_token_if_needed(connection)


def get_connected_provider_connection(provider, connection_id):
    if provider == ONEDRIVE_CLOUD_PROVIDER:
        return get_connected_onedrive_connection(connection_id)
    if provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
        return get_connected_google_drive_connection(connection_id)
    if provider == DROPBOX_CLOUD_PROVIDER:
        return get_connected_dropbox_connection(connection_id)
    raise ValueError("Unsupported cloud provider")


RETRYABLE_CLOUD_ERROR_FRAGMENTS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "rate limit",
    "service unavailable",
    "temporarily",
    "temporarily_unavailable",
    "throttl",
    "timed out",
    "timeout",
)


def cloud_error_message_is_retryable(message):
    normalized = str(message or "").strip().lower()
    return bool(normalized) and any(fragment in normalized for fragment in RETRYABLE_CLOUD_ERROR_FRAGMENTS)


def cloud_response_error_is_retryable(response, message=""):
    try:
        status_code = int(getattr(response, "status_code", 0))
    except (TypeError, ValueError):
        status_code = 0
    return status_code == 429 or status_code >= 500 or cloud_error_message_is_retryable(message)


def cloud_connection_can_retry(connection):
    if not isinstance(connection, dict):
        return False
    return (
        str(connection.get("status") or "").strip().lower() == "failed"
        and cloud_error_message_is_retryable(connection.get("last_error"))
    )


def cloud_connection_display_name(connection, provider=None):
    connection = connection if isinstance(connection, dict) else {}
    account = connection.get("account") if isinstance(connection.get("account"), dict) else {}
    return (
        connection.get("display_name")
        or account.get("userPrincipalName")
        or account.get("mail")
        or account.get("displayName")
        or provider_display_name(provider or connection.get("provider"))
    )


def cloud_connection_not_connected_message(provider, connection):
    status = str((connection or {}).get("status") or "not connected").strip() or "not connected"
    display_name = cloud_connection_display_name(connection, provider)
    last_error = str((connection or {}).get("last_error") or "").strip()
    if cloud_connection_can_retry(connection):
        return (
            f"{provider_display_name(provider)} connection '{display_name}' had a temporary provider error"
            f"{': ' + last_error if last_error else ''}. Try syncing again."
        )
    return (
        f"{provider_display_name(provider)} connection '{display_name}' is {status}. "
        "Reconnect the account, select it for this index, and save the index mode before syncing."
    )


def ensure_cloud_sync_config_ready(index_name, config=None):
    config = config or load_index_config(index_name)
    if not index_has_cloud_source(index_name, config):
        raise ValueError("This index has no configured cloud folder")

    cloud = config.get("cloud") or {}
    provider = cloud.get("provider")
    if provider not in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
        return config

    connection_id = cloud.get("connection_id")
    connection = get_cloud_connection(connection_id)
    if not connection:
        raise ValueError(f"Select a connected {provider_display_name(provider)} account before syncing")
    if connection.get("provider") != provider:
        raise ValueError("Selected cloud account does not match this provider")
    if connection.get("status") != "connected" and not cloud_connection_can_retry(connection):
        raise ValueError(cloud_connection_not_connected_message(provider, connection))

    return config


def encode_onedrive_path(path_value):
    return "/".join(
        quote(part, safe="")
        for part in PurePosixPath(normalize_virtual_path(path_value)).parts
    )


def onedrive_path_endpoint(path_value="", suffix=""):
    encoded_path = encode_onedrive_path(path_value)
    if encoded_path:
        return f"/me/drive/root:/{encoded_path}:{suffix}" if suffix else f"/me/drive/root:/{encoded_path}"
    return f"/me/drive/root{suffix}"


def join_cloud_path(*parts):
    return normalize_virtual_path("/".join(str(part or "") for part in parts))


def onedrive_item_id_endpoint(item_id, suffix=""):
    return f"/me/drive/items/{quote(str(item_id), safe='')}{suffix}"


def microsoft_graph_request(connection, method, endpoint, **kwargs):
    response = None
    refreshed_connection = refresh_onedrive_token_if_needed(connection)
    provider_config = get_onedrive_provider_config(refreshed_connection)
    request_headers = dict(kwargs.pop("headers", {}) or {})
    request_timeout = kwargs.pop("timeout", provider_config["request_timeout_seconds"])
    for attempt in range(2):
        token = refreshed_connection.get("token") or {}
        headers = dict(request_headers)
        headers["Authorization"] = f"Bearer {token.get('access_token')}"
        url = endpoint if str(endpoint).startswith("http") else f"{MICROSOFT_GRAPH_BASE_URL}{endpoint}"
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=request_timeout,
            **kwargs,
        )
        if response.status_code != 401 or attempt == 1:
            break
        if response is not None:
            response.close()
        refreshed_connection = refresh_onedrive_token_if_needed(refreshed_connection, force=True)
        provider_config = get_onedrive_provider_config(refreshed_connection)

    if response.status_code >= 400:
        raise RuntimeError(f"Microsoft Graph request failed: {graph_error_message(response)}")
    return response


def microsoft_graph_json(connection, method, endpoint, **kwargs):
    response = microsoft_graph_request(connection, method, endpoint, **kwargs)
    if response.status_code == 204:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def onedrive_list_children(connection, remote_folder_path):
    endpoint = onedrive_path_endpoint(remote_folder_path, "/children")
    children = []
    while endpoint:
        payload = microsoft_graph_json(connection, "GET", endpoint)
        children.extend(payload.get("value") or [])
        endpoint = payload.get("@odata.nextLink")
    return children


def find_onedrive_child_folder(children, folder_name):
    folder_name_lower = folder_name.lower()
    for child in children:
        if child.get("name") == folder_name and isinstance(child.get("folder"), dict):
            return child
    for child in children:
        if str(child.get("name") or "").lower() == folder_name_lower and isinstance(child.get("folder"), dict):
            return child
    return None


def ensure_onedrive_remote_folder_path(connection, remote_folder_path):
    remote_folder_path = normalize_virtual_path(remote_folder_path)
    if not remote_folder_path:
        return microsoft_graph_json(connection, "GET", "/me/drive/root")

    current_path = ""
    current_item = None
    for folder_name in PurePosixPath(remote_folder_path).parts:
        children = onedrive_list_children(connection, current_path)
        current_item = find_onedrive_child_folder(children, folder_name)
        if not current_item:
            parent_endpoint = onedrive_path_endpoint(current_path, "/children")
            current_item = microsoft_graph_json(
                connection,
                "POST",
                parent_endpoint,
                json={
                    "name": folder_name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            )
        current_path = join_cloud_path(current_path, folder_name)

    return current_item


def list_onedrive_pdf_files(connection, remote_root_path):
    remote_root_path = normalize_virtual_path(remote_root_path)
    ensure_onedrive_remote_folder_path(connection, remote_root_path)

    pdf_files = []

    def walk(current_remote_path, relative_parent):
        for child in onedrive_list_children(connection, current_remote_path):
            name = str(child.get("name") or "").strip()
            if not name or name.startswith("._") or name == CLOUD_INTERNAL_FOLDER:
                continue
            child_relative_path = join_cloud_path(relative_parent, name)
            child_remote_path = join_cloud_path(current_remote_path, name)
            if isinstance(child.get("folder"), dict):
                walk(child_remote_path, child_relative_path)
                continue
            if isinstance(child.get("file"), dict) and name.lower().endswith(".pdf"):
                pdf_files.append({
                    "remote_item_id": child.get("id"),
                    "relative_path": child_relative_path,
                    "remote_path": child_remote_path,
                    "title": name,
                    "size": child.get("size"),
                    "etag": child.get("eTag"),
                    "ctag": child.get("cTag"),
                    "last_modified_at": child.get("lastModifiedDateTime"),
                })

    walk(remote_root_path, "")
    pdf_files.sort(key=lambda item: item["relative_path"].lower())
    return pdf_files


def cloud_local_document_path(root_path, relative_path):
    root_real = os.path.realpath(os.path.normpath(root_path))
    candidate = os.path.realpath(
        os.path.join(root_real, *PurePosixPath(normalize_virtual_path(relative_path)).parts)
    )
    if not path_within(root_real, candidate) or candidate == root_real:
        raise ValueError("Cloud relative path escapes the managed cloud folder")
    return candidate


def download_onedrive_file(connection, remote_item, local_path):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    response = microsoft_graph_request(
        connection,
        "GET",
        onedrive_item_id_endpoint(remote_item["remote_item_id"], "/content"),
        stream=True,
    )
    fd, temp_path = tempfile.mkstemp(
        prefix=".onedrive_download_",
        suffix=".tmp",
        dir=os.path.dirname(local_path),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        os.replace(temp_path, local_path)
    finally:
        response.close()
        if os.path.exists(temp_path):
            os.remove(temp_path)


def upload_onedrive_file_to_path(connection, remote_file_path, local_file_path, content_type="application/octet-stream"):
    remote_file_path = normalize_virtual_path(remote_file_path)
    parent_path = normalize_virtual_path(str(PurePosixPath(remote_file_path).parent))
    if parent_path == ".":
        parent_path = ""
    ensure_onedrive_remote_folder_path(connection, parent_path)

    file_size = os.path.getsize(local_file_path)
    if file_size <= MICROSOFT_GRAPH_SIMPLE_UPLOAD_LIMIT:
        with open(local_file_path, "rb") as f:
            content = f.read()
        microsoft_graph_request(
            connection,
            "PUT",
            onedrive_path_endpoint(remote_file_path, "/content"),
            data=content,
            headers={"Content-Type": content_type},
        )
        return {"remote_path": remote_file_path, "size": file_size, "method": "simple"}

    session_payload = microsoft_graph_json(
        connection,
        "POST",
        onedrive_path_endpoint(remote_file_path, "/createUploadSession"),
        json={
            "item": {
                "@microsoft.graph.conflictBehavior": "replace",
                "name": os.path.basename(remote_file_path),
            }
        },
    )
    upload_url = session_payload.get("uploadUrl")
    if not upload_url:
        raise RuntimeError("Microsoft Graph did not return an upload session URL")

    with open(local_file_path, "rb") as f:
        start = 0
        while start < file_size:
            chunk = f.read(MICROSOFT_GRAPH_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            end = start + len(chunk) - 1
            response = requests.put(
                upload_url,
                data=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                },
                timeout=get_onedrive_provider_config(connection)["request_timeout_seconds"],
            )
            if response.status_code not in {200, 201, 202}:
                raise RuntimeError(f"OneDrive upload session failed: {graph_error_message(response)}")
            start = end + 1

    return {"remote_path": remote_file_path, "size": file_size, "method": "upload_session"}


def delete_onedrive_file_by_path(connection, remote_file_path):
    try:
        microsoft_graph_request(connection, "DELETE", onedrive_path_endpoint(remote_file_path))
    except RuntimeError as e:
        if "not found" in str(e).lower() or "itemNotFound" in str(e):
            return {"remote_path": remote_file_path, "deleted": False}
        raise
    return {"remote_path": remote_file_path, "deleted": True}


def cloud_remote_item_changed(remote_item, manifest_entry):
    if not manifest_entry:
        return True
    return any(
        str(remote_item.get(remote_key) or "") != str(manifest_entry.get(manifest_key) or "")
        for remote_key, manifest_key in (
            ("remote_item_id", "remote_item_id"),
            ("etag", "etag"),
            ("ctag", "ctag"),
            ("size", "size"),
            ("last_modified_at", "last_modified_at"),
        )
    )


def google_drive_request(connection, method, endpoint, **kwargs):
    response = None
    refreshed_connection = refresh_google_drive_token_if_needed(connection)
    provider_config = get_google_drive_provider_config(refreshed_connection)
    request_headers = dict(kwargs.pop("headers", {}) or {})
    request_timeout = kwargs.pop("timeout", provider_config["request_timeout_seconds"])
    for attempt in range(2):
        token = refreshed_connection.get("token") or {}
        headers = dict(request_headers)
        headers["Authorization"] = f"Bearer {token.get('access_token')}"
        url = endpoint if str(endpoint).startswith("http") else f"{GOOGLE_DRIVE_BASE_URL}{endpoint}"
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=request_timeout,
            **kwargs,
        )
        if response.status_code != 401 or attempt == 1:
            break
        if response is not None:
            response.close()
        refreshed_connection = refresh_google_drive_token_if_needed(refreshed_connection, force=True)
        provider_config = get_google_drive_provider_config(refreshed_connection)

    if response.status_code >= 400:
        raise RuntimeError(f"Google Drive request failed: {graph_error_message(response)}")
    return response


def google_drive_json(connection, method, endpoint, **kwargs):
    response = google_drive_request(connection, method, endpoint, **kwargs)
    if response.status_code == 204:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def google_drive_query_string(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def google_drive_list_files(connection, query, fields):
    files = []
    params = {
        "q": query,
        "fields": f"nextPageToken, files({fields})",
        "pageSize": 1000,
        "spaces": "drive",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    while True:
        payload = google_drive_json(connection, "GET", "/files", params=params)
        files.extend(payload.get("files") or [])
        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            break
        params["pageToken"] = next_page_token
    return files


def find_google_drive_child_folder(connection, parent_id, folder_name):
    folder_name_query = google_drive_query_string(folder_name)
    parent_id_query = google_drive_query_string(parent_id)
    matches = google_drive_list_files(
        connection,
        f"'{parent_id_query}' in parents and name = '{folder_name_query}' and mimeType = '{GOOGLE_DRIVE_FOLDER_MIME_TYPE}' and trashed = false",
        "id, name, mimeType, modifiedTime",
    )
    for item in matches:
        if item.get("name") == folder_name:
            return item
    return matches[0] if matches else None


def google_drive_folder_path(connection, remote_folder_path, create=False):
    remote_folder_path = normalize_virtual_path(remote_folder_path)
    if not remote_folder_path:
        return {"id": "root", "name": "My Drive", "mimeType": GOOGLE_DRIVE_FOLDER_MIME_TYPE}

    current_id = "root"
    current_item = None
    for folder_name in PurePosixPath(remote_folder_path).parts:
        current_item = find_google_drive_child_folder(connection, current_id, folder_name)
        if not current_item:
            if not create:
                return None
            current_item = google_drive_json(
                connection,
                "POST",
                "/files",
                json={
                    "name": folder_name,
                    "mimeType": GOOGLE_DRIVE_FOLDER_MIME_TYPE,
                    "parents": [current_id],
                },
                params={"fields": "id, name, mimeType, modifiedTime"},
            )
        current_id = current_item.get("id")
    return current_item


def ensure_google_drive_remote_folder_path(connection, remote_folder_path):
    return google_drive_folder_path(connection, remote_folder_path, create=True)


def google_drive_list_children(connection, parent_id):
    parent_id_query = google_drive_query_string(parent_id)
    return google_drive_list_files(
        connection,
        f"'{parent_id_query}' in parents and trashed = false",
        "id, name, mimeType, size, md5Checksum, modifiedTime, version",
    )


def list_google_drive_pdf_files(connection, remote_root_path):
    remote_root_path = normalize_virtual_path(remote_root_path)
    root_item = ensure_google_drive_remote_folder_path(connection, remote_root_path)
    root_id = root_item.get("id") or "root"
    pdf_files = []

    def walk(parent_id, current_remote_path, relative_parent):
        for child in google_drive_list_children(connection, parent_id):
            name = str(child.get("name") or "").strip()
            if not name or name.startswith("._") or name == CLOUD_INTERNAL_FOLDER:
                continue
            child_relative_path = join_cloud_path(relative_parent, name)
            child_remote_path = join_cloud_path(current_remote_path, name)
            if child.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                walk(child.get("id"), child_remote_path, child_relative_path)
                continue
            if child.get("mimeType") == "application/pdf" or name.lower().endswith(".pdf"):
                pdf_files.append({
                    "remote_item_id": child.get("id"),
                    "relative_path": child_relative_path,
                    "remote_path": child_remote_path,
                    "title": name,
                    "size": child.get("size"),
                    "etag": child.get("md5Checksum"),
                    "ctag": child.get("version"),
                    "last_modified_at": child.get("modifiedTime"),
                })

    walk(root_id, remote_root_path, "")
    pdf_files.sort(key=lambda item: item["relative_path"].lower())
    return pdf_files


def download_google_drive_file(connection, remote_item, local_path):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    response = google_drive_request(
        connection,
        "GET",
        f"/files/{quote(str(remote_item['remote_item_id']), safe='')}?alt=media",
        stream=True,
    )
    fd, temp_path = tempfile.mkstemp(
        prefix=".google_drive_download_",
        suffix=".tmp",
        dir=os.path.dirname(local_path),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        os.replace(temp_path, local_path)
    finally:
        response.close()
        if os.path.exists(temp_path):
            os.remove(temp_path)


def find_google_drive_child_file(connection, parent_id, file_name):
    file_name_query = google_drive_query_string(file_name)
    parent_id_query = google_drive_query_string(parent_id)
    matches = google_drive_list_files(
        connection,
        f"'{parent_id_query}' in parents and name = '{file_name_query}' and trashed = false",
        "id, name, mimeType, size, md5Checksum, modifiedTime, version",
    )
    for item in matches:
        if item.get("name") == file_name:
            return item
    return matches[0] if matches else None


def upload_google_drive_file_to_path(connection, remote_file_path, local_file_path, content_type="application/octet-stream"):
    remote_file_path = normalize_virtual_path(remote_file_path)
    parent_path = normalize_virtual_path(str(PurePosixPath(remote_file_path).parent))
    if parent_path == ".":
        parent_path = ""
    file_name = os.path.basename(remote_file_path)
    parent_item = ensure_google_drive_remote_folder_path(connection, parent_path)
    parent_id = parent_item.get("id") or "root"
    existing_file = find_google_drive_child_file(connection, parent_id, file_name)
    file_size = os.path.getsize(local_file_path)

    metadata = {
        "name": file_name,
        "mimeType": content_type,
    }
    if existing_file:
        method = "PATCH"
        endpoint = f"{GOOGLE_DRIVE_UPLOAD_BASE_URL}/files/{quote(str(existing_file['id']), safe='')}?uploadType=resumable&fields=id,name,size,md5Checksum,modifiedTime,version"
    else:
        method = "POST"
        metadata["parents"] = [parent_id]
        endpoint = f"{GOOGLE_DRIVE_UPLOAD_BASE_URL}/files?uploadType=resumable&fields=id,name,size,md5Checksum,modifiedTime,version"

    session_response = google_drive_request(
        connection,
        method,
        endpoint,
        json=metadata,
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(file_size),
        },
    )
    upload_url = session_response.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Google Drive did not return an upload session URL")

    with open(local_file_path, "rb") as f:
        response = requests.put(
            upload_url,
            data=f,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
            },
            timeout=get_google_drive_provider_config(connection)["request_timeout_seconds"],
        )
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"Google Drive upload session failed: {graph_error_message(response)}")
    payload = response.json() if response.content else {}
    return {
        "remote_path": remote_file_path,
        "remote_item_id": payload.get("id") or (existing_file or {}).get("id"),
        "size": file_size,
        "method": "resumable",
    }


def find_google_drive_file_by_path(connection, remote_file_path):
    remote_file_path = normalize_virtual_path(remote_file_path)
    if not remote_file_path:
        return None
    parent_path = normalize_virtual_path(str(PurePosixPath(remote_file_path).parent))
    if parent_path == ".":
        parent_path = ""
    parent_item = google_drive_folder_path(connection, parent_path, create=False)
    if not parent_item:
        return None
    return find_google_drive_child_file(connection, parent_item.get("id") or "root", os.path.basename(remote_file_path))


def delete_google_drive_file_by_path(connection, remote_file_path):
    remote_file_path = normalize_virtual_path(remote_file_path)
    remote_file = find_google_drive_file_by_path(connection, remote_file_path)
    if not remote_file:
        return {"remote_path": remote_file_path, "deleted": False}
    google_drive_request(
        connection,
        "DELETE",
        f"/files/{quote(str(remote_file['id']), safe='')}",
        params={"supportsAllDrives": "true"},
    )
    return {"remote_path": remote_file_path, "deleted": True}


def dropbox_api_path(path_value):
    normalized = normalize_virtual_path(path_value)
    return f"/{normalized}" if normalized else ""


def dropbox_relative_path(path_display, remote_root_path):
    path_display = str(path_display or "").strip()
    root_path = dropbox_api_path(remote_root_path)
    if not path_display:
        return ""
    if root_path and path_display.lower().startswith(root_path.lower().rstrip("/") + "/"):
        return normalize_virtual_path(path_display[len(root_path):])
    if root_path and path_display.lower() == root_path.lower():
        return ""
    return normalize_virtual_path(path_display)


def dropbox_request(connection, method, endpoint, content=False, **kwargs):
    response = None
    refreshed_connection = refresh_dropbox_token_if_needed(connection)
    provider_config = get_dropbox_provider_config(refreshed_connection)
    request_headers = dict(kwargs.pop("headers", {}) or {})
    request_timeout = kwargs.pop("timeout", provider_config["request_timeout_seconds"])
    base_url = DROPBOX_CONTENT_BASE_URL if content else DROPBOX_API_BASE_URL
    for attempt in range(2):
        token = refreshed_connection.get("token") or {}
        headers = dict(request_headers)
        headers["Authorization"] = f"Bearer {token.get('access_token')}"
        url = endpoint if str(endpoint).startswith("http") else f"{base_url}{endpoint}"
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=request_timeout,
            **kwargs,
        )
        if response.status_code != 401 or attempt == 1:
            break
        if response is not None:
            response.close()
        refreshed_connection = refresh_dropbox_token_if_needed(refreshed_connection, force=True)
        provider_config = get_dropbox_provider_config(refreshed_connection)

    if response.status_code >= 400:
        raise RuntimeError(f"Dropbox request failed: {graph_error_message(response)}")
    return response


def dropbox_json(connection, method, endpoint, **kwargs):
    response = dropbox_request(connection, method, endpoint, **kwargs)
    if response.status_code == 204 or not response.text:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def dropbox_get_metadata(connection, remote_path):
    try:
        return dropbox_json(
            connection,
            "POST",
            "/files/get_metadata",
            json={"path": dropbox_api_path(remote_path)},
        )
    except RuntimeError as e:
        if "not_found" in str(e).lower() or "not found" in str(e).lower():
            return None
        raise


def ensure_dropbox_remote_folder_path(connection, remote_folder_path):
    remote_folder_path = normalize_virtual_path(remote_folder_path)
    if not remote_folder_path:
        return {".tag": "folder", "path_display": ""}

    current_path = ""
    current_item = None
    for folder_name in PurePosixPath(remote_folder_path).parts:
        current_path = join_cloud_path(current_path, folder_name)
        current_item = dropbox_get_metadata(connection, current_path)
        if current_item and current_item.get(".tag") == "folder":
            continue
        if current_item:
            raise ValueError(f"Dropbox path is not a folder: {current_path}")
        current_item = dropbox_json(
            connection,
            "POST",
            "/files/create_folder_v2",
            json={"path": dropbox_api_path(current_path), "autorename": False},
        ).get("metadata") or {}
    return current_item


def list_dropbox_pdf_files(connection, remote_root_path):
    remote_root_path = normalize_virtual_path(remote_root_path)
    ensure_dropbox_remote_folder_path(connection, remote_root_path)
    payload = dropbox_json(
        connection,
        "POST",
        "/files/list_folder",
        json={
            "path": dropbox_api_path(remote_root_path),
            "recursive": True,
            "include_deleted": False,
            "include_mounted_folders": True,
            "include_non_downloadable_files": False,
        },
    )
    entries = list(payload.get("entries") or [])
    while payload.get("has_more"):
        payload = dropbox_json(
            connection,
            "POST",
            "/files/list_folder/continue",
            json={"cursor": payload.get("cursor")},
        )
        entries.extend(payload.get("entries") or [])

    pdf_files = []
    for entry in entries:
        if entry.get(".tag") != "file":
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name.startswith("._") or not name.lower().endswith(".pdf"):
            continue
        relative_path = dropbox_relative_path(entry.get("path_display") or entry.get("path_lower"), remote_root_path)
        if CLOUD_INTERNAL_FOLDER in PurePosixPath(relative_path).parts:
            continue
        pdf_files.append({
            "remote_item_id": entry.get("id") or entry.get("path_lower"),
            "relative_path": relative_path,
            "remote_path": normalize_virtual_path((entry.get("path_display") or entry.get("path_lower") or "").lstrip("/")),
            "title": name,
            "size": entry.get("size"),
            "etag": entry.get("content_hash"),
            "ctag": entry.get("rev"),
            "last_modified_at": entry.get("server_modified") or entry.get("client_modified"),
        })

    pdf_files.sort(key=lambda item: item["relative_path"].lower())
    return pdf_files


def download_dropbox_file(connection, remote_item, local_path):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    remote_path = dropbox_api_path(remote_item.get("remote_path") or remote_item.get("relative_path"))
    response = dropbox_request(
        connection,
        "POST",
        "/files/download",
        content=True,
        headers={"Dropbox-API-Arg": json.dumps({"path": remote_path})},
        stream=True,
    )
    fd, temp_path = tempfile.mkstemp(
        prefix=".dropbox_download_",
        suffix=".tmp",
        dir=os.path.dirname(local_path),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        os.replace(temp_path, local_path)
    finally:
        response.close()
        if os.path.exists(temp_path):
            os.remove(temp_path)


def upload_dropbox_file_to_path(connection, remote_file_path, local_file_path, content_type="application/octet-stream"):
    remote_file_path = normalize_virtual_path(remote_file_path)
    parent_path = normalize_virtual_path(str(PurePosixPath(remote_file_path).parent))
    if parent_path == ".":
        parent_path = ""
    ensure_dropbox_remote_folder_path(connection, parent_path)
    remote_path = dropbox_api_path(remote_file_path)
    file_size = os.path.getsize(local_file_path)

    commit = {
        "path": remote_path,
        "mode": "overwrite",
        "autorename": False,
        "mute": True,
        "strict_conflict": False,
    }
    if file_size <= DROPBOX_SIMPLE_UPLOAD_LIMIT:
        with open(local_file_path, "rb") as f:
            response = dropbox_request(
                connection,
                "POST",
                "/files/upload",
                content=True,
                data=f.read(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": json.dumps(commit),
                },
            )
        payload = response.json()
        return {"remote_path": remote_file_path, "remote_item_id": payload.get("id"), "size": file_size, "method": "simple"}

    with open(local_file_path, "rb") as f:
        first_chunk = f.read(DROPBOX_UPLOAD_CHUNK_SIZE)
        payload = dropbox_request(
            connection,
            "POST",
            "/files/upload_session/start",
            content=True,
            data=first_chunk,
            headers={
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": json.dumps({"close": False}),
            },
        ).json()
        session_id = payload.get("session_id")
        if not session_id:
            raise RuntimeError("Dropbox did not return an upload session id")
        offset = len(first_chunk)
        while offset < file_size:
            chunk = f.read(DROPBOX_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            next_offset = offset + len(chunk)
            is_last = next_offset >= file_size
            endpoint = "/files/upload_session/finish" if is_last else "/files/upload_session/append_v2"
            arg = {
                "cursor": {"session_id": session_id, "offset": offset},
            }
            if is_last:
                arg["commit"] = commit
            else:
                arg["close"] = False
            response = dropbox_request(
                connection,
                "POST",
                endpoint,
                content=True,
                data=chunk,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": json.dumps(arg),
                },
            )
            offset = next_offset
        payload = response.json() if response.text else {}
    return {"remote_path": remote_file_path, "remote_item_id": payload.get("id"), "size": file_size, "method": "upload_session"}


def delete_dropbox_file_by_path(connection, remote_file_path):
    remote_file_path = normalize_virtual_path(remote_file_path)
    try:
        dropbox_json(
            connection,
            "POST",
            "/files/delete_v2",
            json={"path": dropbox_api_path(remote_file_path)},
        )
    except RuntimeError as e:
        if "not_found" in str(e).lower() or "not found" in str(e).lower():
            return {"remote_path": remote_file_path, "deleted": False}
        raise
    return {"remote_path": remote_file_path, "deleted": True}


def managed_cloud_component(value, fallback="root"):
    digest = hashlib.sha256(str(value or fallback).encode("utf-8")).hexdigest()[:16]
    return digest or fallback


def safe_cloud_connection_component(connection_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(connection_id or "connection")).strip("_")
    return (safe or "connection")[:80]


def managed_cloud_documents_root(index_name, connection_id, remote_path):
    return os.path.join(
        MANAGED_CLOUD_FOLDER,
        validate_index_name(index_name),
        safe_cloud_connection_component(connection_id),
        managed_cloud_component(remote_path),
        "documents",
    )


def provider_sync_manifest_path(root_path):
    return os.path.join(root_path, CLOUD_INTERNAL_FOLDER, "provider_sync_manifest.json")


def load_provider_sync_manifest(root_path):
    path = provider_sync_manifest_path(root_path)
    if not os.path.exists(path):
        return {"version": 1, "files": {}}
    try:
        with open(path, "r") as f:
            payload = json.load(f)
    except Exception:
        logger.warning("Unable to read provider sync manifest: %s", path, exc_info=True)
        return {"version": 1, "files": {}}
    files = payload.get("files") if isinstance(payload, dict) else {}
    return {
        "version": 1,
        "files": files if isinstance(files, dict) else {},
    }


def save_provider_sync_manifest(root_path, manifest):
    path = provider_sync_manifest_path(root_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "version": 1,
        "files": manifest.get("files") if isinstance(manifest, dict) and isinstance(manifest.get("files"), dict) else {},
        "updated_at": utc_now_iso(),
    }
    fd, temp_path = tempfile.mkstemp(
        prefix=".provider_sync_manifest_",
        suffix=".json",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=4)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return payload


def prune_empty_cloud_dirs(start_path, root_path):
    root_real = os.path.realpath(root_path)
    current = os.path.realpath(start_path)
    while path_within(root_real, current) and current != root_real:
        if os.path.basename(current) == CLOUD_INTERNAL_FOLDER:
            return
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def sync_onedrive_connection_folder(index_name, config):
    cloud = config.get("cloud") or {}
    connection = get_connected_onedrive_connection(cloud.get("connection_id"))
    remote_root_path = normalize_virtual_path(cloud.get("remote_path") or "")
    root_path = cloud.get("root_path") or managed_cloud_documents_root(index_name, connection["id"], remote_root_path)
    os.makedirs(root_path, exist_ok=True)

    remote_files = list_onedrive_pdf_files(connection, remote_root_path)
    remote_by_relative_path = {
        item["relative_path"]: item
        for item in remote_files
    }
    manifest = load_provider_sync_manifest(root_path)
    manifest_files = manifest.setdefault("files", {})

    downloaded_files = []
    removed_local_files = []
    unchanged_files = []

    for relative_path, manifest_entry in list(manifest_files.items()):
        if relative_path in remote_by_relative_path:
            continue
        local_path = cloud_local_document_path(root_path, relative_path)
        if os.path.exists(local_path):
            os.remove(local_path)
            prune_empty_cloud_dirs(os.path.dirname(local_path), root_path)
        manifest_files.pop(relative_path, None)
        removed_local_files.append(relative_path)

    for remote_item in remote_files:
        relative_path = remote_item["relative_path"]
        local_path = cloud_local_document_path(root_path, relative_path)
        manifest_entry = manifest_files.get(relative_path)
        if (
            os.path.exists(local_path)
            and not cloud_remote_item_changed(remote_item, manifest_entry)
        ):
            unchanged_files.append(relative_path)
            continue

        download_onedrive_file(connection, remote_item, local_path)
        manifest_files[relative_path] = {
            "remote_item_id": remote_item.get("remote_item_id"),
            "remote_path": remote_item.get("remote_path"),
            "etag": remote_item.get("etag"),
            "ctag": remote_item.get("ctag"),
            "size": remote_item.get("size"),
            "last_modified_at": remote_item.get("last_modified_at"),
            "downloaded_at": utc_now_iso(),
        }
        downloaded_files.append(relative_path)

    save_provider_sync_manifest(root_path, manifest)
    return {
        "provider": ONEDRIVE_CLOUD_PROVIDER,
        "remote_path": remote_root_path,
        "local_root_path": root_path,
        "checked_remote_files": len(remote_files),
        "downloaded_files": downloaded_files,
        "removed_local_files": removed_local_files,
        "unchanged_files": unchanged_files,
    }


def sync_api_provider_connection_folder(index_name, config, provider, list_remote_files, download_remote_file):
    cloud = config.get("cloud") or {}
    connection = get_connected_provider_connection(provider, cloud.get("connection_id"))
    remote_root_path = normalize_virtual_path(cloud.get("remote_path") or "")
    root_path = cloud.get("root_path") or managed_cloud_documents_root(index_name, connection["id"], remote_root_path)
    os.makedirs(root_path, exist_ok=True)

    remote_files = list_remote_files(connection, remote_root_path)
    remote_by_relative_path = {
        item["relative_path"]: item
        for item in remote_files
    }
    manifest = load_provider_sync_manifest(root_path)
    manifest_files = manifest.setdefault("files", {})

    downloaded_files = []
    removed_local_files = []
    unchanged_files = []

    for relative_path, manifest_entry in list(manifest_files.items()):
        if relative_path in remote_by_relative_path:
            continue
        local_path = cloud_local_document_path(root_path, relative_path)
        if os.path.exists(local_path):
            os.remove(local_path)
            prune_empty_cloud_dirs(os.path.dirname(local_path), root_path)
        manifest_files.pop(relative_path, None)
        removed_local_files.append(relative_path)

    for remote_item in remote_files:
        relative_path = remote_item["relative_path"]
        local_path = cloud_local_document_path(root_path, relative_path)
        manifest_entry = manifest_files.get(relative_path)
        if (
            os.path.exists(local_path)
            and not cloud_remote_item_changed(remote_item, manifest_entry)
        ):
            unchanged_files.append(relative_path)
            continue

        download_remote_file(connection, remote_item, local_path)
        manifest_files[relative_path] = {
            "remote_item_id": remote_item.get("remote_item_id"),
            "remote_path": remote_item.get("remote_path"),
            "etag": remote_item.get("etag"),
            "ctag": remote_item.get("ctag"),
            "size": remote_item.get("size"),
            "last_modified_at": remote_item.get("last_modified_at"),
            "downloaded_at": utc_now_iso(),
        }
        downloaded_files.append(relative_path)

    save_provider_sync_manifest(root_path, manifest)
    return {
        "provider": provider,
        "remote_path": remote_root_path,
        "local_root_path": root_path,
        "checked_remote_files": len(remote_files),
        "downloaded_files": downloaded_files,
        "removed_local_files": removed_local_files,
        "unchanged_files": unchanged_files,
    }


def sync_google_drive_connection_folder(index_name, config):
    return sync_api_provider_connection_folder(
        index_name,
        config,
        GOOGLE_DRIVE_CLOUD_PROVIDER,
        list_google_drive_pdf_files,
        download_google_drive_file,
    )


def sync_dropbox_connection_folder(index_name, config):
    return sync_api_provider_connection_folder(
        index_name,
        config,
        DROPBOX_CLOUD_PROVIDER,
        list_dropbox_pdf_files,
        download_dropbox_file,
    )


def sync_cloud_provider_to_local(index_name, config):
    cloud = config.get("cloud") or {}
    if cloud.get("provider") == ONEDRIVE_CLOUD_PROVIDER:
        return sync_onedrive_connection_folder(index_name, config)
    if cloud.get("provider") == GOOGLE_DRIVE_CLOUD_PROVIDER:
        return sync_google_drive_connection_folder(index_name, config)
    if cloud.get("provider") == DROPBOX_CLOUD_PROVIDER:
        return sync_dropbox_connection_folder(index_name, config)
    return None


def upload_provider_file_to_path(provider, connection, remote_file_path, local_path, content_type="application/octet-stream"):
    if provider == ONEDRIVE_CLOUD_PROVIDER:
        return upload_onedrive_file_to_path(connection, remote_file_path, local_path, content_type=content_type)
    if provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
        return upload_google_drive_file_to_path(connection, remote_file_path, local_path, content_type=content_type)
    if provider == DROPBOX_CLOUD_PROVIDER:
        return upload_dropbox_file_to_path(connection, remote_file_path, local_path, content_type=content_type)
    return None


def delete_provider_file_by_path(provider, connection, remote_file_path):
    if provider == ONEDRIVE_CLOUD_PROVIDER:
        return delete_onedrive_file_by_path(connection, remote_file_path)
    if provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
        return delete_google_drive_file_by_path(connection, remote_file_path)
    if provider == DROPBOX_CLOUD_PROVIDER:
        return delete_dropbox_file_by_path(connection, remote_file_path)
    return None


def ensure_provider_remote_folder_path(provider, connection, remote_folder_path):
    if provider == ONEDRIVE_CLOUD_PROVIDER:
        return ensure_onedrive_remote_folder_path(connection, remote_folder_path)
    if provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
        return ensure_google_drive_remote_folder_path(connection, remote_folder_path)
    if provider == DROPBOX_CLOUD_PROVIDER:
        return ensure_dropbox_remote_folder_path(connection, remote_folder_path)
    raise ValueError("Unsupported cloud provider")


def sync_cloud_provider_document_update(index_name, metadata, local_path):
    if not metadata or metadata.get("source") != "cloud":
        return None
    config = load_index_config(index_name)
    cloud = config.get("cloud") or {}
    provider = cloud.get("provider")
    if provider not in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
        return None

    remote_file_path = join_cloud_path(cloud.get("remote_path"), metadata.get("cloud_relative_path"))
    if not remote_file_path:
        raise ValueError("Cloud document has no remote path")
    connection = get_connected_provider_connection(provider, cloud.get("connection_id"))
    return upload_provider_file_to_path(provider, connection, remote_file_path, local_path, content_type="application/pdf")


def delete_cloud_provider_document(index_name, metadata):
    if not metadata or metadata.get("source") != "cloud":
        return None
    config = load_index_config(index_name)
    cloud = config.get("cloud") or {}
    provider = cloud.get("provider")
    if provider not in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
        return None

    remote_file_path = join_cloud_path(cloud.get("remote_path"), metadata.get("cloud_relative_path"))
    if not remote_file_path:
        return None
    connection = get_connected_provider_connection(provider, cloud.get("connection_id"))
    return delete_provider_file_by_path(provider, connection, remote_file_path)


def sync_low_data_preview_to_provider(index_name, config, cache_path):
    cloud = config.get("cloud") or {}
    provider = cloud.get("provider")
    if provider not in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
        return None

    preview_folder = cloud_preview_folder(index_name, config)
    if not path_within(preview_folder, cache_path):
        raise ValueError("Low-data preview cache is outside the configured cloud preview folder")
    relative_preview_path = normalize_virtual_path(os.path.relpath(cache_path, preview_folder))
    if not relative_preview_path:
        raise ValueError("Low-data preview cache is outside the configured cloud preview folder")

    remote_preview_path = join_cloud_path(
        cloud.get("remote_path"),
        CLOUD_INTERNAL_FOLDER,
        "previews",
        validate_index_name(index_name),
        relative_preview_path,
    )
    connection = get_connected_provider_connection(provider, cloud.get("connection_id"))
    return upload_provider_file_to_path(provider, connection, remote_preview_path, cache_path, content_type="image/jpeg")


def prepare_index_cloud_config_for_save(index_name, config):
    mode = config.get("mode")
    cloud = config.get("cloud") or {}
    if mode not in {"cloud", "hybrid"}:
        return config

    provider = cloud.get("provider") or DEFAULT_CLOUD_PROVIDER
    ensure_cloud_provider_usable_for_principal(provider)
    if provider in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
        ensure_cloud_connection_usable_for_index(index_name, cloud.get("connection_id"))
        connection = get_connected_provider_connection(provider, cloud.get("connection_id"))
        remote_path = normalize_virtual_path(cloud.get("remote_path") or "")
        root_path = managed_cloud_documents_root(index_name, connection["id"], remote_path)
        preview_folder = cloud.get("preview_folder") or ""
        if preview_folder and not path_within(root_path, preview_folder):
            preview_folder = ""
        cloud["root_path"] = root_path
        cloud["managed_root_path"] = root_path
        cloud["remote_path"] = remote_path
        cloud["preview_folder"] = preview_folder or os.path.join(
            root_path,
            CLOUD_INTERNAL_FOLDER,
            "previews",
            validate_index_name(index_name),
        )
        if not cloud.get("display_root_name"):
            cloud["display_root_name"] = os.path.basename(remote_path) if remote_path else provider_display_name(provider)
        os.makedirs(root_path, exist_ok=True)
        ensure_provider_remote_folder_path(provider, connection, remote_path)
        ensure_provider_remote_folder_path(
            provider,
            connection,
            join_cloud_path(remote_path, CLOUD_INTERNAL_FOLDER, "previews", validate_index_name(index_name)),
        )

    config["cloud"] = cloud
    return config


def validate_index_config_for_save(config):
    mode = config.get("mode")
    cloud = config.get("cloud") or {}
    if mode not in VALID_INDEX_MODES:
        raise ValueError("Invalid index mode")

    if mode in {"cloud", "hybrid"}:
        if cloud.get("provider") not in VALID_CLOUD_PROVIDERS:
            raise ValueError("Unsupported cloud provider")

        root_path = cloud.get("root_path")
        if not root_path:
            raise ValueError("Cloud root path is required for cloud and hybrid modes")
        if not os.path.isdir(root_path):
            raise ValueError(f"Cloud root path does not exist or is not a folder: {root_path}")
        if cloud.get("provider") in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
            if not cloud.get("connection_id"):
                raise ValueError(f"{provider_display_name(cloud.get('provider'))} cloud mode requires a connected account")
            connection = get_cloud_connection(cloud.get("connection_id"))
            if connection is None:
                raise ValueError(f"Selected {provider_display_name(cloud.get('provider'))} connection no longer exists")
            if connection.get("provider") != cloud.get("provider"):
                raise ValueError("Selected cloud account does not match this provider")
            if not path_within(MANAGED_CLOUD_FOLDER, root_path):
                raise ValueError(f"{provider_display_name(cloud.get('provider'))} managed folder must stay inside MANAGED_CLOUD_FOLDER")
        for local_store in (PDF_FOLDER, INDEX_FOLDER, TMP_FOLDER, DELETED_FOLDER):
            if path_within(local_store, root_path):
                raise ValueError("Cloud root path must not be inside PDFSearch system storage")

        preview_folder = cloud.get("preview_folder")
        if preview_folder and not path_within(root_path, preview_folder):
            raise ValueError("Cloud preview folder must be inside the configured cloud root")


def save_index_config(index_name, requested_config, validate=True):
    index_name = validate_index_name(index_name)
    requested_config = requested_config if isinstance(requested_config, dict) else {}
    existing_config = load_index_config(index_name)
    normalized = normalize_index_config(index_name, requested_config, existing_config)
    now = utc_now_iso()
    normalized["created_at"] = normalized.get("created_at") or existing_config.get("created_at") or now
    normalized["updated_at"] = now
    if validate:
        normalized = prepare_index_cloud_config_for_save(index_name, normalized)
        validate_index_config_for_save(normalized)

    os.makedirs(get_index_folder_path(index_name), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".index_config_",
        suffix=".json",
        dir=get_index_folder_path(index_name),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(normalized, f, indent=4)
        os.replace(temp_path, get_index_config_path(index_name))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return normalized


def update_index_cloud_sync_state(index_name, status, message, task_id=None):
    config = load_index_config(index_name)
    cloud = dict(config.get("cloud") or {})
    cloud.update({
        "last_sync_at": utc_now_iso(),
        "last_sync_status": status,
        "last_sync_message": message,
    })
    if task_id:
        cloud["last_sync_task_id"] = task_id
    config["cloud"] = cloud
    return save_index_config(index_name, config, validate=False)


def index_has_cloud_source(index_name, config=None):
    config = config or load_index_config(index_name)
    return config.get("mode") in {"cloud", "hybrid"} and bool(
        (config.get("cloud") or {}).get("root_path")
    )


def index_accepts_uploads(index_name, config=None):
    config = config or load_index_config(index_name)
    return config.get("mode") in {"standalone", "hybrid"}


def ensure_index_accepts_uploads(index_name):
    if not index_accepts_uploads(index_name):
        raise ValueError("Uploads are disabled for cloud-only indices. Switch to hybrid mode to upload documents.")


def cloud_display_root_name(index_name, config=None):
    config = config or load_index_config(index_name)
    cloud = config.get("cloud") or {}
    if cloud.get("display_root_name"):
        return cloud["display_root_name"]
    if cloud.get("provider") in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
        remote_path = normalize_virtual_path(cloud.get("remote_path") or "")
        return os.path.basename(remote_path) if remote_path else provider_display_name(cloud.get("provider"))
    root_name = os.path.basename(os.path.normpath(cloud.get("root_path") or ""))
    return root_name or "Cloud documents"


def cloud_preview_folder(index_name, config=None):
    config = config or load_index_config(index_name)
    cloud = config.get("cloud") or {}
    root_path = cloud.get("root_path") or ""
    if cloud.get("preview_folder"):
        return cloud["preview_folder"]
    if not root_path:
        return ""
    return os.path.join(root_path, CLOUD_INTERNAL_FOLDER, "previews", validate_index_name(index_name))


def build_standalone_virtual_path(title):
    return normalize_virtual_path(f"{UPLOADS_VIRTUAL_FOLDER}/{title or 'document.pdf'}")


def build_cloud_virtual_path(index_name, relative_path, config=None):
    return normalize_virtual_path(f"{cloud_display_root_name(index_name, config)}/{relative_path}")


def file_stat_payload(file_path):
    try:
        stat = os.stat(file_path)
        return {
            "size": int(stat.st_size),
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
            "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
        }
    except OSError:
        return {
            "size": None,
            "modified_at": None,
            "mtime_ns": None,
        }


def metadata_file_stat_matches(metadata, stat_payload):
    if not metadata or not stat_payload:
        return False
    if stat_payload.get("size") is None:
        return False
    try:
        if int(metadata.get("size")) != int(stat_payload.get("size")):
            return False
    except (TypeError, ValueError):
        if str(metadata.get("size") or "") != str(stat_payload.get("size") or ""):
            return False

    metadata_mtime_ns = metadata.get("mtime_ns")
    stat_mtime_ns = stat_payload.get("mtime_ns")
    if metadata_mtime_ns is not None and stat_mtime_ns is not None:
        try:
            return int(metadata_mtime_ns) == int(stat_mtime_ns)
        except (TypeError, ValueError):
            return str(metadata_mtime_ns) == str(stat_mtime_ns)

    return bool(metadata.get("modified_at")) and metadata.get("modified_at") == stat_payload.get("modified_at")


def update_metadata_file_stat(metadata, stat_payload):
    changed = False
    for key in ("size", "modified_at", "mtime_ns"):
        if metadata.get(key) != stat_payload.get(key):
            metadata[key] = stat_payload.get(key)
            changed = True
    return changed


def calculate_file_hash(file_path, chunk_size=1024 * 1024):
    hash_sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()


def make_cloud_document_id(index_name, relative_path):
    identity = f"cloud\0{validate_index_name(index_name)}\0{normalize_virtual_path(relative_path)}"
    return "cloud-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def default_document_metadata_payload():
    return {
        "version": 1,
        "documents": {},
    }


def normalize_document_metadata_entry(index_name, raw_entry):
    raw_entry = raw_entry if isinstance(raw_entry, dict) else {}
    doc_id = str(raw_entry.get("id") or "").strip()
    if not doc_id:
        return None

    path_value = str(raw_entry.get("path") or "").strip()
    title = str(raw_entry.get("title") or os.path.basename(path_value) or "document.pdf")
    source = str(raw_entry.get("source") or "standalone").strip().lower()
    if source not in {"standalone", "cloud"}:
        source = "standalone"

    cloud_relative_path = normalize_virtual_path(raw_entry.get("cloud_relative_path") or "")
    virtual_path = normalize_virtual_path(raw_entry.get("virtual_path") or "")
    if not virtual_path:
        if source == "cloud" and cloud_relative_path:
            virtual_path = build_cloud_virtual_path(index_name, cloud_relative_path)
        else:
            virtual_path = build_standalone_virtual_path(title)

    stat_payload = file_stat_payload(path_value) if path_value else {
        "size": None,
        "modified_at": None,
        "mtime_ns": None,
    }

    return {
        "id": doc_id,
        "title": title,
        "path": path_value,
        "source": source,
        "virtual_path": virtual_path,
        "cloud_relative_path": cloud_relative_path,
        "size": raw_entry.get("size", stat_payload["size"]),
        "modified_at": raw_entry.get("modified_at") or stat_payload["modified_at"],
        "mtime_ns": raw_entry.get("mtime_ns", stat_payload["mtime_ns"]),
        "content_hash": raw_entry.get("content_hash") or (doc_id if source == "standalone" else ""),
        "file_hash": raw_entry.get("file_hash") or "",
        "indexed_at": raw_entry.get("indexed_at"),
        "years": raw_entry.get("years") or [],
    }


def load_document_metadata(index_name):
    validate_index_name(index_name)
    metadata_path = get_document_metadata_path(index_name)
    raw_payload = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r") as f:
                raw_payload = json.load(f)
        except Exception:
            logger.warning("Unable to read document metadata for %s", index_name, exc_info=True)

    raw_documents = raw_payload.get("documents") if isinstance(raw_payload, dict) else {}
    if isinstance(raw_documents, list):
        raw_documents = {
            str(entry.get("id")): entry
            for entry in raw_documents
            if isinstance(entry, dict) and entry.get("id")
        }
    elif not isinstance(raw_documents, dict):
        raw_documents = {}

    documents = {}
    for doc_id, raw_entry in raw_documents.items():
        if isinstance(raw_entry, dict):
            raw_entry = dict(raw_entry)
            raw_entry.setdefault("id", doc_id)
        normalized = normalize_document_metadata_entry(index_name, raw_entry)
        if normalized:
            documents[normalized["id"]] = normalized

    return {
        "version": 1,
        "documents": documents,
    }


def save_document_metadata(index_name, metadata_payload):
    index_name = validate_index_name(index_name)
    payload = metadata_payload if isinstance(metadata_payload, dict) else default_document_metadata_payload()
    normalized_documents = {}
    for doc_id, entry in (payload.get("documents") or {}).items():
        if isinstance(entry, dict):
            entry = dict(entry)
            entry.setdefault("id", doc_id)
        normalized = normalize_document_metadata_entry(index_name, entry)
        if normalized:
            normalized_documents[normalized["id"]] = normalized

    payload = {
        "version": 1,
        "documents": normalized_documents,
    }

    os.makedirs(get_index_folder_path(index_name), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".document_metadata_",
        suffix=".json",
        dir=get_index_folder_path(index_name),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=4)
        os.replace(temp_path, get_document_metadata_path(index_name))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return payload


def build_document_metadata_entry(
    index_name,
    index_document,
    source="standalone",
    cloud_relative_path="",
    virtual_path="",
    content_hash=None,
    file_hash=None,
    file_info_path=None,
):
    stored_path = index_document.get("path") or file_info_path or ""
    title = index_document.get("title") or os.path.basename(stored_path) or "document.pdf"
    stat_payload = file_stat_payload(file_info_path or stored_path)
    years_value = index_document.get("years") or ""
    if isinstance(years_value, str):
        years = [year for year in years_value.split(",") if year]
    else:
        years = years_value if isinstance(years_value, list) else []

    source = source if source in {"standalone", "cloud"} else "standalone"
    cloud_relative_path = normalize_virtual_path(cloud_relative_path)
    if not virtual_path:
        virtual_path = (
            build_cloud_virtual_path(index_name, cloud_relative_path)
            if source == "cloud" and cloud_relative_path else
            build_standalone_virtual_path(title)
        )

    return {
        "id": index_document["id"],
        "title": title,
        "path": stored_path,
        "source": source,
        "virtual_path": normalize_virtual_path(virtual_path),
        "cloud_relative_path": cloud_relative_path,
        "size": stat_payload["size"],
        "modified_at": stat_payload["modified_at"],
        "mtime_ns": stat_payload["mtime_ns"],
        "content_hash": content_hash if content_hash is not None else index_document.get("content_hash", ""),
        "file_hash": file_hash or "",
        "indexed_at": utc_now_iso(),
        "years": years,
    }


def infer_metadata_from_index_document(index_name, index_document):
    doc_id = index_document.get("id")
    if not doc_id:
        return None

    path_value = index_document.get("path") or ""
    title = index_document.get("title") or os.path.basename(path_value) or "document.pdf"
    source = "standalone"
    cloud_relative_path = ""
    config = load_index_config(index_name)
    cloud_root = (config.get("cloud") or {}).get("root_path")
    if cloud_root and path_within(cloud_root, path_value):
        source = "cloud"
        cloud_relative_path = normalize_virtual_path(os.path.relpath(path_value, cloud_root))

    file_hash = ""
    if path_value and os.path.exists(path_value):
        try:
            file_hash = calculate_file_hash(path_value)
        except Exception:
            logger.debug("Unable to calculate file hash for %s", path_value, exc_info=True)

    return build_document_metadata_entry(
        index_name,
        {
            "id": doc_id,
            "title": title,
            "path": path_value,
            "years": index_document.get("years") or "",
            "content_hash": doc_id if source == "standalone" else "",
        },
        source=source,
        cloud_relative_path=cloud_relative_path,
        file_hash=file_hash,
        file_info_path=path_value,
    )


def ensure_document_metadata(index_name, ix=None):
    payload = load_document_metadata(index_name)
    documents = payload["documents"]
    changed = False

    try:
        if ix is None:
            ix, _ = create_or_open_index(index_name, check_access=False)
        indexed_ids = set()
        with ix.searcher() as searcher:
            for stored_doc in searcher.documents():
                doc_id = stored_doc.get("id")
                if not doc_id:
                    continue
                indexed_ids.add(doc_id)
                if doc_id not in documents:
                    inferred = infer_metadata_from_index_document(index_name, stored_doc)
                    if inferred:
                        documents[doc_id] = inferred
                        changed = True
                else:
                    existing = dict(documents[doc_id])
                    for field in ("title", "path", "years"):
                        if stored_doc.get(field) and existing.get(field) != stored_doc.get(field):
                            existing[field] = stored_doc.get(field)
                            changed = True
                    documents[doc_id] = normalize_document_metadata_entry(index_name, existing)

        stale_ids = [doc_id for doc_id in documents if doc_id not in indexed_ids]
        for doc_id in stale_ids:
            documents.pop(doc_id, None)
            changed = True
    except Exception:
        logger.debug("Unable to reconcile document metadata for %s", index_name, exc_info=True)

    if changed:
        payload = save_document_metadata(index_name, payload)
    return payload


def upsert_document_metadata(index_name, metadata_entry):
    payload = load_document_metadata(index_name)
    payload.setdefault("documents", {})[metadata_entry["id"]] = metadata_entry
    return save_document_metadata(index_name, payload)


def remove_document_metadata(index_name, doc_id):
    payload = load_document_metadata(index_name)
    if doc_id in payload.get("documents", {}):
        payload["documents"].pop(doc_id, None)
        save_document_metadata(index_name, payload)


def get_document_metadata_entry(index_name, doc_id):
    payload = ensure_document_metadata(index_name)
    return payload.get("documents", {}).get(doc_id)


def find_document_metadata_by_path(pdf_path, accessible_indices=None):
    full_path = os.path.realpath(os.path.normpath(unquote(pdf_path or "")))
    candidate_indices = discover_indices() if accessible_indices is None else accessible_indices
    for index_name in candidate_indices:
        try:
            payload = ensure_document_metadata(index_name)
            for doc in payload.get("documents", {}).values():
                if doc.get("path") and os.path.realpath(os.path.normpath(doc["path"])) == full_path:
                    return index_name, doc
        except Exception:
            logger.debug("Unable to inspect metadata while resolving %s", full_path, exc_info=True)
    return None, None


TEST_BYPASS_MIN_TOKEN_LENGTH = 32


def test_bypass_enabled():
    """The integration-test bypass exists only in an explicitly isolated test stack."""
    token = os.environ.get("TEST_BYPASS_TOKEN") or ""
    test_mode = os.environ.get("PDFSEARCH_TEST_MODE", "").strip().lower() == "true"
    return test_mode and len(token) >= TEST_BYPASS_MIN_TOKEN_LENGTH


def is_test_bypass_request():
    if not test_bypass_enabled():
        return False
    supplied = request.headers.get("X-Test-Bypass") or ""
    expected = os.environ.get("TEST_BYPASS_TOKEN") or ""
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def mail_token_matches(supplied):
    return MAIL_TOKEN not in PUBLIC_MAIL_TOKENS and bool(supplied) and hmac.compare_digest(
        supplied.encode(), MAIL_TOKEN.encode())


def warn_about_insecure_test_settings():
    if os.environ.get("TEST_BYPASS_TOKEN") and not test_bypass_enabled():
        logger.warning("TEST_BYPASS_TOKEN is set but ignored: the bypass requires PDFSEARCH_TEST_MODE=true "
                       "and a token of at least %d characters in an isolated test stack.",
                       TEST_BYPASS_MIN_TOKEN_LENGTH)
    elif test_bypass_enabled():
        logger.warning("PDFSEARCH_TEST_MODE is enabled: authorization bypass is active for integration tests. "
                       "Never enable it for a real installation.")
    if MAIL_TOKEN in PUBLIC_MAIL_TOKENS:
        logger.warning("Mail ingestion is disabled until MAIL_TOKEN is set to a private value.")


def log_request_client_activity(principal):
    peer = request.remote_addr or "unknown"
    if principal["type"] == "tailscale":
        key = f"tailscale:{principal['login']}"
        display = f"Tailscale user {principal['login']} ({peer})"
    else:
        key = f"{principal['type']}:{peer}"
        display = f"{principal['type'].capitalize()} client ({peer})"
    now = time.monotonic()
    with request_client_lock:
        for previous_key, state in list(request_clients.items()):
            idle_seconds = now - state.get("last_seen", now)
            if idle_seconds >= REQUEST_CLIENT_IDLE_SECONDS:
                logger.info("Client disconnected: %s after %.0fs idle", state["display"], idle_seconds)
                request_clients.pop(previous_key, None)

        previous = request_clients.get(key)
        if not previous:
            logger.info("Client connected: %s via %s %s", display, request.method, request.path)
        elif previous["display"] != display:
            logger.info("Client identity updated: %s", display)

        request_clients[key] = {"display": display, "last_seen": now}


def get_initial_admin_logins():
    raw_logins = os.environ.get("ADMIN_TAILSCALE_USERS", "")
    return {
        normalized
        for normalized in (normalize_login(login) for login in raw_logins.split(","))
        if normalized
    }


def unique_valid_index_names(index_names):
    unique = []
    seen = set()
    for index_name in index_names or []:
        try:
            valid_name = validate_index_name(str(index_name).strip())
        except (ValueError, TypeError):
            continue
        if valid_name not in seen:
            unique.append(valid_name)
            seen.add(valid_name)
    return unique


def discover_indices():
    index_names = []
    reserved_index_folder_names = {"uploads"}

    for root_folder in [INDEX_FOLDER, PDF_FOLDER]:
        try:
            if os.path.isdir(root_folder):
                index_names.extend(
                    item
                    for item in os.listdir(root_folder)
                    if (
                        item not in reserved_index_folder_names
                        and os.path.isdir(os.path.join(root_folder, item))
                    )
                )
        except OSError:
            logger.debug("Unable to discover indices in %s", root_folder, exc_info=True)

    return unique_valid_index_names(index_names)


def access_index_names(raw_indices):
    if not isinstance(raw_indices, list) or not all(isinstance(name, str) for name in raw_indices):
        raise ValueError("Index names must be listed as strings")
    return list(dict.fromkeys(validate_index_name(name.strip()) for name in raw_indices))


def normalize_user_permissions(raw_permissions):
    if isinstance(raw_permissions, list):
        raw_permissions = {"read": raw_permissions}
    if not isinstance(raw_permissions, dict):
        raise ValueError("User permissions must be an object or a list of readable indexes")

    is_admin = raw_permissions.get("admin", False)
    if not isinstance(is_admin, bool):
        raise ValueError("User admin permission must be true or false")
    read_indices = access_index_names(raw_permissions.get("read", []))
    write_indices = access_index_names(raw_permissions.get("write", []))

    return {
        "admin": is_admin,
        "read": list(dict.fromkeys(read_indices + write_indices)),
        "write": write_indices,
    }


def normalize_access_config(raw_config, include_discovered=True, strict=False):
    if not isinstance(raw_config, dict):
        raise ValueError("Access configuration must be an object")

    raw_users = raw_config.get("users", {})
    if not isinstance(raw_users, dict):
        raise ValueError("Access configuration users must be an object")
    index_names = dict.fromkeys(access_index_names(raw_config.get("indices", [])))
    if include_discovered:
        index_names.update(dict.fromkeys(discover_indices()))

    users = {}
    for login, raw_permissions in raw_users.items():
        normalized_login = normalize_login(login)
        if not normalized_login or normalized_login in users:
            raise ValueError("Access configuration requires one entry per valid Tailscale login")

        permissions = normalize_user_permissions(raw_permissions)
        if strict:
            unknown_indices = sorted(set(permissions["read"]).difference(index_names))
            if unknown_indices:
                raise ValueError(
                    f"Unknown index permission for {normalized_login}: {', '.join(unknown_indices)}"
                )
        else:
            index_names.update(dict.fromkeys(permissions["read"]))

        users[normalized_login] = permissions

    return {
        "indices": list(index_names),
        "users": users
    }


def read_access_config():
    try:
        with open(ACCESS_CONFIG_PATH, encoding="utf-8") as f:
            raw_config = json.load(f, object_pairs_hook=unique_access_config_fields)
    except FileNotFoundError:
        raw_config = {}
    return normalize_access_config(raw_config)


def unique_access_config_fields(pairs):
    config = dict(pairs)
    if len(config) != len(pairs):
        raise ValueError("Access configuration has duplicate fields")
    return config


def load_access_config():
    if not has_request_context():
        return read_access_config()
    if "pdfsearch.access_config" not in request.environ:
        request.environ["pdfsearch.access_config"] = read_access_config()
    return request.environ["pdfsearch.access_config"]


def save_access_config(config):
    normalized = normalize_access_config(config, include_discovered=False, strict=True)
    config_dir = os.path.dirname(ACCESS_CONFIG_PATH) or "."
    os.makedirs(config_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".access_config_", suffix=".json", dir=config_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=4)
        os.replace(temp_path, ACCESS_CONFIG_PATH)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return normalized


def resolve_request_principal():
    if is_test_bypass_request():
        kind, login = "test", "test-bypass"
    elif (request.endpoint == "add_document" and request.method == "POST"
            and "X-Mail-Token" in request.headers):
        if ("Origin" in request.headers or has_proxy_headers(request.headers)
                or not mail_token_matches(request.headers["X-Mail-Token"])):
            return None
        kind, login = "mail", ""
    else:
        identity = browser_identity(request, app.config)
        if identity is None:
            return None
        kind, login = identity

    if (request.endpoint, request.method) in {("manage_access_config", "PUT"), ("create_admin_index", "POST")}:
        # Serialize permission changes from the snapshot through the write.
        access_config_lock.acquire()
        request.environ["pdfsearch.access_config_locked"] = True

    config = load_access_config()
    permissions = config["users"].get(login, {})
    is_admin = kind in {"test", "local"} or (kind == "tailscale" and (
        permissions.get("admin", False) or login in get_initial_admin_logins()
    ))
    return {
        "type": kind,
        "id": login or kind,
        "login": login,
        "role": "admin" if is_admin else "user",
        "is_admin": is_admin,
        "read": config["indices"] if is_admin else permissions.get("read", []),
        "write": config["indices"] if is_admin or kind == "mail" else permissions.get("write", []),
    }


def get_current_principal():
    if "pdfsearch.principal" not in request.environ:
        request.environ["pdfsearch.principal"] = resolve_request_principal()
    return request.environ["pdfsearch.principal"]


def get_accessible_indices(action="read"):
    principal = get_current_principal() if has_request_context() else None
    return principal[action] if principal else []


def ensure_admin_access():
    if has_request_context() and not (get_current_principal() or {}).get("is_admin"):
        raise PermissionError("Admin access required")
    return True


def ensure_index_access(index_name, action="read"):
    validate_index_name(index_name)

    if has_request_context():
        if action not in {"read", "write"}:
            raise ValueError("Invalid access action")
        principal = get_current_principal()
        if not principal or (principal["type"] != "test" and index_name not in principal[action]):
            raise PermissionError(f"Access denied to index '{index_name}'")

    return True


def resolve_index_pdf_path(pdf_path):
    if not pdf_path:
        raise ValueError("Missing file path")

    full_path = os.path.realpath(os.path.normpath(unquote(pdf_path)))
    pdf_root = os.path.realpath(PDF_FOLDER)

    if path_within(pdf_root, full_path):
        relative_path = os.path.relpath(full_path, pdf_root)
        parts = Path(relative_path).parts
        if len(parts) < 2:
            raise ValueError("PDF path must include an index folder and filename")

        index_name = validate_index_name(parts[0])
        ensure_index_access(index_name)

        if not os.path.exists(full_path):
            raise FileNotFoundError("PDF not found")

        return full_path, index_name

    accessible_indices = get_accessible_indices()
    index_name, metadata = find_document_metadata_by_path(full_path, accessible_indices)
    if not index_name or not metadata or metadata.get("source") != "cloud":
        raise PermissionError("Path is outside configured PDFSearch document sources")

    ensure_index_access(index_name)
    if not os.path.exists(full_path):
        raise FileNotFoundError("PDF not found")

    return full_path, index_name


def validate_pdf_path_for_index(pdf_path, index_name):
    if not pdf_path:
        raise ValueError("Missing file path")

    index_root = os.path.realpath(os.path.join(PDF_FOLDER, validate_index_name(index_name)))
    full_path = os.path.realpath(os.path.normpath(pdf_path))

    if not path_within(index_root, full_path):
        config = load_index_config(index_name)
        cloud_root = (config.get("cloud") or {}).get("root_path")
        if not index_has_cloud_source(index_name, config) or not path_within(cloud_root, full_path):
            raise PermissionError("PDF path is outside the selected index document sources")

    if not os.path.exists(full_path):
        raise FileNotFoundError("PDF not found")

    return full_path


def extract_zip_safely(zip_ref, destination):
    destination = os.path.realpath(destination)
    for member in zip_ref.infolist():
        target_path = os.path.realpath(os.path.join(destination, member.filename))
        if os.path.commonpath([destination, target_path]) != destination:
            raise ValueError(f"Unsafe path in ZIP archive: {member.filename}")
        zip_ref.extract(member, destination)

@app.before_request
def authorize_request():
    if (request.endpoint == "health" and request.method in {"GET", "HEAD"}) or (
        request.method == "OPTIONS" and request.url_rule and request.url_rule.provide_automatic_options
    ):
        return None

    try:
        principal = get_current_principal()
    except (OSError, ValueError):
        logger.error("Unable to load access configuration", exc_info=True)
        return authorization_error("Access configuration is unavailable", 503)
    if principal is None:
        return authorization_error("Access denied", 403)
    if (principal["type"] == "tailscale" and not principal["is_admin"]
            and request.endpoint not in {"auth_me", "get_indices"}
            and principal["login"] not in load_access_config()["users"]):
        return authorization_error("This Tailscale user has no configured access", 403)

    g.skip_antivirus = principal["type"] == "test"
    if not g.skip_antivirus:
        log_request_client_activity(principal)


def authorization_error(message, status):
    response = jsonify({"error": message})
    response.status_code = status
    response.cache_control.no_store = True
    return response


@app.teardown_request
def release_access_config_lock(_error):
    if request.environ.pop("pdfsearch.access_config_locked", False):
        access_config_lock.release()


def update_task_status(task_id, status, message=None, result=None, index_name=None, coordinator=None):
    now = datetime.utcnow().isoformat() + "Z"
    existing = task_status.get(task_id, {})
    payload = {
        "status": status,
        "message": message,
        "result": result,
        "index_name": index_name,
        "created_at": existing.get("created_at", now),
        "updated_at": now
    }
    if coordinator is not None:
        payload["coordinator"] = coordinator
    elif "coordinator" in existing:
        payload["coordinator"] = existing["coordinator"]
    task_status[task_id] = payload


def task_result_with_updates(task_id, **updates):
    existing_result = task_status.get(task_id, {}).get("result")
    result = dict(existing_result) if isinstance(existing_result, dict) else {}
    result.update({key: value for key, value in updates.items() if value is not None})
    return result


def load_coordinator_state_file():
    if not os.path.exists(JOB_COORDINATOR_STATE_PATH):
        return {"paused_tasks": {}}
    try:
        with open(JOB_COORDINATOR_STATE_PATH, "r") as f:
            raw_state = json.load(f)
        if isinstance(raw_state, dict):
            paused_tasks = raw_state.get("paused_tasks")
            if isinstance(paused_tasks, dict):
                return {"paused_tasks": paused_tasks}
    except Exception:
        logger.warning("Unable to read coordinator state file", exc_info=True)
    return {"paused_tasks": {}}


def save_coordinator_state_file(state):
    state_dir = os.path.dirname(JOB_COORDINATOR_STATE_PATH) or "."
    os.makedirs(state_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".job_coordinator_state_",
        suffix=".json",
        dir=state_dir,
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=4)
        os.replace(temp_path, JOB_COORDINATOR_STATE_PATH)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def save_paused_indexing_task(task_id, state):
    if not state:
        return
    with coordinator_state_lock:
        raw_state = load_coordinator_state_file()
        paused_state = dict(state)
        paused_state["task_id"] = task_id
        paused_state["paused_at"] = datetime.utcnow().isoformat() + "Z"
        raw_state.setdefault("paused_tasks", {})[task_id] = paused_state
        save_coordinator_state_file(raw_state)


def clear_paused_indexing_task(task_id):
    with coordinator_state_lock:
        raw_state = load_coordinator_state_file()
        paused_tasks = raw_state.setdefault("paused_tasks", {})
        if task_id in paused_tasks:
            paused_tasks.pop(task_id, None)
            save_coordinator_state_file(raw_state)


def list_paused_indexing_tasks(index_name=None):
    raw_state = load_coordinator_state_file()
    paused_tasks = raw_state.get("paused_tasks", {})
    if not index_name:
        return paused_tasks
    return {
        task_id: state
        for task_id, state in paused_tasks.items()
        if state.get("index_name") == index_name
    }


def coordinator_event_payload(index_name, task_id, job_type, extra=None):
    payload = {
        "source": "pdfsearch",
        "index_name": index_name,
        "task_id": task_id,
        "job_type": job_type,
    }
    if extra:
        payload.update(extra)
    return payload


def coordinator_job_api_active():
    try:
        return bool(getattr(job_coordinator, "job_api_active", False))
    except Exception:
        return False


def coordinator_call(method_name, *args, default=None, **kwargs):
    method = getattr(job_coordinator, method_name, None)
    if not callable(method):
        return default
    return method(*args, **kwargs)


def notify_coordinator(action, index_name, task_id, job_type, extra=None):
    payload = coordinator_event_payload(index_name, task_id, job_type, extra=extra)
    if action == "task_finished" and coordinator_call("complete_task", task_id, payload, default=False):
        return True
    if action == "task_failed" and coordinator_call("fail_task", task_id, payload, default=False):
        return True
    return job_coordinator.notify(action, payload)


def public_coordinator_status(status):
    public_status = {
        "enabled": bool(status.get("enabled")),
        "available": bool(status.get("available")),
        "restricted": bool(status.get("restricted")),
        "reason": status.get("reason") or "",
        "resume_after_seconds": status.get("resume_after_seconds"),
        "checked_at": status.get("checked_at"),
        "source": status.get("source"),
        "error": status.get("error"),
    }
    for key in (
        "decision",
        "job_id",
        "cancel_requested",
        "cancel_reason",
        "mode",
        "active_jobs",
        "queued_jobs",
        "locks",
        "jellyfin_active_playback_count",
    ):
        if key in status:
            public_status[key] = status.get(key)
    return public_status


def coordinator_sleep_seconds(status):
    resume_after = status.get("resume_after_seconds")
    if isinstance(resume_after, (int, float)) and resume_after > 0:
        return max(0.5, min(float(resume_after), 3600.0))
    return job_coordinator.poll_seconds


def estimate_remaining_indexing_seconds(remaining_count, durations=None):
    if remaining_count <= 0:
        return 0.0
    durations = [duration for duration in (durations or []) if duration and duration > 0]
    seconds_per_document = (
        sum(durations) / len(durations)
        if durations else
        job_coordinator.default_seconds_per_document
    )
    return round(max(0.0, remaining_count * seconds_per_document), 1)


def build_coordinator_result(status, remaining_files=None, estimated_seconds=None):
    remaining_files = remaining_files or []
    result = {
        "coordinator": public_coordinator_status(status),
        "remaining_documents": len(remaining_files),
        "remaining_files": [os.path.basename(path) for path in remaining_files],
    }
    if estimated_seconds is not None:
        result["estimated_remaining_seconds"] = estimated_seconds
    return result


def make_coordinator_pause_checker(task_id):
    if not task_id or not coordinator_job_api_active():
        return None

    def check_pause():
        status = coordinator_call("get_task_runtime_status", task_id, default=None)
        if not status:
            return
        if status.get("cancel_requested"):
            raise CoordinatorPauseRequested(status)

    return check_pause


def await_coordinator_job_api(
    index_name,
    task_id,
    job_type,
    blocked_status,
    blocked_message,
    resume_message,
    pause_state=None,
    result_updates=None,
):
    if not coordinator_job_api_active():
        return False
    if coordinator_call("task_has_lease", task_id, default=False):
        return False

    was_blocked = False
    status = coordinator_call(
        "request_job",
        index_name,
        task_id,
        job_type,
        extra=result_updates,
        default=None,
    )
    if not isinstance(status, dict):
        return False
    terminal_decisions = {"cancelled", "failed", "rejected"}

    while True:
        decision = status.get("decision")
        public_status = public_coordinator_status(status)
        result_payload = dict(result_updates or {})
        result_payload["coordinator"] = public_status

        if decision in terminal_decisions:
            raise RuntimeError(status.get("reason") or f"Coordinator rejected job: {decision}")

        if not status.get("restricted"):
            if was_blocked:
                clear_paused_indexing_task(task_id)
                notify_coordinator(
                    "task_resumed",
                    index_name,
                    task_id,
                    job_type,
                    {"coordinator": public_status},
                )
                update_task_status(
                    task_id,
                    "processing",
                    message=resume_message,
                    result=task_result_with_updates(task_id, **result_payload),
                    index_name=index_name,
                    coordinator=public_status,
                )
            return was_blocked

        was_blocked = True
        if pause_state:
            save_paused_indexing_task(task_id, pause_state)
        notify_coordinator(
            "task_blocked",
            index_name,
            task_id,
            job_type,
            {"coordinator": public_status},
        )
        update_task_status(
            task_id,
            blocked_status,
            message=blocked_message if not status.get("reason") else f"{blocked_message} {status.get('reason')}",
            result=task_result_with_updates(task_id, **result_payload),
            index_name=index_name,
            coordinator=public_status,
        )
        time.sleep(coordinator_sleep_seconds(status))

        if status.get("job_id"):
            status = coordinator_call("poll_task_job", task_id, default=None)
        else:
            status = coordinator_call(
                "request_job",
                index_name,
                task_id,
                job_type,
                extra=result_updates,
                default=None,
            )
        if not isinstance(status, dict):
            return was_blocked


def await_coordinator_open(
    index_name,
    task_id,
    job_type,
    blocked_status="blocked",
    blocked_message="Waiting for the external job coordinator...",
    resume_message="External job coordinator cleared. Resuming indexing...",
    pause_state=None,
    result_updates=None,
):
    if not job_coordinator.enabled:
        return False
    if coordinator_job_api_active():
        return await_coordinator_job_api(
            index_name,
            task_id,
            job_type,
            blocked_status,
            blocked_message,
            resume_message,
            pause_state=pause_state,
            result_updates=result_updates,
        )

    was_blocked = False
    while True:
        status = job_coordinator.get_status()
        public_status = public_coordinator_status(status)
        result_payload = dict(result_updates or {})
        result_payload["coordinator"] = public_status
        if not status.get("restricted"):
            if was_blocked:
                clear_paused_indexing_task(task_id)
                notify_coordinator(
                    "task_resumed",
                    index_name,
                    task_id,
                    job_type,
                    {"coordinator": public_status},
                )
                update_task_status(
                    task_id,
                    "processing",
                    message=resume_message,
                    result=task_result_with_updates(task_id, **result_payload),
                    index_name=index_name,
                    coordinator=public_status,
                )
            return was_blocked

        was_blocked = True
        if pause_state:
            save_paused_indexing_task(task_id, pause_state)
        notify_coordinator(
            "task_blocked",
            index_name,
            task_id,
            job_type,
            {"coordinator": public_status},
        )
        update_task_status(
            task_id,
            blocked_status,
            message=blocked_message if not status.get("reason") else f"{blocked_message} {status.get('reason')}",
            result=task_result_with_updates(task_id, **result_payload),
            index_name=index_name,
            coordinator=public_status,
        )
        time.sleep(coordinator_sleep_seconds(status))


def wait_before_indexing_task(index_name, task_id, job_type, pause_state=None, result_updates=None):
    return await_coordinator_open(
        index_name,
        task_id,
        job_type,
        blocked_status="blocked",
        blocked_message="Heavy jobs are restricted by the external coordinator. Waiting to start indexing.",
        resume_message="Coordinator cleared. Starting indexing...",
        pause_state=pause_state,
        result_updates=result_updates,
    )


def should_pause_indexing_batch(remaining_files, durations=None, task_id=None):
    if not job_coordinator.enabled or not remaining_files:
        return False, None, 0.0

    if coordinator_job_api_active() and task_id:
        status = coordinator_call("get_task_runtime_status", task_id, default=None)
        if not isinstance(status, dict):
            return False, None, 0.0
        estimated_seconds = estimate_remaining_indexing_seconds(len(remaining_files), durations)
        should_pause = bool(status.get("cancel_requested"))
        return should_pause, status, estimated_seconds

    status = job_coordinator.get_status()
    estimated_seconds = estimate_remaining_indexing_seconds(len(remaining_files), durations)
    should_pause = bool(status.get("restricted")) and estimated_seconds > job_coordinator.wrapup_seconds
    return should_pause, status, estimated_seconds


def pause_indexing_batch(
    index_name,
    task_id,
    job_type,
    remaining_files,
    durations=None,
    pause_state=None,
):
    should_pause, status, estimated_seconds = should_pause_indexing_batch(
        remaining_files,
        durations,
        task_id=task_id,
    )
    if not status:
        return False, estimated_seconds

    result_updates = build_coordinator_result(status, remaining_files, estimated_seconds)
    if should_pause:
        state = dict(pause_state or {})
        state.setdefault("job_type", job_type)
        state.update({
            "index_name": index_name,
            "remaining_files": remaining_files,
            "estimated_remaining_seconds": estimated_seconds,
        })
        if coordinator_job_api_active() and status.get("cancel_requested"):
            coordinator_call("complete_task", task_id, {
                "paused": True,
                "reason": status.get("cancel_reason") or status.get("reason"),
                "remaining_documents": len(remaining_files),
                "estimated_remaining_seconds": estimated_seconds,
            }, default=False)
        await_coordinator_open(
            index_name,
            task_id,
            job_type,
            blocked_status="paused",
            blocked_message="Indexing paused after the current document because heavy jobs are restricted.",
            resume_message="Coordinator cleared. Resuming paused indexing...",
            pause_state=state,
            result_updates=result_updates,
        )
        return True, estimated_seconds

    if status.get("restricted"):
        update_task_status(
            task_id,
            "processing",
            message="Coordinator restriction detected, but remaining indexing is below the wrap-up threshold.",
            result=task_result_with_updates(task_id, **result_updates),
            index_name=index_name,
            coordinator=public_coordinator_status(status),
        )
    return False, estimated_seconds


def release_current_job_and_wait_for_resume(
    index_name,
    task_id,
    job_type,
    status,
    remaining_files,
    estimated_seconds,
    pause_state,
    result_updates,
    blocked_message,
    resume_message,
):
    if coordinator_job_api_active() and status and status.get("cancel_requested"):
        coordinator_call("complete_task", task_id, {
            "paused": True,
            "reason": status.get("cancel_reason") or status.get("reason"),
            "remaining_documents": len(remaining_files or []),
            "estimated_remaining_seconds": estimated_seconds,
        }, default=False)

    await_coordinator_open(
        index_name,
        task_id,
        job_type,
        blocked_status="paused",
        blocked_message=blocked_message,
        resume_message=resume_message,
        pause_state=pause_state,
        result_updates=result_updates,
    )


storage_config_lock = threading.Lock()
backup_job_lock = threading.Lock()
backup_scheduler_lock = threading.Lock()
backup_scheduler_thread = None


def coerce_positive_float(value, default):
    try:
        parsed = float(value)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default


def coerce_positive_int(value, default):
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default


def parse_utc_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def load_storage_config_file():
    if not os.path.exists(STORAGE_CONFIG_PATH):
        return {}
    try:
        with open(STORAGE_CONFIG_PATH, "r") as f:
            raw_config = json.load(f)
        return raw_config if isinstance(raw_config, dict) else {}
    except Exception:
        logger.warning("Unable to read storage config from %s", STORAGE_CONFIG_PATH, exc_info=True)
        return {}


def save_storage_config_file(config):
    config_dir = os.path.dirname(STORAGE_CONFIG_PATH) or "."
    os.makedirs(config_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".storage_config_",
        suffix=".json",
        dir=config_dir
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=4)
        os.replace(temp_path, STORAGE_CONFIG_PATH)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def normalize_backup_config(raw_config=None):
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    env_enabled = parse_bool(os.environ.get("BACKUP_ENABLED"), False)

    return {
        "enabled": parse_bool(raw_config.get("enabled"), env_enabled),
        "target_folder": str(raw_config.get("target_folder") or BACKUP_TARGET_FOLDER or "").strip(),
        "interval_hours": coerce_positive_float(
            raw_config.get("interval_hours", os.environ.get("BACKUP_INTERVAL_HOURS")),
            24.0
        ),
        "retention_count": coerce_positive_int(
            raw_config.get("retention_count", os.environ.get("BACKUP_RETENTION_COUNT")),
            7
        ),
        "include_deleted": parse_bool(raw_config.get("include_deleted"), True),
        "include_configs": parse_bool(raw_config.get("include_configs"), True),
        "last_run_at": raw_config.get("last_run_at"),
        "last_status": raw_config.get("last_status"),
        "last_message": raw_config.get("last_message"),
        "last_snapshot_path": raw_config.get("last_snapshot_path"),
        "last_task_id": raw_config.get("last_task_id")
    }


def get_backup_config():
    raw_config = load_storage_config_file()
    return normalize_backup_config(raw_config.get("backup", {}))


def get_active_storage_config():
    return {
        "pdf_folder": PDF_FOLDER,
        "index_folder": INDEX_FOLDER,
        "deleted_folder": DELETED_FOLDER,
        "tmp_folder": TMP_FOLDER,
        "log_folder": LOG_FOLDER,
        "email_configs": EMAIL_CONFIGS,
        "upload_folder": UPLOAD_FOLDER,
        "storage_config_path": STORAGE_CONFIG_PATH,
        "backup_target_default": BACKUP_TARGET_FOLDER
    }


def get_storage_config_payload():
    return {
        "storage": get_active_storage_config(),
        "backup": get_backup_config()
    }


def save_backup_config(requested_backup_config):
    requested_backup_config = requested_backup_config if isinstance(requested_backup_config, dict) else {}
    with storage_config_lock:
        raw_config = load_storage_config_file()
        existing_backup = normalize_backup_config(raw_config.get("backup", {}))
        merged_backup = dict(existing_backup)
        merged_backup.update(requested_backup_config)
        raw_config["backup"] = normalize_backup_config(merged_backup)
        raw_config.setdefault("storage", {})
        save_storage_config_file(raw_config)
        return raw_config["backup"]


def update_backup_run_state(status, message, task_id, snapshot_path=None):
    now = datetime.utcnow().isoformat() + "Z"
    with storage_config_lock:
        raw_config = load_storage_config_file()
        backup_config = normalize_backup_config(raw_config.get("backup", {}))
        backup_config.update({
            "last_run_at": now,
            "last_status": status,
            "last_message": message,
            "last_snapshot_path": snapshot_path,
            "last_task_id": task_id
        })
        raw_config["backup"] = backup_config
        raw_config.setdefault("storage", {})
        save_storage_config_file(raw_config)


def build_backup_sources(backup_config):
    sources = [
        ("pdfs", PDF_FOLDER),
        ("index", INDEX_FOLDER)
    ]

    if backup_config.get("include_deleted"):
        sources.append(("deleted", DELETED_FOLDER))

    if backup_config.get("include_configs"):
        sources.extend([
            ("configs/access_config.json", ACCESS_CONFIG_PATH),
            ("configs/roles.json", ROLE_CONFIG_PATH),
            ("configs/storage_config.json", STORAGE_CONFIG_PATH),
            ("email_configs", EMAIL_CONFIGS)
        ])

    return [(label, source) for label, source in sources if source and os.path.exists(source)]


def validate_backup_target(target_folder, sources):
    if not target_folder:
        raise ValueError("Backup target folder is required")

    target_root = os.path.realpath(os.path.normpath(target_folder))
    for _, source_path in sources:
        source_root = os.path.realpath(os.path.normpath(source_path))
        if os.path.commonpath([source_root, target_root]) == source_root:
            raise ValueError(
                f"Backup target must not be inside a backed-up source folder: {source_path}"
            )

    return target_root


def backup_ignore_patterns(_directory, names):
    ignored = []
    for name in names:
        if name.endswith("WRITELOCK") or name == "MAIN_WRITELOCK" or name.endswith(".tmp"):
            ignored.append(name)
    return ignored


@contextmanager
def acquire_index_locks_for_backup():
    lock_names = unique_valid_index_names(discover_indices())
    locks = []
    try:
        for index_name in sorted(lock_names):
            lock = task_queue.get_lock(index_name)
            lock.acquire()
            locks.append(lock)
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


def copy_backup_source(source_path, destination_path):
    if os.path.isdir(source_path):
        shutil.copytree(source_path, destination_path, ignore=backup_ignore_patterns)
        return "directory"

    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return "file"


def prune_backup_snapshots(target_folder, retention_count):
    snapshots = []
    for name in os.listdir(target_folder):
        path = os.path.join(target_folder, name)
        if name.startswith("pdfsearch-") and os.path.isdir(path):
            snapshots.append((name, path))

    snapshots.sort(reverse=True)
    pruned = []
    for name, path in snapshots[retention_count:]:
        shutil.rmtree(path)
        pruned.append(name)
    return pruned


def create_backup_snapshot(backup_config, trigger):
    sources = build_backup_sources(backup_config)
    target_folder = validate_backup_target(backup_config.get("target_folder"), sources)
    os.makedirs(target_folder, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    snapshot_dir = os.path.join(target_folder, f"pdfsearch-{timestamp}")
    if os.path.exists(snapshot_dir):
        snapshot_dir = os.path.join(target_folder, f"pdfsearch-{timestamp}-{uuid.uuid4().hex[:8]}")
    os.makedirs(snapshot_dir)

    manifest = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "trigger": trigger,
        "snapshot_path": snapshot_dir,
        "storage": get_active_storage_config(),
        "sources": [],
        "pruned_snapshots": []
    }

    with acquire_index_locks_for_backup():
        for label, source_path in sources:
            destination_path = os.path.join(snapshot_dir, label)
            copied_type = copy_backup_source(source_path, destination_path)
            manifest["sources"].append({
                "label": label,
                "source": source_path,
                "destination": destination_path,
                "type": copied_type
            })

    manifest["pruned_snapshots"] = prune_backup_snapshots(
        target_folder,
        backup_config.get("retention_count", 7)
    )

    manifest_path = os.path.join(snapshot_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=4)

    return manifest


def perform_backup_task(task_id, trigger="manual"):
    if not backup_job_lock.acquire(blocking=False):
        message = "Backup is already running"
        update_task_status(task_id, "failed", message=message)
        update_backup_run_state("failed", message, task_id)
        return

    try:
        backup_config = get_backup_config()
        update_task_status(task_id, "processing", message="Creating storage backup...")
        manifest = create_backup_snapshot(backup_config, trigger)
        message = f"Backup completed: {manifest['snapshot_path']}"
        update_backup_run_state("done", message, task_id, manifest["snapshot_path"])
        update_task_status(task_id, "done", message="Backup completed.", result={
            "message": message,
            "snapshot_path": manifest["snapshot_path"],
            "sources": manifest["sources"],
            "pruned_snapshots": manifest["pruned_snapshots"]
        })
    except Exception as e:
        logger.error("Backup task failed: %s", e, exc_info=True)
        message = f"Backup failed: {e}"
        update_backup_run_state("failed", message, task_id)
        update_task_status(task_id, "failed", message=message)
    finally:
        backup_job_lock.release()


def queue_backup_task(trigger="manual"):
    task_id = str(uuid.uuid4())
    update_task_status(task_id, "queued", message=f"Queued {trigger} backup")
    thread = threading.Thread(target=perform_backup_task, args=(task_id, trigger), daemon=True)
    thread.start()
    return task_id


def seconds_until_next_backup(backup_config):
    last_run = parse_utc_datetime(backup_config.get("last_run_at"))
    if not last_run:
        return 0
    next_run = last_run + timedelta(hours=backup_config.get("interval_hours", 24.0))
    return max(0, (next_run - datetime.utcnow()).total_seconds())


def backup_scheduler_loop():
    while True:
        sleep_seconds = 60
        try:
            backup_config = get_backup_config()
            if backup_config.get("enabled"):
                wait_seconds = seconds_until_next_backup(backup_config)
                if wait_seconds <= 0 and not backup_job_lock.locked():
                    queue_backup_task("scheduled")
                    sleep_seconds = 60
                else:
                    sleep_seconds = min(max(wait_seconds, 60), 3600)
        except Exception:
            logger.error("Backup scheduler error", exc_info=True)
        time.sleep(sleep_seconds)


def ensure_backup_scheduler_running():
    global backup_scheduler_thread
    with backup_scheduler_lock:
        if backup_scheduler_thread and backup_scheduler_thread.is_alive():
            return
        backup_scheduler_thread = threading.Thread(target=backup_scheduler_loop, daemon=True)
        backup_scheduler_thread.start()


@app.route("/admin/storage-config", methods=["GET", "PUT"])
def manage_storage_config():
    try:
        ensure_admin_access()

        if request.method == "GET":
            return jsonify(get_storage_config_payload()), 200

        data = request.get_json(silent=True) or {}
        backup_config = save_backup_config(data.get("backup", data))
        if backup_config.get("enabled") and not parse_bool(os.environ.get("DISABLE_BACKUP_SCHEDULER")):
            ensure_backup_scheduler_running()
        return jsonify({
            "message": "Storage backup configuration saved.",
            "storage": get_active_storage_config(),
            "backup": backup_config
        }), 200
    except PermissionError as e:
        logger.warning(f"Forbidden storage-config request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid storage-config request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to manage storage config: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/admin/backup/run", methods=["POST"])
def run_backup_now():
    try:
        ensure_admin_access()
        task_id = queue_backup_task("manual")
        return jsonify({
            "message": "Backup task queued.",
            "task_id": task_id
        }), 202
    except PermissionError as e:
        logger.warning(f"Forbidden backup request: {e}")
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        logger.error(f"Failed to queue backup: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/tasks", methods=["GET"])
def list_all_tasks():
    try:
        index_filter = request.args.get("index_name")
        if index_filter:
            ensure_index_access(index_filter)
            filtered = {tid: t for tid, t in task_status.items() if t.get("index_name") == index_filter}
            return jsonify(filtered)

        if get_current_principal()["is_admin"]:
            return jsonify(task_status)

        allowed_indices = set(get_accessible_indices())
        filtered = {
            tid: task
            for tid, task in task_status.items()
            if task.get("index_name") in allowed_indices
        }
        return jsonify(filtered)
    except PermissionError as e:
        logger.warning(f"Forbidden tasks request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid tasks request: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/task-status/<task_id>", methods=["GET"])
def get_task_status(task_id):
    task = task_status.get(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    try:
        index_name = task.get("index_name")
        if index_name:
            ensure_index_access(index_name)
        else:
            ensure_admin_access()
    except PermissionError as e:
        logger.warning(f"Forbidden task-status request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid task-status request: {e}")
        return jsonify({"error": str(e)}), 400
    return jsonify(task), 200


@app.route("/job-coordinator/status", methods=["GET"])
def get_job_coordinator_status():
    try:
        index_name = request.args.get("index_name")
        if index_name:
            ensure_index_access(index_name)
        else:
            ensure_admin_access()

        paused_tasks = list_paused_indexing_tasks(index_name)
        return jsonify({
            "enabled": job_coordinator.enabled,
            "status": public_coordinator_status(job_coordinator.get_overview_status()),
            "paused_tasks": {
                task_id: {
                    "job_type": state.get("job_type"),
                    "phase": state.get("phase"),
                    "index_name": state.get("index_name"),
                    "filename": state.get("filename"),
                    "remaining_documents": len(state.get("remaining_files") or []),
                    "remaining_files": [
                        os.path.basename(path)
                        for path in (state.get("remaining_files") or [])
                    ],
                    "estimated_remaining_seconds": state.get("estimated_remaining_seconds"),
                    "paused_at": state.get("paused_at"),
                }
                for task_id, state in paused_tasks.items()
            }
        }), 200
    except PermissionError as e:
        logger.warning(f"Forbidden coordinator status request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid coordinator status request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to fetch coordinator status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def load_roles():
    if os.path.exists(ROLE_CONFIG_PATH):
        with open(ROLE_CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


def save_roles(roles):
    os.makedirs(os.path.dirname(ROLE_CONFIG_PATH), exist_ok=True)
    with open(ROLE_CONFIG_PATH, "w") as f:
        json.dump(roles, f)


@app.route('/roles', methods=['GET', 'POST'])
def manage_roles():
    """Read or edit archived IP roles for legacy clients. These grant no access."""
    try:
        ensure_admin_access()

        if request.method == 'GET':
            return jsonify(load_roles()), 200

        if request.method == 'POST':
            data = request.json
            ip = data.get("ip")
            role = data.get("role")
            if not ip or not role:
                return jsonify({"error": "IP and role are required"}), 400

            roles = load_roles()
            roles[ip] = role
            save_roles(roles)
            return jsonify({"message": f"Role '{role}' assigned to IP '{ip}'"}), 200
    except PermissionError as e:
        logger.warning(f"Forbidden roles request: {e}")
        return jsonify({"error": str(e)}), 403


@app.route("/clean-test-index", methods=["POST"])
def clean_test_index():
    if get_current_principal()["type"] != "test":
        return authorization_error("Test authorization required", 403)

    index_name = "test_index"
    index_path = os.path.join(INDEX_FOLDER, index_name)
    ocr_path = os.path.join(PDF_FOLDER , index_name)
    tmp_path = os.path.join(TMP_FOLDER, index_name)

    try:
        # Remove index folder
        if os.path.exists(index_path):
            shutil.rmtree(index_path)
            logger.info(f"Deleted index folder: {index_path}")

        # Remove OCR’d files
        if os.path.exists(ocr_path):
            shutil.rmtree(ocr_path)
            logger.info(f"Deleted OCR folder: {ocr_path}")

        # Remove TMP folder
        if os.path.exists(tmp_path):
            shutil.rmtree(tmp_path)
            logger.info(f"Deleted TMP folder: {tmp_path}")

        return jsonify({"message": "Test index and associated files cleaned."}), 200

    except Exception as e:
        logger.error(f"Failed to clean test index: {e}")
        return jsonify({"error": "Failed to clean test index."}), 500

@app.route('/indices', methods=['GET'])
def get_indices():
    """
    Return the list of indices accessible to the current user.
    """
    try:
        principal = get_current_principal()
        available_indices = get_accessible_indices()
        logger.debug("Current principal: %s (%s)", principal["id"], principal["type"])
        logger.debug("Available indices: %s", available_indices)
        index_configs = {
            index_name: public_index_config(index_name)
            for index_name in available_indices
        }
        return jsonify({
            "available_indices": available_indices,
            "index_configs": index_configs,
            "current_user": {
                "type": principal["type"],
                "id": principal["id"],
                "login": principal["login"],
                "role": principal["role"],
                "is_admin": principal["is_admin"],
                "readable_indices": get_accessible_indices("read"),
                "writable_indices": get_accessible_indices("write"),
            }
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/auth/me', methods=['GET'])
def auth_me():
    try:
        principal = get_current_principal()
        return jsonify({
            "type": principal["type"],
            "id": principal["id"],
            "login": principal["login"],
            "role": principal["role"],
            "is_admin": principal["is_admin"],
            "available_indices": get_accessible_indices(),
            "readable_indices": get_accessible_indices("read"),
            "writable_indices": get_accessible_indices("write"),
        }), 200
    except Exception as e:
        logger.error(f"Failed to resolve current user: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/admin/access-config', methods=['GET', 'PUT'])
def manage_access_config():
    try:
        ensure_admin_access()

        if request.method == 'GET':
            return jsonify({
                "config": load_access_config(),
                "current_user": get_current_principal()
            }), 200

        data = app.json.loads(request.get_data(), object_pairs_hook=unique_access_config_fields) if request.is_json else None
        if not isinstance(data, dict):
            raise ValueError("Access configuration must be an object")
        config = data.get("config", data)
        saved_config = save_access_config(config)
        return jsonify({
            "message": "Access configuration saved.",
            "config": saved_config
        }), 200
    except PermissionError as e:
        logger.warning(f"Forbidden access-config request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid access-config request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to manage access config: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/admin/indices', methods=['POST'])
def create_admin_index():
    try:
        principal = get_current_principal()
        data = request.get_json(silent=True) or {}
        index_name = validate_index_name((data.get("index_name") or "").strip())
        requested_index_config = data.get("index_config") or {
            key: data[key]
            for key in ("mode", "cloud")
            if key in data
        }

        access_config = load_access_config()
        already_registered = index_name in access_config.get("indices", [])
        if already_registered:
            ensure_index_access(index_name, action="write")
        available_indices = get_accessible_indices()

        index_config = (
            save_index_config(index_name, requested_index_config)
            if requested_index_config else
            load_index_config(index_name)
        )
        create_or_open_index(index_name, check_access=False)
        os.makedirs(os.path.join(PDF_FOLDER, index_name), exist_ok=True)
        os.makedirs(os.path.join(TMP_FOLDER, index_name), exist_ok=True)

        if not already_registered:
            access_config["indices"] = access_config["indices"] + [index_name]
            if not principal["is_admin"]:
                user_permissions = access_config["users"][principal["login"]]
                user_permissions["read"] = user_permissions["read"] + [index_name]
                user_permissions["write"] = user_permissions["write"] + [index_name]
            access_config = save_access_config(access_config)
            available_indices = available_indices + [index_name]

        cloud_sync_task_id = None
        if index_has_cloud_source(index_name, index_config):
            cloud_sync_task_id = queue_cloud_sync_task(index_name, reason="index_config_saved")

        return jsonify({
            "message": f"Index '{index_name}' is ready.",
            "index_name": index_name,
            "created": not already_registered,
            "config": access_config if principal["is_admin"] else None,
            "available_indices": available_indices,
            "index_config": public_index_config(index_name),
            "cloud_sync_task_id": cloud_sync_task_id,
        }), 201 if not already_registered else 200
    except PermissionError as e:
        logger.warning(f"Forbidden create-index request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid create-index request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to create index: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def public_index_config(index_name):
    config = load_index_config(index_name)
    return {
        **config,
        "capabilities": {
            "uploads_enabled": index_accepts_uploads(index_name, config),
            "cloud_sync_enabled": index_has_cloud_source(index_name, config),
            "folder_view_enabled": config.get("mode") in {"cloud", "hybrid"},
        },
    }


@app.route("/cloud-providers", methods=["GET"])
def get_cloud_providers():
    try:
        principal = get_current_principal()
        return jsonify({
            "providers": cloud_provider_definitions(),
            "provider_settings": cloud_provider_settings_public(include_secrets=False),
            "connections": list_visible_cloud_connections_public(),
            "can_manage_provider_settings": principal["is_admin"],
            "current_user": principal,
        }), 200
    except PermissionError as e:
        logger.warning(f"Forbidden cloud-providers request: {e}")
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        logger.error(f"Failed to list cloud providers: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/cloud-provider-settings", methods=["GET", "PUT"])
def manage_cloud_provider_settings():
    try:
        if request.method == "GET":
            principal = get_current_principal()
            return jsonify({
                "provider_settings": cloud_provider_settings_public(include_secrets=False),
                "can_manage_provider_settings": principal["is_admin"],
            }), 200

        ensure_admin_access()
        data = request.get_json(silent=True) or {}
        provider = str(data.get("provider") or ONEDRIVE_CLOUD_PROVIDER).strip().lower()
        if provider not in {ONEDRIVE_CLOUD_PROVIDER, GOOGLE_DRIVE_CLOUD_PROVIDER, DROPBOX_CLOUD_PROVIDER}:
            raise ValueError("Unsupported cloud provider settings")

        settings = data.get("settings") if isinstance(data.get("settings"), dict) else data.get(provider)
        if not isinstance(settings, dict):
            settings = data

        current = load_cloud_provider_config()
        existing = current.setdefault("providers", {}).get(provider)
        if provider == ONEDRIVE_CLOUD_PROVIDER:
            current["providers"][provider] = normalize_onedrive_provider_config(settings)
        elif provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
            current["providers"][provider] = normalize_google_drive_provider_config(settings, existing)
        elif provider == DROPBOX_CLOUD_PROVIDER:
            current["providers"][provider] = normalize_dropbox_provider_config(settings, existing)

        saved = save_cloud_provider_config(current, validate_provider=provider)
        return jsonify({
            "message": "Cloud provider registration saved.",
            "providers": cloud_provider_definitions(),
            "provider_settings": cloud_provider_settings_public(include_secrets=False),
            "connections": list_visible_cloud_connections_public(),
            "saved_provider": provider,
        }), 200
    except PermissionError as e:
        logger.warning(f"Forbidden cloud-provider-settings request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid cloud provider settings request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to manage cloud provider settings: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/cloud-connections/start", methods=["POST"])
def start_cloud_connection():
    try:
        data = request.get_json(silent=True) or {}
        provider = str(data.get("provider") or "").strip().lower()
        display_name = str(data.get("display_name") or "").strip()
        index_name = data.get("index_name")
        ensure_can_start_cloud_connection(index_name)
        if provider == ONEDRIVE_CLOUD_PROVIDER:
            connection = start_onedrive_device_login(display_name=display_name)
        elif provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
            connection = start_google_drive_browser_login(display_name=display_name)
        elif provider == DROPBOX_CLOUD_PROVIDER:
            connection = start_dropbox_browser_login(display_name=display_name)
        else:
            raise ValueError("Unsupported cloud provider login")

        return jsonify({
            "message": "Cloud login started.",
            "connection": connection,
        }), 202
    except PermissionError as e:
        logger.warning(f"Forbidden start-cloud-connection request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid cloud connection request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to start cloud connection: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/cloud-connections/<connection_id>", methods=["GET"])
def get_cloud_connection_status(connection_id):
    try:
        with cloud_auth_sessions_lock:
            session = cloud_auth_sessions.get(connection_id)
        if session:
            if not get_current_principal()["is_admin"] and not owner_matches_principal(session.get("owner")):
                raise PermissionError("Access denied to this cloud login")
            return jsonify(sanitize_cloud_auth_session(session)), 200

        connection = get_cloud_connection(connection_id)
        ensure_cloud_connection_access(connection_id, connection)
        if not connection:
            return jsonify({"error": "Cloud connection not found"}), 404
        return jsonify(sanitize_cloud_connection(connection)), 200
    except PermissionError as e:
        logger.warning(f"Forbidden cloud connection status request: {e}")
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        logger.error(f"Failed to read cloud connection status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/cloud-connections/callback/<provider>", methods=["GET"])
def cloud_connection_callback(provider):
    provider = str(provider or "").strip().lower()
    state = request.args.get("state")
    code = request.args.get("code")
    error = request.args.get("error")
    error_description = request.args.get("error_description")
    try:
        if provider == GOOGLE_DRIVE_CLOUD_PROVIDER:
            return complete_google_drive_login(state, code=code, error=error, error_description=error_description)
        if provider == DROPBOX_CLOUD_PROVIDER:
            return complete_dropbox_login(state, code=code, error=error, error_description=error_description)
        return cloud_login_result_page("Cloud login", "Unsupported cloud provider callback.", ok=False)
    except PermissionError as e:
        return authorization_error(str(e), 403)


@app.route("/index-config", methods=["GET", "PUT"])
def manage_index_config():
    try:
        if request.method == "GET":
            index_name = request.args.get("index_name")
            if not index_name:
                return jsonify({"error": "index_name is required"}), 400
            ensure_index_access(index_name)
            return jsonify(public_index_config(index_name)), 200

        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        if not index_name:
            return jsonify({"error": "index_name is required"}), 400
        ensure_index_access(index_name, action="write")

        requested_config = data.get("index_config") or {
            key: data[key]
            for key in ("mode", "cloud")
            if key in data
        }
        saved_config = save_index_config(index_name, requested_config)
        cloud_sync_task_id = None
        if index_has_cloud_source(index_name, saved_config):
            cloud_sync_task_id = queue_cloud_sync_task(index_name, reason="index_config_saved")

        return jsonify({
            "message": "Index mode settings saved.",
            "index_config": public_index_config(index_name),
            "cloud_sync_task_id": cloud_sync_task_id,
        }), 200
    except PermissionError as e:
        logger.warning(f"Forbidden index-config request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid index-config request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to manage index config: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/sync-cloud", methods=["POST"])
def sync_cloud_now():
    try:
        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        if not index_name:
            return jsonify({"error": "index_name is required"}), 400
        ensure_index_access(index_name, action="write")
        ensure_cloud_sync_config_ready(index_name)

        task_id = queue_cloud_sync_task(index_name, reason="manual")
        return jsonify({
            "message": "Cloud sync task queued.",
            "task_id": task_id,
        }), 202
    except PermissionError as e:
        logger.warning(f"Forbidden cloud-sync request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid cloud-sync request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to queue cloud sync: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/folder-tree", methods=["GET"])
def folder_tree():
    try:
        index_name = request.args.get("index_name")
        if not index_name:
            return jsonify({"error": "index_name is required"}), 400
        ensure_index_access(index_name)
        folder_path = normalize_virtual_path(request.args.get("path") or "")
        ix, _ = create_or_open_index(index_name)
        metadata = ensure_document_metadata(index_name, ix=ix)
        return jsonify(build_folder_tree_response(index_name, folder_path, metadata)), 200
    except PermissionError as e:
        logger.warning(f"Forbidden folder-tree request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid folder-tree request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to build folder tree: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


################################################################################################################acces controll done

##################################frontend

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    """
    Serve static frontend files or fallback to index.html for Single Page Applications.
    """
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, "index.html")


#####################################frontend done


# Browser-origin policy is installed immediately after Flask initialization.


@lru_cache(maxsize=256)
def indexed_document_path(index_folder, revision, doc_id):
    # Only retain the path. The stored document can include the full OCR text.
    ix = index.open_dir(index_folder)
    with ix.searcher() as searcher:
        stored_doc = searcher.document(id=doc_id)
    return stored_doc.get("path") if stored_doc else None


def resolve_requested_pdf(payload, action="read"):
    payload = payload or {}
    index_name = payload.get("index_name")
    doc_id = payload.get("id") or payload.get("doc_id")
    pdf_path = payload.get("path")

    if index_name and doc_id:
        ensure_index_access(index_name, action=action)
        index_folder = get_index_folder_path(index_name)
        # Whoosh publishes each TOC by renaming a file in this directory.
        # Include directory identity and timestamps to catch rebuilds as well.
        with DOCUMENT_PATH_CACHE_LOCK:
            try:
                stat = os.stat(index_folder)
                revision = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
                stored_path = indexed_document_path(index_folder, revision, doc_id)
            except (FileNotFoundError, index.EmptyIndexError):
                stored_path = None
        if not stored_path:
            raise FileNotFoundError("Document not found")
        return validate_pdf_path_for_index(stored_path, index_name), index_name, doc_id

    if pdf_path:
        resolved_path, resolved_index_name = resolve_index_pdf_path(pdf_path)
        return resolved_path, resolved_index_name, None

    raise ValueError("Missing document path or id")


def pdf_work_busy():
    response = jsonify({"error": "PDF preview is busy. Please try again."})
    response.status_code = 503
    response.headers["Retry-After"] = "1"
    response.cache_control.no_store = True
    return response


def limit_preview_requests(view):
    @wraps(view)
    def limited_view():
        if not PREVIEW_REQUESTS.acquire(blocking=False):
            return pdf_work_busy()
        try:
            response = app.make_response(view())
            # Direct file responses bypass call_on_close. Wrap the iterable so
            # the slot is held while streaming and released on disconnect.
            response.response = ClosingIterator(response.response, PREVIEW_REQUESTS.release)
            return response
        except BaseException:
            PREVIEW_REQUESTS.release()
            raise
    return limited_view


def send_extracted_pdf(reader, page_numbers, attachment_name=None):
    with tempfile.NamedTemporaryFile(dir=TMP_FOLDER, suffix=".pdf") as output:
        writer = PdfWriter()
        for page_number in page_numbers:
            writer.add_page(reader.pages[page_number - 1])
        writer.write(output)
        output.flush()
        # send_file opens its own descriptor. The temporary name is removed
        # here and the descriptor is closed when the WSGI response ends.
        return send_file(
            output.name, mimetype="application/pdf",
            as_attachment=attachment_name is not None,
            download_name=attachment_name,
        )


@app.route('/download', methods=['POST'])
def download():
    """
    Returns the requested pages of a PDF for download.
    If no pages are specified, returns the entire PDF.
    """
    try:
        data = request.get_json(silent=True) or {}
        pdf_path, _, _ = resolve_requested_pdf(data)
        pages = data.get("pages", [])  # Default to an empty list if no pages are provided

        # Handle case where no pages are specified (return the entire file)
        if not pages:
            logger.info(f"Returning entire PDF: {pdf_path}")
            return send_file(
                pdf_path,
                as_attachment=True,
                download_name=os.path.basename(pdf_path)
            )

        if not PDF_PAGE_WORK.acquire(blocking=False):
            return pdf_work_busy()
        try:
            with open(pdf_path, "rb") as source:
                reader = PdfReader(source)
                count = len(reader.pages)
                selected_pages = [page for page in pages if 1 <= page <= count]
                return send_extracted_pdf(
                    reader, selected_pages, f"selected_pages_{os.path.basename(pdf_path)}",
                )
        finally:
            PDF_PAGE_WORK.release()

    except HTTPException:
        raise
    except PermissionError as e:
        logger.warning(f"Forbidden download request: {e}")
        return jsonify({"error": str(e)}), 403
    except FileNotFoundError as e:
        logger.warning(f"Download file not found: {e}")
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        logger.warning(f"Invalid download request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Unhandled error in /download: {e}")
        return jsonify({"error": f"Unhandled error: {e}"}), 500


def extract_years_from_text(text):
    """
    Extracts years from various date formats in the given text.
    Handles numeric formats, German full and abbreviated month names.
    """
    # Numeric date formats: DD.MM.YYYY, DD-MM-YYYY, DD/MM/YYYY
    numeric_date_pattern = r'\b\d{1,2}[./-]\d{1,2}[./-](\d{4})\b'

    # German full month names: e.g., 1. Januar 2024
    german_full_month_pattern = r'\b\d{1,2}\.\s+(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+(\d{4})\b'

    # German abbreviated month names: e.g., 1. Jan 2024
    german_abbreviated_month_pattern = r'\b\d{1,2}\.\s+(Jan|Feb|Mär|Apr|Mai|Jun|Jul|Aug|Sep|Okt|Nov|Dez)\s+(\d{4})\b'

    # Extract years from numeric date formats
    numeric_years = re.findall(numeric_date_pattern, text)

    # Extract years from German full month formats
    german_full_month_years = [match[1] for match in re.findall(german_full_month_pattern, text)]

    # Extract years from German abbreviated month formats
    german_abbreviated_month_years = [match[1] for match in re.findall(german_abbreviated_month_pattern, text)]

    # Combine and deduplicate all extracted years
    all_years = set(numeric_years + german_full_month_years + german_abbreviated_month_years)

    # Convert all years to integers and sort
    return sorted(int(year) for year in all_years)



def extract_years_from_title(file_title):
    """
    Extracts valid years from the file title, including four-digit years and
    two-digit years preceded by a period (e.g., .12 maps to 2012 if within range).
    Only includes years within the last 24 years, starting from the current year.
    Deduplicates the extracted years.
    """
    # Calculate the range of valid years
    current_year = datetime.now().year
    earliest_year = current_year - 24

    # Regex to match 4-digit years
    year_pattern_4digit = r'(?<!\d)(?:19|20)\d{2}(?!\d)'

    # Regex to match two-digit years preceded by a "."
    year_pattern_2digit = r'\.\d{2}\b'

    # Find all four-digit years
    four_digit_years = re.findall(year_pattern_4digit, file_title)

    # Find all two-digit years preceded by a "."
    two_digit_years = re.findall(year_pattern_2digit, file_title)
    two_digit_years = [int(year[1:]) for year in two_digit_years]  # Remove leading "."

    # Convert two-digit years to four-digit years
    converted_two_digit_years = [
        year + 2000 for year in two_digit_years if earliest_year <= year + 2000 <= current_year
    ]

    # Filter and deduplicate valid four-digit years
    valid_four_digit_years = [
        int(year) for year in four_digit_years if earliest_year <= int(year) <= current_year
    ]

    # Combine and deduplicate all valid years
    all_valid_years = sorted(set(valid_four_digit_years + converted_two_digit_years))

    return all_valid_years


def create_or_open_index(index_name, check_access=True):
    """
    Creates or opens a Whoosh index for a given index name.
    If check_access is True and running in request context, enforces role-based access.
    """

    validate_index_name(index_name)

    # Only enforce access control if allowed and inside a request
    if check_access and has_request_context():
        try:
            ensure_index_access(index_name)
        except Exception as e:
            logging.error(f"Error during access control check: {e}")
            raise
    elif not has_request_context():
        logging.info(f"[BG TASK] Skipping access check for index '{index_name}' (no request context).")

    # Create or open the target index
    index_folder = os.path.join(INDEX_FOLDER, index_name)
    schema = Schema(
        id=ID(stored=True, unique=True),  # Use the content hash as a unique identifier
        title=TEXT(stored=True),
        path=TEXT(stored=True),
        content=TEXT(stored=True),
        years=TEXT(stored=True)
    )

    try:
        if not os.path.exists(index_folder):
            os.makedirs(index_folder)
            logging.info(f"Creating a new index folder at: {index_folder}")
            return index.create_in(index_folder, schema), index_name

        elif not index.exists_in(index_folder):
            logging.warning(f"Index folder exists but no valid index found. Creating new index at: {index_folder}")
            return index.create_in(index_folder, schema), index_name

        else:
            logging.info(f"Opening existing index at: {index_folder}")
            return index.open_dir(index_folder), index_name

    except KeyError as e:
        logging.error(f"Role or index mapping error: {str(e)}", exc_info=True)
        raise

    except OSError as e:
        logging.error(f"File system error when accessing index folder: {str(e)}", exc_info=True)
        raise

    except Exception as e:
        logging.error(f"Unexpected error in create_or_open_index: {str(e)}", exc_info=True)
        raise

PDF_MISSING_PAGES_MESSAGE = (
    "PDF has no readable page tree (/Pages); it may be malformed or not a standard page-based PDF"
)


def is_missing_pdf_pages_error(error):
    return isinstance(error, KeyError) and str(error).strip("'\"") == "/Pages"


def format_pdf_read_error(error):
    if is_missing_pdf_pages_error(error):
        return PDF_MISSING_PAGES_MESSAGE
    return str(error) or error.__class__.__name__


def pdf_missing_readable_page_tree(file_path):
    try:
        reader = PdfReader(file_path)
        len(reader.pages)
        return False
    except Exception as e:
        return is_missing_pdf_pages_error(e)


def searchable_title_text(file_title):
    return re.sub(r"[_\W]+", " ", file_title or "", flags=re.UNICODE).strip()


def build_index_document(file_path, file_title, indexed_path=None, document_id=None, allow_metadata_only=False):
    """
    Extracts searchable fields for a PDF before writing them into Whoosh.
    """
    all_years = set()
    all_content = []
    metadata_only = False

    try:
        reader = PdfReader(file_path)
        page_count = len(reader.pages)
    except Exception as e:
        if allow_metadata_only and is_missing_pdf_pages_error(e):
            logger.warning(
                "Indexing PDF without page content because its page tree is unreadable: %s: %s",
                file_path,
                format_pdf_read_error(e),
            )
            reader = None
            page_count = 0
            metadata_only = True
        else:
            raise RuntimeError(f"Unable to read PDF pages: {format_pdf_read_error(e)}") from e

    for page_num in range(page_count):
        try:
            page = reader.pages[page_num]
            content = page.extract_text() or ""
        except Exception as e:
            logger.warning(f"Text extraction failed on page {page_num + 1} of {file_path}: {e}")
            content = ""  # fallback so indexing continues
        all_content.append(content)
        all_years.update(extract_years_from_text(content))

    all_years.update(extract_years_from_title(file_title))
    content_parts = list(all_content)
    for title_text in (file_title, searchable_title_text(file_title)):
        if title_text and title_text not in content_parts:
            content_parts.append(title_text)
    full_content = "\n".join(content_parts)
    if metadata_only:
        try:
            content_hash = calculate_file_hash(file_path)
        except Exception:
            content_hash = hashlib.sha256(file_title.encode("utf-8", errors="replace")).hexdigest()
    else:
        content_hash = calculate_text_hash(file_path)

    return {
        "id": document_id or content_hash,
        "title": file_title,
        "path": indexed_path or file_path,
        "content": full_content,
        "years": ",".join(map(str, sorted(all_years))),
        "content_hash": content_hash,
    }


def index_document_for_schema(ix, index_document):
    schema_names = set(ix.schema.names())
    return {
        key: value
        for key, value in index_document.items()
        if key in schema_names
    }


def add_to_index(
    index_name,
    file_path,
    file_title,
    ix=None,
    writer=None,
    indexed_path=None,
    document_id=None,
    source="standalone",
    cloud_relative_path="",
    virtual_path="",
    file_hash=None,
    allow_existing_id=False,
    update_metadata=True,
):
    """
    Adds the content of a file to the search index using the content hash as the unique identifier.
    Accepts optional shared `ix` and `writer` for batching.
    """
    should_commit = False

    if ix is None or writer is None:
        ix, index_name = create_or_open_index(index_name,check_access=False)
        writer = ix.writer()
        should_commit = True

    index_document = build_index_document(
        file_path,
        file_title,
        indexed_path=indexed_path,
        document_id=document_id,
    )

    with ix.searcher() as searcher:
        if searcher.document(id=index_document["id"]) and not allow_existing_id:
            logger.warning(f"Duplicate file detected in index {index_name}: {file_path}. Saving but not indexing")
            raise DuplicateDocumentError("Duplicate file detected — error: already indexed")

    writer.update_document(**index_document_for_schema(ix, index_document))
    logger.info(f"Indexed document {file_title} with ID {index_document['id']}.")

    if should_commit:
        writer.commit()

    if update_metadata:
        upsert_document_metadata(
            index_name,
            build_document_metadata_entry(
                index_name,
                index_document,
                source=source,
                cloud_relative_path=cloud_relative_path,
                virtual_path=virtual_path,
                content_hash=index_document.get("content_hash"),
                file_hash=file_hash,
                file_info_path=indexed_path or file_path,
            ),
        )

    return True


@app.route('/delete-document', methods=['POST'])
def delete_document():
    try:
        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        doc_id = data.get("id")

        if not index_name or not doc_id:
            return jsonify({"error": "Missing 'index_name' or 'id'"}), 400
        ensure_index_access(index_name, action="write")

        task_id = str(uuid.uuid4())
        update_task_status(task_id, "queued", message="Queued delete-document task", index_name=index_name)

        task_queue.enqueue(
            index_name,
            process_delete_document,
            index_name,
            doc_id,
            task_id
        )

        return jsonify({"message": "Delete task queued", "task_id": task_id}), 202

    except PermissionError as e:
        logger.warning(f"Forbidden delete request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid delete request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /delete-document: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500


def process_delete_document(index_name, doc_id, task_id):
    try:
        update_task_status(task_id, "processing", message="Deleting document...", index_name=index_name)

        ix, index_name = create_or_open_index(index_name,check_access=False)

        # Find the document path before deleting
        doc_path = None
        doc_metadata = get_document_metadata_entry(index_name, doc_id)
        with ix.searcher() as searcher:
            results = searcher.document(id=doc_id)
            if results:
                doc_path = results.get("path")

        delete_cloud_provider_document(index_name, doc_metadata)

        writer = ix.writer()
        writer.delete_by_term("id", doc_id)
        writer.commit()
        remove_document_metadata(index_name, doc_id)
        logger.info(f"Deleted document with ID {doc_id} from index '{index_name}'.")

        # Move file to deleted folder (organized by index name)
        deleted_path = None
        if doc_path and os.path.exists(doc_path):
            deleted_folder = os.path.join(DELETED_FOLDER, index_name)
            os.makedirs(deleted_folder, exist_ok=True)

            filename = os.path.basename(doc_path)
            destination = os.path.join(deleted_folder, filename)

            # Ensure unique filename
            counter = 1
            while os.path.exists(destination):
                base, ext = os.path.splitext(filename)
                destination = os.path.join(deleted_folder, f"{base}_{counter}{ext}")
                counter += 1

            shutil.move(doc_path, destination)
            deleted_path = destination
            logger.info(f"Moved deleted file to: {destination}")

        update_task_status(task_id, "done", result={
            "message": f"Document deleted from index '{index_name}' and moved to backup.",
            "deleted_path": deleted_path
        }, index_name=index_name)

    except Exception as e:
        logger.error(f"Error in delete task: {e}", exc_info=True)
        update_task_status(task_id, "failed", message=f"Delete task failed: {e}", index_name=index_name)


#takes zip or pdf, applies odr-indexing pipeline to all members
@app.route('/add-document', methods=['POST'])
def add_document():
    try:
    # Ensure file is provided
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
    #ensure index is selected
        index_name = request.form.get("index_name")
        if not index_name:
            return jsonify({"error": "Index name is required"}), 400
        ensure_index_access(index_name, action="write")
        ensure_index_accepts_uploads(index_name)


    # Save uploaded file to a temp folder
        filename = secure_filename(file.filename)
        tmp_index_path = os.path.join(TMP_FOLDER, validate_index_name(index_name))
        os.makedirs(tmp_index_path, exist_ok=True)
        temp_file_path = os.path.join(tmp_index_path, filename)
        file.save(temp_file_path)
        logger.info(f"📥 Saved file for processing: {temp_file_path}")



        # Enqueue the processing!!! All tasks that need exclusive index access (e.g. write) need queuing
        # Generate a task ID
        task_id = str(uuid.uuid4())
        update_task_status(task_id, "queued", message="Queued add-document task", index_name=index_name)
        notify_coordinator("task_queued", index_name, task_id, "upload", {"source_file": filename})

        # Enqueue task for background processing, see "process_uploaded_file" below
        task_queue.enqueue(
            index_name,
            process_uploaded_file,
            index_name,
            temp_file_path,
            filename,
            task_id,
            getattr(g, "skip_antivirus", False)
        )

        return jsonify({
            "message": "Document is being processed.",
            "task_id": task_id
        }), 202

    except PermissionError as e:
        logger.warning(f"Forbidden add-document request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid add-document request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /add-document: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500


def scan_uploaded_file_for_task(file_path, filename, task_id, index_name):
    update_task_status(task_id, "processing", message="Waiting for antivirus scanner...", result={
        "source_file": filename
    }, index_name=index_name)

    is_ready, readiness_message = wait_for_clamav(return_message=True)
    if not is_ready:
        failure_message = f"Antivirus not ready: {readiness_message}"
        logger.warning(failure_message)
        update_task_status(task_id, "failed", message="Antivirus not ready", result={
            "source_file": filename,
            "failed_files": [filename],
            "failed_details": [{"file": filename, "error": failure_message}]
        }, index_name=index_name)
        return False

    update_task_status(task_id, "processing", message="Scanning uploaded file for viruses...", result={
        "source_file": filename
    }, index_name=index_name)
    is_clean, message = scan_with_clamav(file_path)
    if not is_clean:
        logger.warning(f"File failed virus scan: {message}")
        update_task_status(task_id, "failed", message="Virus found in uploaded File!!!", result={
            "source_file": filename,
            "failed_files": [filename],
            "failed_details": [{"file": filename, "error": message}]
        }, index_name=index_name)
        return False

    return True


def prepare_pdf_for_merge(file_path, coordinator_task_id=None):
    processed_path = file_path
    generated_paths = []
    should_pause = make_coordinator_pause_checker(coordinator_task_id)

    try:
        if should_pause:
            should_pause()

        if not is_pdf_searchable(processed_path):
            base_path, extension = os.path.splitext(processed_path)
            ocr_output_path = f"{base_path}_ocr{extension or '.pdf'}"
            generated_paths.append(ocr_output_path)
            try:
                convert_pdf_to_searchable(processed_path, ocr_output_path, should_pause=should_pause)
                processed_path = ocr_output_path
            except CoordinatorPauseRequested:
                raise
            except Exception as e:
                logger.error(f"OCR failed: {e}")
                raise RuntimeError(f"OCR failed for {file_path}: {e}")

        compressed_path = os.path.join(TMP_FOLDER, f"{uuid.uuid4().hex}_{os.path.basename(processed_path)}")
        generated_paths.append(compressed_path)
        try:
            compress_pdf(processed_path, compressed_path, should_pause=should_pause)
        except CoordinatorPauseRequested:
            raise
        except Exception as e:
            logger.error(f"Compression failed: {e}")
            raise RuntimeError(f"Compression failed for {file_path}: {e}")

        return compressed_path, generated_paths
    except CoordinatorPauseRequested:
        for generated_path in generated_paths:
            if generated_path and os.path.exists(generated_path):
                try:
                    os.remove(generated_path)
                except OSError:
                    logger.warning(f"Failed to remove paused append temporary file: {generated_path}", exc_info=True)
        raise


def merge_pdf_files(source_paths, output_path):
    writer = PdfWriter()

    for source_path in source_paths:
        reader = PdfReader(source_path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:
                raise RuntimeError(f"Failed to decrypt PDF for merging: {source_path}: {e}")

        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as output_file:
        writer.write(output_file)


def create_append_backup(file_path, index_name):
    backup_folder = os.path.join(DELETED_FOLDER, validate_index_name(index_name))
    os.makedirs(backup_folder, exist_ok=True)

    base_name, extension = os.path.splitext(os.path.basename(file_path))
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    backup_path = os.path.join(backup_folder, f"{base_name}_before_append_{timestamp}{extension}")

    counter = 1
    while os.path.exists(backup_path):
        backup_path = os.path.join(backup_folder, f"{base_name}_before_append_{timestamp}_{counter}{extension}")
        counter += 1

    shutil.copy2(file_path, backup_path)
    return backup_path


def replace_file_via_target_folder(source_path, target_path):
    target_folder = os.path.dirname(target_path)
    target_name = os.path.basename(target_path)
    staged_path = os.path.join(target_folder, f".{uuid.uuid4().hex}_{target_name}.tmp")

    try:
        shutil.copy2(source_path, staged_path)
        os.replace(staged_path, target_path)
    finally:
        if os.path.exists(staged_path):
            os.remove(staged_path)


@app.route('/append-document', methods=['POST'])
def append_document():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        index_name = request.form.get("index_name")
        doc_id = request.form.get("id")
        if not index_name or not doc_id:
            return jsonify({"error": "index_name and id are required"}), 400
        ensure_index_access(index_name, action="write")

        filename = secure_filename(file.filename)
        if not filename.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF files can be appended"}), 400

        tmp_index_path = os.path.join(TMP_FOLDER, validate_index_name(index_name))
        os.makedirs(tmp_index_path, exist_ok=True)
        temp_file_path = os.path.join(tmp_index_path, f"{uuid.uuid4().hex}_{filename}")
        file.save(temp_file_path)
        logger.info(f"Saved PDF for append processing: {temp_file_path}")

        task_id = str(uuid.uuid4())
        update_task_status(task_id, "queued", message="Queued append-document task", index_name=index_name)
        notify_coordinator("task_queued", index_name, task_id, "append", {
            "source_file": filename,
            "doc_id": doc_id,
        })

        task_queue.enqueue(
            index_name,
            process_append_document,
            index_name,
            doc_id,
            temp_file_path,
            filename,
            task_id,
            getattr(g, "skip_antivirus", False)
        )

        return jsonify({
            "message": "Document append is being processed.",
            "task_id": task_id
        }), 202

    except PermissionError as e:
        logger.warning(f"Forbidden append request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid append request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /append-document: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500


def process_append_document(index_name, doc_id, append_file_path, append_filename, task_id, skip_av=False):
    cleanup_paths = [append_file_path]
    backup_path = None

    try:
        wait_before_indexing_task(
            index_name,
            task_id,
            "append",
            pause_state={
                "job_type": "append",
                "phase": "before_start",
                "index_name": index_name,
                "doc_id": doc_id,
                "append_file_path": append_file_path,
                "append_filename": append_filename,
                "skip_av": skip_av,
            },
            result_updates={"source_file": append_filename},
        )
        notify_coordinator("task_started", index_name, task_id, "append", {
            "source_file": append_filename,
            "doc_id": doc_id,
        })

        if not skip_av:
            if not scan_uploaded_file_for_task(append_file_path, append_filename, task_id, index_name):
                notify_coordinator("task_failed", index_name, task_id, "append", {
                    "source_file": append_filename,
                    "doc_id": doc_id,
                    "error": "Antivirus scan failed",
                })
                return

        update_task_status(task_id, "processing", message="Appending PDF to existing document...", result={
            "source_file": append_filename
        }, index_name=index_name)

        ix, index_name = create_or_open_index(index_name, check_access=False)

        with ix.searcher() as searcher:
            target_doc = searcher.document(id=doc_id)

        if not target_doc:
            update_task_status(task_id, "failed", message="Target document not found", result={
                "source_file": append_filename,
                "failed_files": [append_filename],
                "failed_details": [{"file": append_filename, "error": "Target document not found"}]
            }, index_name=index_name)
            notify_coordinator("task_failed", index_name, task_id, "append", {
                "source_file": append_filename,
                "doc_id": doc_id,
                "error": "Target document not found",
            })
            return

        target_metadata = get_document_metadata_entry(index_name, doc_id) or {}
        target_path = validate_pdf_path_for_index(target_doc.get("path"), index_name)
        target_title = target_doc.get("title") or os.path.basename(target_path)

        append_pdf_path, generated_paths = prepare_pdf_for_merge(append_file_path)
        cleanup_paths.extend(generated_paths)

        merged_path = os.path.join(TMP_FOLDER, f"{uuid.uuid4().hex}_merged.pdf")
        cleanup_paths.append(merged_path)
        merge_pdf_files([target_path, append_pdf_path], merged_path)

        final_merged_path = os.path.join(TMP_FOLDER, f"{uuid.uuid4().hex}_{os.path.basename(target_path)}")
        cleanup_paths.append(final_merged_path)
        compress_pdf(merged_path, final_merged_path)

        replacement_document = build_index_document(
            final_merged_path,
            target_title,
            indexed_path=target_path,
            document_id=doc_id if target_metadata.get("source") == "cloud" else None,
        )

        with ix.searcher() as searcher:
            duplicate_doc = searcher.document(id=replacement_document["id"])
            if duplicate_doc and duplicate_doc.get("id") != doc_id:
                raise DuplicateDocumentError("Merged document duplicates another indexed document")

        backup_path = create_append_backup(target_path, index_name)
        writer = None
        try:
            replace_file_via_target_folder(final_merged_path, target_path)
            sync_cloud_provider_document_update(index_name, target_metadata, target_path)
            writer = ix.writer()
            writer.delete_by_term("id", doc_id)
            writer.update_document(**index_document_for_schema(ix, replacement_document))
            writer.commit()
        except Exception:
            if writer:
                try:
                    writer.cancel()
                except Exception:
                    logger.warning("Failed to cancel append index writer", exc_info=True)
            if backup_path and os.path.exists(backup_path):
                shutil.copy2(backup_path, target_path)
                try:
                    sync_cloud_provider_document_update(index_name, target_metadata, target_path)
                except Exception:
                    logger.warning("Failed to restore cloud document after append failure", exc_info=True)
            raise

        remove_document_metadata(index_name, doc_id)
        source = target_metadata.get("source") or "standalone"
        cloud_relative_path = target_metadata.get("cloud_relative_path") or ""
        virtual_path = target_metadata.get("virtual_path") or ""
        try:
            replacement_file_hash = calculate_file_hash(target_path)
        except Exception:
            replacement_file_hash = target_metadata.get("file_hash") or ""
        upsert_document_metadata(
            index_name,
            build_document_metadata_entry(
                index_name,
                replacement_document,
                source=source,
                cloud_relative_path=cloud_relative_path,
                virtual_path=virtual_path,
                content_hash=replacement_document.get("content_hash"),
                file_hash=replacement_file_hash,
                file_info_path=target_path,
            ),
        )

        update_task_status(task_id, "done", message="Append completed successfully.", result={
            "message": "PDF appended and document reindexed.",
            "action": "append",
            "source_file": append_filename,
            "appended_to": target_title,
            "path": target_path,
            "old_id": doc_id,
            "new_id": replacement_document["id"],
            "backup_path": backup_path
        }, index_name=index_name)
        clear_paused_indexing_task(task_id)
        notify_coordinator("task_finished", index_name, task_id, "append", {
            "source_file": append_filename,
            "doc_id": doc_id,
        })

    except Exception as e:
        logger.error(f"Error in append task: {e}", exc_info=True)
        update_task_status(task_id, "failed", message=f"Append task failed: {e}", result={
            "source_file": append_filename,
            "failed_files": [append_filename],
            "failed_details": [{"file": append_filename, "error": str(e)}],
            "backup_path": backup_path
        }, index_name=index_name)
        notify_coordinator("task_failed", index_name, task_id, "append", {
            "source_file": append_filename,
            "doc_id": doc_id,
            "error": str(e),
        })
    finally:
        for path in cleanup_paths:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    logger.warning(f"Failed to remove temporary append file: {path}", exc_info=True)


# in a task queue: process an uploaded pdf or zip
#can be improved to remove code duplication between zips and pdfs
#can be extended to work for any kind of file type
def process_uploaded_file(index_name, temp_file_path, filename, task_id,skip_av=False):
    try:
        wait_before_indexing_task(
            index_name,
            task_id,
            "upload",
            pause_state={
                "job_type": "upload",
                "phase": "before_start",
                "index_name": index_name,
                "temp_file_path": temp_file_path,
                "filename": filename,
                "skip_av": skip_av,
            },
            result_updates={"source_file": filename},
        )
        notify_coordinator("task_started", index_name, task_id, "upload", {"source_file": filename})

        #skip AV when testing
        if not skip_av:
            update_task_status(task_id, "processing", message="Waiting for antivirus scanner...", result={
                "source_file": filename
            }, index_name=index_name)

            is_ready, readiness_message = wait_for_clamav(return_message=True)
            if not is_ready:
                failure_message = f"Antivirus not ready: {readiness_message}"
                logger.warning(failure_message)
                update_task_status(task_id, "failed", message="Antivirus not ready", result={
                    "source_file": filename,
                    "failed_files": [filename],
                    "failed_details": [{"file": filename, "error": failure_message}]
                }, index_name=index_name)
                notify_coordinator("task_failed", index_name, task_id, "upload", {
                    "source_file": filename,
                    "error": failure_message,
                })
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                return

            update_task_status(task_id, "processing", message="Scanning uploaded file for viruses...", result={
                "source_file": filename
            }, index_name=index_name)
            is_clean, message = scan_with_clamav(temp_file_path)
            if not is_clean:
                logger.warning(f"File failed virus scan: {message}")
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                update_task_status(task_id, "failed", message="Virus found in uploaded File!!!", result={
                    "source_file": filename,
                    "failed_files": [filename],
                    "failed_details": [{"file": filename, "error": message}]
                }, index_name=index_name)
                notify_coordinator("task_failed", index_name, task_id, "upload", {
                    "source_file": filename,
                    "error": message,
                })
                return



        update_task_status(task_id, "processing", message="Antivirus scan passed. Processing started...",index_name=index_name)
        #IN CASE UPLOADED DOCUMENT IS A ZIP FILE
        if filename.lower().endswith(".zip"):
            extract_dir = os.path.join(os.path.dirname(temp_file_path), f"extracted_{task_id}")
            os.makedirs(extract_dir, exist_ok=True)

            with zipfile.ZipFile(temp_file_path, 'r') as zip_ref:
                extract_zip_safely(zip_ref, extract_dir)

            duplicate_count = 0
            processed_count = 0
            processed_files = []
            duplicate_files = []
            failed_files = []
            failed_details = []

            ix, index_name = create_or_open_index(index_name,check_access=False)
            writer = ix.writer()
            durations = []
            pdf_files = []
            for root, _, files in os.walk(extract_dir):
                for extracted_file in sorted(files):
                    extracted_file_path = os.path.normpath(os.path.join(root, extracted_file))
                    #SKIP MACOS INVISIBLE PDF FILES
                    if (
                        "__MACOSX" in extracted_file_path or
                        os.path.basename(extracted_file).startswith("._") or
                        not extracted_file.lower().endswith(".pdf")
                    ):
                        continue
                    pdf_files.append(extracted_file_path)

            try:
                position = 0
                while position < len(pdf_files):
                    extracted_file_path = pdf_files[position]
                    extracted_file = os.path.basename(extracted_file_path)
                    started_at = time.monotonic()
                    #PROCESS A SINGLE PDF FILE
                    try:
                        result = process_single_file(
                            extracted_file_path,
                            index_name,
                            ix=ix,
                            writer=writer,
                            coordinator_task_id=task_id,
                        )
                        #is it already indexed? Count duplicates and failed files
                        if isinstance(result, str) and "duplicate" in result:
                            duplicate_count += 1
                            duplicate_files.append(extracted_file)
                        else:
                            processed_count += 1
                            processed_files.append(extracted_file)
                    except CoordinatorPauseRequested as pause:
                        durations.append(time.monotonic() - started_at)
                        remaining_files = pdf_files[position:]
                        estimated_seconds = estimate_remaining_indexing_seconds(
                            len(remaining_files),
                            durations,
                        )
                        result_updates = {
                            "source_file": filename,
                            "processed_files": processed_files,
                            "duplicates_skipped": duplicate_count,
                            "duplicate_files": duplicate_files,
                            "failed_files": failed_files,
                            "failed_details": failed_details,
                            **build_coordinator_result(pause.status, remaining_files, estimated_seconds),
                        }
                        if writer:
                            writer.commit()
                            writer = None
                        release_current_job_and_wait_for_resume(
                            index_name,
                            task_id,
                            "upload_zip",
                            pause.status,
                            remaining_files,
                            estimated_seconds,
                            {
                                "job_type": "upload_zip",
                                "phase": "remaining_documents",
                                "index_name": index_name,
                                "temp_file_path": temp_file_path,
                                "filename": filename,
                                "extract_dir": extract_dir,
                                "remaining_files": remaining_files,
                                "processed_files": processed_files,
                                "duplicate_files": duplicate_files,
                                "failed_files": failed_files,
                                "failed_details": failed_details,
                                "skip_av": skip_av,
                            },
                            result_updates,
                            "ZIP indexing paused after the current page because Jellyfin playback became active.",
                            "Coordinator cleared. Resuming ZIP indexing...",
                        )
                        ix, index_name = create_or_open_index(index_name, check_access=False)
                        writer = ix.writer()
                        continue
                    except Exception as e:
                        logger.error(f"Error processing {extracted_file_path}: {e}")
                        failed_files.append(extracted_file)
                        failed_details.append({"file": extracted_file, "error": str(e)})
                        durations.append(time.monotonic() - started_at)
                    else:
                        durations.append(time.monotonic() - started_at)

                    remaining_files = pdf_files[position + 1:]
                    should_pause, status, estimated_seconds = should_pause_indexing_batch(
                        remaining_files,
                        durations,
                        task_id=task_id,
                    )
                    if status and status.get("restricted"):
                        result_updates = {
                            "source_file": filename,
                            "processed_files": processed_files,
                            "duplicates_skipped": duplicate_count,
                            "duplicate_files": duplicate_files,
                            "failed_files": failed_files,
                            "failed_details": failed_details,
                            **build_coordinator_result(status, remaining_files, estimated_seconds),
                        }

                        if should_pause:
                            writer.commit()
                            writer = None
                            release_current_job_and_wait_for_resume(
                                index_name,
                                task_id,
                                "upload_zip",
                                status,
                                remaining_files,
                                estimated_seconds,
                                {
                                    "job_type": "upload_zip",
                                    "phase": "remaining_documents",
                                    "index_name": index_name,
                                    "temp_file_path": temp_file_path,
                                    "filename": filename,
                                    "extract_dir": extract_dir,
                                    "remaining_files": remaining_files,
                                    "processed_files": processed_files,
                                    "duplicate_files": duplicate_files,
                                    "failed_files": failed_files,
                                    "failed_details": failed_details,
                                    "skip_av": skip_av,
                                },
                                result_updates,
                                "ZIP indexing paused after the current document because heavy jobs are restricted.",
                                "Coordinator cleared. Resuming ZIP indexing...",
                            )
                            ix, index_name = create_or_open_index(index_name, check_access=False)
                            writer = ix.writer()
                        else:
                            update_task_status(
                                task_id,
                                "processing",
                                message="Coordinator restriction detected, but remaining ZIP indexing is below the wrap-up threshold.",
                                result=task_result_with_updates(task_id, **result_updates),
                                index_name=index_name,
                                coordinator=public_coordinator_status(status),
                            )
                    position += 1
            finally:
                #cleanup download directory in case of errors
                if writer:
                    writer.commit()
                shutil.rmtree(extract_dir)
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
            #return results and update task status to done to free up access to index
            clear_paused_indexing_task(task_id)
            update_task_status(task_id, "done", result={
                "message": f"ZIP processed. {processed_count} files indexed, {duplicate_count} duplicates skipped, {len(failed_files)} failed.",
                "source_file": filename,
                "processed_files": processed_files,
                "duplicates_skipped": duplicate_count,
                "duplicate_files": duplicate_files,
                "failed_files": failed_files,
                "failed_details": failed_details
            },index_name=index_name)
            notify_coordinator("task_finished", index_name, task_id, "upload_zip", {
                "source_file": filename,
                "processed_count": processed_count,
                "duplicates_skipped": duplicate_count,
                "failed_count": len(failed_files),
            })
            return
        #CASE ITS A SINGLE PDF
        elif filename.lower().endswith(".pdf"):
            while True:
                try:
                    result = process_single_file(temp_file_path, index_name, coordinator_task_id=task_id)
                    break
                except CoordinatorPauseRequested as pause:
                    remaining_files = [temp_file_path]
                    estimated_seconds = estimate_remaining_indexing_seconds(len(remaining_files))
                    result_updates = {
                        "source_file": filename,
                        **build_coordinator_result(pause.status, remaining_files, estimated_seconds),
                    }
                    release_current_job_and_wait_for_resume(
                        index_name,
                        task_id,
                        "upload_pdf",
                        pause.status,
                        remaining_files,
                        estimated_seconds,
                        {
                            "job_type": "upload",
                            "phase": "before_start",
                            "index_name": index_name,
                            "temp_file_path": temp_file_path,
                            "filename": filename,
                            "skip_av": skip_av,
                        },
                        result_updates,
                        "PDF indexing paused after the current page because Jellyfin playback became active.",
                        "Coordinator cleared. Resuming PDF indexing...",
                    )
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            if isinstance(result, str) and "duplicate" in result:
                clear_paused_indexing_task(task_id)
                update_task_status(task_id, "done", message="Duplicate skipped", result={
                    "message": "Duplicate skipped.",
                    "source_file": filename,
                    "processed_files": [],
                    "duplicates_skipped": 1,
                    "duplicate_files": [filename],
                    "failed_files": []
                }, index_name=index_name)
            else:
                clear_paused_indexing_task(task_id)
                update_task_status(task_id, "done", message="PDF processed and indexed", result={
                    "message": "PDF processed and indexed.",
                    "source_file": filename,
                    "processed_files": [filename],
                    "duplicates_skipped": 0,
                    "duplicate_files": [],
                    "failed_files": []
                }, index_name=index_name)
            notify_coordinator("task_finished", index_name, task_id, "upload_pdf", {"source_file": filename})
            return
        #CASE its not a supported file type
        else:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            clear_paused_indexing_task(task_id)
            update_task_status(task_id, "failed", message="Unsupported file type", result={
                "source_file": filename,
                "failed_files": [filename],
                "failed_details": [{"file": filename, "error": "Unsupported file type"}]
            }, index_name=index_name)
            notify_coordinator("task_failed", index_name, task_id, "upload", {
                "source_file": filename,
                "error": "Unsupported file type",
            })
            return

    except Exception as e:
        logger.error(f"Error processing uploaded file: {e}", exc_info=True)
        update_task_status(task_id, "failed", message=f"Exception: {e}", result={
            "source_file": filename,
            "failed_files": [filename],
            "failed_details": [{"file": filename, "error": str(e)}]
        }, index_name=index_name)
        notify_coordinator("task_failed", index_name, task_id, "upload", {
            "source_file": filename,
            "error": str(e),
        })


@app.route('/process-tmp', methods=['POST'])
def process_tmp():
    try:
        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        if not index_name:
            return jsonify({"error": "Index name is required"}), 400
        ensure_index_access(index_name, action="write")
        ensure_index_accepts_uploads(index_name)

        task_id = str(uuid.uuid4())
        update_task_status(task_id, "queued", message="Queued TMP folder processing", index_name=index_name)
        notify_coordinator("task_queued", index_name, task_id, "process_tmp")
        task_queue.enqueue(
            index_name,
            process_tmp_folder,
            index_name,
            task_id,
            getattr(g, "skip_antivirus", False)
        )
        return jsonify({
            "message": f"TMP processing queued for index '{index_name}'",
            "task_id": task_id
        }), 202
    except PermissionError as e:
        logger.warning(f"Forbidden process-tmp request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid process-tmp request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /process-tmp: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500


def process_tmp_folder(index_name, task_id, skip_av=False):
    tmp_index_path = os.path.join(TMP_FOLDER, validate_index_name(index_name))
    processed_files = []
    duplicate_files = []
    failed_files = []
    failed_details = []

    try:
        wait_before_indexing_task(
            index_name,
            task_id,
            "process_tmp",
            pause_state={
                "job_type": "process_tmp",
                "phase": "before_start",
                "index_name": index_name,
                "skip_av": skip_av,
            },
        )
        notify_coordinator("task_started", index_name, task_id, "process_tmp")
        update_task_status(task_id, "processing", message="Processing TMP folder...", index_name=index_name)

        if not os.path.exists(tmp_index_path):
            update_task_status(task_id, "done", result={
                "message": "TMP folder does not exist; nothing to process.",
                "processed_files": processed_files,
                "failed_files": failed_files
            }, index_name=index_name)
            notify_coordinator("task_finished", index_name, task_id, "process_tmp", {
                "processed_count": 0,
                "failed_count": 0,
            })
            return

        candidates = []
        for root, _, files in os.walk(tmp_index_path):
            if os.path.basename(root).startswith("extracted"):
                continue
            for filename in files:
                if filename.lower().endswith((".pdf", ".zip")):
                    candidates.append(os.path.join(root, filename))
        candidates.sort()

        av_ready = True
        av_readiness_message = "Antivirus check skipped"
        if candidates and not skip_av:
            update_task_status(task_id, "processing", message="Waiting for antivirus scanner...", result={
                "candidate_files": [os.path.basename(path) for path in candidates]
            }, index_name=index_name)
            av_ready, av_readiness_message = wait_for_clamav(return_message=True)
            if av_ready:
                update_task_status(task_id, "processing", message="Antivirus ready. Processing TMP folder...", result={
                    "candidate_files": [os.path.basename(path) for path in candidates]
                }, index_name=index_name)
            else:
                logger.warning(f"Antivirus not ready for TMP processing: {av_readiness_message}")

        position = 0
        while position < len(candidates):
            file_path = candidates[position]
            if not os.path.exists(file_path):
                position += 1
                continue

            filename = os.path.basename(file_path)
            try:
                if not skip_av:
                    if not av_ready:
                        raise RuntimeError(f"Antivirus not ready: {av_readiness_message}")
                    is_clean, message = scan_with_clamav(file_path)
                    if not is_clean:
                        raise RuntimeError(message)

                if filename.lower().endswith(".pdf"):
                    result = process_single_file(file_path, index_name, coordinator_task_id=task_id)
                    if result == "duplicate":
                        duplicate_files.append(filename)
                    else:
                        processed_files.append(filename)
                else:
                    extract_dir = os.path.join(os.path.dirname(file_path), f"extracted_{uuid.uuid4().hex}")
                    os.makedirs(extract_dir, exist_ok=True)
                    try:
                        with zipfile.ZipFile(file_path, "r") as zip_ref:
                            extract_zip_safely(zip_ref, extract_dir)

                        for root, _, files in os.walk(extract_dir):
                            for extracted_file in files:
                                extracted_file_path = os.path.join(root, extracted_file)
                                if (
                                    "__MACOSX" in extracted_file_path or
                                    os.path.basename(extracted_file).startswith("._") or
                                    not extracted_file.lower().endswith(".pdf")
                                ):
                                    continue

                                result = process_single_file(extracted_file_path, index_name, coordinator_task_id=task_id)
                                if result == "duplicate":
                                    duplicate_files.append(extracted_file)
                                else:
                                    processed_files.append(extracted_file)
                        os.remove(file_path)
                    finally:
                        shutil.rmtree(extract_dir, ignore_errors=True)
            except CoordinatorPauseRequested as pause:
                remaining_files = candidates[position:]
                estimated_seconds = estimate_remaining_indexing_seconds(len(remaining_files))
                result_updates = {
                    "processed_files": processed_files,
                    "duplicates_skipped": len(duplicate_files),
                    "duplicate_files": duplicate_files,
                    "failed_files": failed_files,
                    "failed_details": failed_details,
                    **build_coordinator_result(pause.status, remaining_files, estimated_seconds),
                }
                release_current_job_and_wait_for_resume(
                    index_name,
                    task_id,
                    "process_tmp",
                    pause.status,
                    remaining_files,
                    estimated_seconds,
                    {
                        "job_type": "process_tmp_remaining",
                        "phase": "remaining_candidates",
                        "index_name": index_name,
                        "remaining_files": remaining_files,
                        "processed_files": processed_files,
                        "duplicate_files": duplicate_files,
                        "failed_files": failed_files,
                        "failed_details": failed_details,
                        "skip_av": skip_av,
                        "av_ready": av_ready,
                        "av_readiness_message": av_readiness_message,
                    },
                    result_updates,
                    "TMP indexing paused after the current page because Jellyfin playback became active.",
                    "Coordinator cleared. Resuming TMP indexing...",
                )
                continue
            except Exception as e:
                logger.error(f"Error processing TMP file {file_path}: {e}", exc_info=True)
                failed_files.append(filename)
                failed_details.append({"file": filename, "error": str(e)})

            remaining_files = candidates[position + 1:]
            pause_indexing_batch(
                index_name,
                task_id,
                "process_tmp",
                remaining_files,
                pause_state={
                    "job_type": "process_tmp_remaining",
                    "phase": "remaining_candidates",
                    "index_name": index_name,
                    "remaining_files": remaining_files,
                    "processed_files": processed_files,
                    "duplicate_files": duplicate_files,
                    "failed_files": failed_files,
                    "failed_details": failed_details,
                    "skip_av": skip_av,
                    "av_ready": av_ready,
                    "av_readiness_message": av_readiness_message,
                },
            )
            position += 1

        clear_paused_indexing_task(task_id)
        update_task_status(task_id, "done", result={
            "message": f"TMP processing complete. {len(processed_files)} files indexed, {len(duplicate_files)} duplicates skipped, {len(failed_files)} failed.",
            "processed_files": processed_files,
            "duplicates_skipped": len(duplicate_files),
            "duplicate_files": duplicate_files,
            "failed_files": failed_files,
            "failed_details": failed_details
        }, index_name=index_name)
        notify_coordinator("task_finished", index_name, task_id, "process_tmp", {
            "processed_count": len(processed_files),
            "duplicates_skipped": len(duplicate_files),
            "failed_count": len(failed_files),
        })
    except Exception as e:
        logger.error(f"Error in TMP processing task: {e}", exc_info=True)
        update_task_status(task_id, "failed", message=f"TMP processing failed: {e}", index_name=index_name)
        notify_coordinator("task_failed", index_name, task_id, "process_tmp", {"error": str(e)})


def list_cloud_pdf_files(index_name, config=None):
    config = config or load_index_config(index_name)
    cloud = config.get("cloud") or {}
    root_path = os.path.realpath(os.path.normpath(cloud.get("root_path") or ""))
    if not root_path or not os.path.isdir(root_path):
        raise FileNotFoundError(f"Cloud root path not found: {root_path}")
    preview_root = os.path.realpath(os.path.normpath(cloud_preview_folder(index_name, config) or ""))

    pdf_files = []
    for root, dirs, files in os.walk(root_path):
        dirs[:] = [
            dirname
            for dirname in dirs
            if (
                dirname != CLOUD_INTERNAL_FOLDER
                and not dirname.startswith("._")
                and not path_within(preview_root, os.path.join(root, dirname))
            )
        ]
        for filename in files:
            if filename.startswith("._") or not filename.lower().endswith(".pdf"):
                continue
            absolute_path = os.path.join(root, filename)
            relative_path = normalize_virtual_path(os.path.relpath(absolute_path, root_path))
            if not relative_path:
                continue
            pdf_files.append({
                "path": absolute_path,
                "relative_path": relative_path,
                "title": filename,
            })
    pdf_files.sort(key=lambda item: item["relative_path"].lower())
    return pdf_files


def prepare_cloud_pdf_for_indexing(file_path, task_id=None):
    if pdf_missing_readable_page_tree(file_path):
        logger.warning(
            "Skipping cloud OCR for PDF without a readable page tree: %s: %s",
            file_path,
            PDF_MISSING_PAGES_MESSAGE,
        )
        return file_path, []

    if is_pdf_searchable(file_path):
        return file_path, []

    should_pause = make_coordinator_pause_checker(task_id)
    tmp_index_path = os.path.join(TMP_FOLDER, "cloud-sync")
    os.makedirs(tmp_index_path, exist_ok=True)
    ocr_output_path = os.path.join(tmp_index_path, f"{uuid.uuid4().hex}_cloud_ocr.pdf")
    try:
        convert_pdf_to_searchable(file_path, ocr_output_path, should_pause=should_pause)
    except Exception as e:
        if is_missing_pdf_pages_error(e):
            logger.warning(
                "Skipping cloud OCR for PDF without a readable page tree: %s: %s",
                file_path,
                format_pdf_read_error(e),
            )
            return file_path, []
        raise
    return ocr_output_path, [ocr_output_path]


def cloud_pdf_files_from_provider_sync(provider_sync_result):
    if not provider_sync_result:
        return None
    root_path = provider_sync_result.get("local_root_path")
    if not root_path:
        return None

    relative_paths = []
    for key in ("unchanged_files", "downloaded_files"):
        relative_paths.extend(
            normalize_virtual_path(relative_path)
            for relative_path in (provider_sync_result.get(key) or [])
        )

    try:
        checked_remote_files = int(provider_sync_result.get("checked_remote_files"))
    except (TypeError, ValueError):
        return None
    if checked_remote_files != len(relative_paths):
        return None

    pdf_files = [
        {
            "path": cloud_local_document_path(root_path, relative_path),
            "relative_path": relative_path,
            "title": os.path.basename(relative_path),
        }
        for relative_path in relative_paths
    ]
    pdf_files.sort(key=lambda item: item["relative_path"].lower())
    return pdf_files


def archive_replaced_standalone_file(index_name, file_path, reason):
    if not file_path or not os.path.exists(file_path):
        return None

    archive_folder = os.path.join(DELETED_FOLDER, validate_index_name(index_name), reason)
    os.makedirs(archive_folder, exist_ok=True)
    filename = os.path.basename(file_path)
    target_path = os.path.join(archive_folder, filename)
    counter = 1
    while os.path.exists(target_path):
        base, extension = os.path.splitext(filename)
        target_path = os.path.join(archive_folder, f"{base}_{counter}{extension}")
        counter += 1

    if path_within(get_index_pdf_folder(index_name), file_path):
        shutil.move(file_path, target_path)
    else:
        shutil.copy2(file_path, target_path)
        os.remove(file_path)
    return target_path


def find_standalone_duplicate_metadata(metadata_documents, content_hash=None, file_hash=None):
    for document in metadata_documents.values():
        if document.get("source") != "standalone":
            continue
        if file_hash and document.get("file_hash") == file_hash:
            return document
        if content_hash and document.get("content_hash") == content_hash:
            return document
    return None


def find_cloud_metadata_by_relative_path(metadata_documents):
    by_relative_path = {}
    for document in metadata_documents.values():
        if document.get("source") == "cloud" and document.get("cloud_relative_path"):
            by_relative_path[document["cloud_relative_path"]] = document
    return by_relative_path


def delete_index_document(ix, writer, doc_id):
    if not doc_id:
        return
    writer.delete_by_term("id", doc_id)


def provider_sync_has_no_document_changes(provider_sync_result, provider_unchanged_relative_paths):
    if not provider_sync_result:
        return False
    if provider_sync_result.get("downloaded_files") or provider_sync_result.get("removed_local_files"):
        return False
    try:
        checked_remote_files = int(provider_sync_result.get("checked_remote_files"))
    except (TypeError, ValueError):
        return False
    return checked_remote_files == len(provider_unchanged_relative_paths)


def sync_cloud_documents(index_name, task_id, reason="scheduled", manage_coordinator=True):
    config = load_index_config(index_name)
    if not index_has_cloud_source(index_name, config):
        raise ValueError("Index has no cloud source configured")

    validate_index_config_for_save(config)
    provider_sync_result = sync_cloud_provider_to_local(index_name, config)
    provider_unchanged_relative_paths = [
        normalize_virtual_path(relative_path)
        for relative_path in ((provider_sync_result or {}).get("unchanged_files") or [])
    ]
    provider_unchanged_relative_path_set = set(provider_unchanged_relative_paths)

    ix, _ = create_or_open_index(index_name, check_access=False)
    metadata_payload = ensure_document_metadata(index_name, ix=ix)
    metadata_documents = metadata_payload.get("documents", {})
    cloud_metadata_by_relative_path = find_cloud_metadata_by_relative_path(metadata_documents)
    preview_root = cloud_preview_folder(index_name, config)

    if (
        provider_sync_has_no_document_changes(provider_sync_result, provider_unchanged_relative_paths)
        and provider_unchanged_relative_path_set == set(cloud_metadata_by_relative_path)
    ):
        return {
            "added_files": [],
            "updated_files": [],
            "removed_files": [],
            "unchanged_files": [
                build_cloud_virtual_path(index_name, relative_path, config)
                for relative_path in provider_unchanged_relative_paths
            ],
            "duplicate_files": [],
            "deduplicated_files": [],
            "failed_files": [],
            "failed_details": [],
            "checked_documents": len(provider_unchanged_relative_paths),
            "preview_folder": preview_root,
            "provider_sync": provider_sync_result,
            "coordinator_job_type": None,
        }

    cloud_files = cloud_pdf_files_from_provider_sync(provider_sync_result)
    if cloud_files is None:
        cloud_files = list_cloud_pdf_files(index_name, config)
    cloud_by_relative_path = {
        item["relative_path"]: item
        for item in cloud_files
    }

    added_files = []
    updated_files = []
    removed_files = []
    unchanged_files = []
    duplicate_files = []
    deduplicated_files = []
    failed_files = []
    failed_details = []
    durations = []
    metadata_changed = False
    writer = None
    cloud_index_coordinator_started = False

    def ensure_writer():
        nonlocal writer
        if writer is None:
            writer = ix.writer()
        return writer

    def start_cloud_indexing_coordinator(current_relative_path, remaining_files):
        nonlocal cloud_index_coordinator_started
        if cloud_index_coordinator_started or not manage_coordinator:
            return

        virtual_path = build_cloud_virtual_path(index_name, current_relative_path, config)
        result_updates = {
            "reason": reason,
            "coordinator_job_type": CLOUD_INDEX_JOB_TYPE,
            "phase": "indexing",
            "source_file": virtual_path,
            "remaining_documents": len(remaining_files),
        }
        wait_before_indexing_task(
            index_name,
            task_id,
            CLOUD_INDEX_JOB_TYPE,
            pause_state={
                "job_type": CLOUD_INDEX_JOB_TYPE,
                "phase": "before_indexing",
                "index_name": index_name,
                "reason": reason,
                "current_file": current_relative_path,
                "remaining_files": remaining_files,
            },
            result_updates=result_updates,
        )
        cloud_index_coordinator_started = True
        update_task_status(
            task_id,
            "processing",
            message="Indexing changed cloud documents...",
            result=task_result_with_updates(task_id, **result_updates),
            index_name=index_name,
        )
        notify_coordinator(
            "task_started",
            index_name,
            task_id,
            CLOUD_INDEX_JOB_TYPE,
            {
                "reason": reason,
                "source_file": virtual_path,
                "remaining_documents": len(remaining_files),
            },
        )

    try:
        missing_cloud_documents = [
            metadata
            for relative_path, metadata in cloud_metadata_by_relative_path.items()
            if relative_path not in cloud_by_relative_path
        ]
        for metadata in missing_cloud_documents:
            delete_index_document(ix, ensure_writer(), metadata.get("id"))
            metadata_documents.pop(metadata.get("id"), None)
            removed_files.append(metadata.get("virtual_path") or metadata.get("title"))

        for position, cloud_file in enumerate(cloud_files):
            started_at = time.monotonic()
            relative_path = cloud_file["relative_path"]
            file_path = cloud_file["path"]
            title = cloud_file["title"]
            cleanup_paths = []
            try:
                stat_payload = file_stat_payload(file_path)
                existing_metadata = cloud_metadata_by_relative_path.get(relative_path)
                if (
                    existing_metadata
                    and existing_metadata.get("file_hash")
                    and relative_path in provider_unchanged_relative_path_set
                    and metadata_file_stat_matches(existing_metadata, stat_payload)
                ):
                    unchanged_files.append(build_cloud_virtual_path(index_name, relative_path, config))
                    continue

                file_hash = calculate_file_hash(file_path)
                if (
                    existing_metadata
                    and existing_metadata.get("file_hash") == file_hash
                    and existing_metadata.get("size") == stat_payload["size"]
                ):
                    if update_metadata_file_stat(existing_metadata, stat_payload):
                        metadata_changed = True
                    unchanged_files.append(build_cloud_virtual_path(index_name, relative_path, config))
                    continue

                remaining_files = [
                    item["path"]
                    for item in cloud_files[position:]
                ]
                start_cloud_indexing_coordinator(relative_path, remaining_files)
                index_source_path, cleanup_paths = prepare_cloud_pdf_for_indexing(file_path, task_id=task_id)
                document_id = make_cloud_document_id(index_name, relative_path)
                index_document = build_index_document(
                    index_source_path,
                    title,
                    indexed_path=file_path,
                    document_id=document_id,
                    allow_metadata_only=True,
                )

                standalone_duplicate = find_standalone_duplicate_metadata(
                    metadata_documents,
                    content_hash=index_document.get("content_hash"),
                    file_hash=file_hash,
                )
                if standalone_duplicate and config["cloud"].get("delete_standalone_duplicates"):
                    delete_index_document(ix, ensure_writer(), standalone_duplicate.get("id"))
                    archived_path = archive_replaced_standalone_file(
                        index_name,
                        standalone_duplicate.get("path"),
                        "cloud-deduplicated",
                    )
                    metadata_documents.pop(standalone_duplicate.get("id"), None)
                    deduplicated_files.append({
                        "standalone": standalone_duplicate.get("title"),
                        "cloud": build_cloud_virtual_path(index_name, relative_path, config),
                        "archived_path": archived_path,
                    })

                if existing_metadata and existing_metadata.get("id") != document_id:
                    delete_index_document(ix, ensure_writer(), existing_metadata.get("id"))
                    metadata_documents.pop(existing_metadata.get("id"), None)

                ensure_writer().update_document(**index_document_for_schema(ix, index_document))
                metadata_documents[document_id] = build_document_metadata_entry(
                    index_name,
                    index_document,
                    source="cloud",
                    cloud_relative_path=relative_path,
                    virtual_path=build_cloud_virtual_path(index_name, relative_path, config),
                    content_hash=index_document.get("content_hash"),
                    file_hash=file_hash,
                    file_info_path=file_path,
                )
                metadata_changed = True

                if existing_metadata:
                    updated_files.append(build_cloud_virtual_path(index_name, relative_path, config))
                else:
                    added_files.append(build_cloud_virtual_path(index_name, relative_path, config))
            except CoordinatorPauseRequested:
                raise
            except Exception as e:
                logger.error("Failed to sync cloud file %s: %s", file_path, e, exc_info=True)
                failed_files.append(relative_path)
                failed_details.append({"file": relative_path, "error": str(e)})
            finally:
                for cleanup_path in cleanup_paths:
                    if cleanup_path and os.path.exists(cleanup_path):
                        try:
                            os.remove(cleanup_path)
                        except OSError:
                            logger.warning("Failed to remove cloud sync temporary file %s", cleanup_path, exc_info=True)
                durations.append(time.monotonic() - started_at)

            remaining_files = [
                item["path"]
                for item in cloud_files[position + 1:]
            ]
            if cloud_index_coordinator_started or not manage_coordinator:
                pause_indexing_batch(
                    index_name,
                    task_id,
                    CLOUD_INDEX_JOB_TYPE,
                    remaining_files,
                    durations=durations,
                    pause_state={
                        "job_type": CLOUD_INDEX_JOB_TYPE,
                        "phase": "remaining_documents",
                        "index_name": index_name,
                        "reason": reason,
                        "remaining_files": remaining_files,
                    },
                )

        if writer:
            writer.commit()
            writer = None
        if metadata_changed or added_files or updated_files or removed_files or deduplicated_files:
            save_document_metadata(index_name, metadata_payload)
    except Exception:
        if writer:
            try:
                writer.cancel()
            except Exception:
                logger.warning("Failed to cancel cloud sync writer", exc_info=True)
        raise

    return {
        "added_files": added_files,
        "updated_files": updated_files,
        "removed_files": removed_files,
        "unchanged_files": unchanged_files,
        "duplicate_files": duplicate_files,
        "deduplicated_files": deduplicated_files,
        "failed_files": failed_files,
        "failed_details": failed_details,
        "checked_documents": len(cloud_files),
        "preview_folder": preview_root,
        "provider_sync": provider_sync_result,
        "coordinator_job_type": CLOUD_INDEX_JOB_TYPE if cloud_index_coordinator_started else None,
    }


cloud_sync_scheduler_lock = threading.Lock()
cloud_sync_scheduler_thread = None
cloud_sync_index_state = {}


def cloud_sync_task_active(index_name):
    state = cloud_sync_index_state.get(index_name) or {}
    task_id = state.get("task_id")
    if not task_id:
        return False
    status = (task_status.get(task_id) or {}).get("status")
    return status in {"queued", "processing", "blocked", "paused"}


def queue_cloud_sync_task(index_name, reason="scheduled"):
    validate_index_name(index_name)
    if cloud_sync_task_active(index_name):
        return cloud_sync_index_state[index_name]["task_id"]

    task_id = str(uuid.uuid4())
    cloud_sync_index_state[index_name] = {
        "task_id": task_id,
        "queued_at": utc_now_iso(),
        "reason": reason,
    }
    update_task_status(task_id, "queued", message="Queued cloud folder sync", index_name=index_name)
    task_queue.enqueue(index_name, run_cloud_sync_task, index_name, task_id, reason)
    return task_id


def run_cloud_sync_task(index_name, task_id, reason="scheduled"):
    try:
        update_task_status(task_id, "processing", message="Syncing cloud folder...", result={
            "reason": reason
        }, index_name=index_name)
        result = sync_cloud_documents(index_name, task_id, reason=reason)
        message = (
            f"Cloud sync complete. {len(result['added_files'])} added, "
            f"{len(result['updated_files'])} updated, {len(result['removed_files'])} removed, "
            f"{len(result['failed_files'])} failed."
        )
        update_index_cloud_sync_state(index_name, "done", message, task_id=task_id)
        clear_paused_indexing_task(task_id)
        update_task_status(task_id, "done", message="Cloud sync completed.", result={
            "message": message,
            **result,
        }, index_name=index_name)
        coordinator_job_type = result.get("coordinator_job_type")
        if coordinator_job_type:
            notify_coordinator("task_finished", index_name, task_id, coordinator_job_type, {
                "reason": reason,
                "added_count": len(result["added_files"]),
                "updated_count": len(result["updated_files"]),
                "removed_count": len(result["removed_files"]),
                "failed_count": len(result["failed_files"]),
            })
    except Exception as e:
        logger.error("Cloud sync failed for %s: %s", index_name, e, exc_info=True)
        update_index_cloud_sync_state(index_name, "failed", str(e), task_id=task_id)
        existing_result = task_status.get(task_id, {}).get("result") or {}
        update_task_status(task_id, "failed", message=f"Cloud sync failed: {e}", result={
            "reason": reason,
            "failed_details": [{"file": cloud_display_root_name(index_name), "error": str(e)}],
        }, index_name=index_name)
        coordinator_job_type = existing_result.get("coordinator_job_type")
        if coordinator_job_type or coordinator_call("task_has_lease", task_id, default=False):
            notify_coordinator("task_failed", index_name, task_id, coordinator_job_type or CLOUD_INDEX_JOB_TYPE, {
                "reason": reason,
                "error": str(e),
            })
    finally:
        state = cloud_sync_index_state.get(index_name) or {}
        if state.get("task_id") == task_id:
            cloud_sync_index_state.pop(index_name, None)


def cloud_sync_due(index_name, config):
    cloud = config.get("cloud") or {}
    if not cloud.get("sync_enabled") or not index_has_cloud_source(index_name, config):
        return False

    last_sync = parse_utc_datetime(cloud.get("last_sync_at"))
    if not last_sync:
        return True
    interval = max(
        MIN_CLOUD_SYNC_INTERVAL_SECONDS,
        coerce_positive_int(
            cloud.get("sync_interval_seconds"),
            DEFAULT_CLOUD_SYNC_INTERVAL_SECONDS,
        ),
    )
    return (datetime.utcnow() - last_sync).total_seconds() >= interval


def cloud_sync_scheduler_loop():
    sleep_seconds = max(5, CLOUD_SYNC_SCHEDULER_POLL_SECONDS)
    while True:
        try:
            for index_name in discover_indices():
                try:
                    config = load_index_config(index_name)
                    if cloud_sync_due(index_name, config):
                        queue_cloud_sync_task(index_name, reason="scheduled")
                except Exception:
                    logger.warning("Unable to schedule cloud sync for %s", index_name, exc_info=True)
        except Exception:
            logger.error("Cloud sync scheduler error", exc_info=True)
        time.sleep(sleep_seconds)


def ensure_cloud_sync_scheduler_running():
    global cloud_sync_scheduler_thread
    with cloud_sync_scheduler_lock:
        if cloud_sync_scheduler_thread and cloud_sync_scheduler_thread.is_alive():
            return
        cloud_sync_scheduler_thread = threading.Thread(target=cloud_sync_scheduler_loop, daemon=True)
        cloud_sync_scheduler_thread.start()


def build_folder_tree_response(index_name, folder_path, metadata_payload):
    config = load_index_config(index_name)
    documents = list((metadata_payload or {}).get("documents", {}).values())
    folder_path = normalize_virtual_path(folder_path)
    folder_parts = tuple(PurePosixPath(folder_path).parts) if folder_path else tuple()
    child_folders = {}
    files = []

    for document in documents:
        virtual_path = normalize_virtual_path(document.get("virtual_path") or document.get("title"))
        if not virtual_path:
            continue
        parts = tuple(PurePosixPath(virtual_path).parts)
        if len(parts) <= len(folder_parts) or parts[:len(folder_parts)] != folder_parts:
            continue

        if len(parts) == len(folder_parts) + 1:
            files.append({
                "id": document.get("id"),
                "title": document.get("title"),
                "path": document.get("path"),
                "virtual_path": virtual_path,
                "source": document.get("source"),
                "size": document.get("size"),
                "modified_at": document.get("modified_at"),
                "indexed_at": document.get("indexed_at"),
                "years": document.get("years") or [],
            })
            continue

        child_name = parts[len(folder_parts)]
        child_path = "/".join(parts[:len(folder_parts) + 1])
        folder = child_folders.setdefault(child_path, {
            "name": child_name,
            "path": child_path,
            "document_count": 0,
            "source": document.get("source"),
        })
        folder["document_count"] += 1
        if folder.get("source") != document.get("source"):
            folder["source"] = "mixed"

    files.sort(key=lambda item: (item["title"] or "").lower())
    folders = sorted(child_folders.values(), key=lambda item: item["name"].lower())

    breadcrumbs = []
    cumulative = []
    for part in folder_parts:
        cumulative.append(part)
        breadcrumbs.append({
            "name": part,
            "path": "/".join(cumulative),
        })

    return {
        "index_name": index_name,
        "mode": config.get("mode"),
        "path": folder_path,
        "breadcrumbs": breadcrumbs,
        "folders": folders,
        "files": files,
        "total_documents": len(documents),
        "uploads_enabled": index_accepts_uploads(index_name, config),
        "cloud_sync_enabled": index_has_cloud_source(index_name, config),
    }


@app.route("/email-task", methods=["POST"])
def create_email_task():
    try:
        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        if not index_name:
            return jsonify({"error": "Index name is required"}), 400
        ensure_index_access(index_name, action="write")
        ensure_index_accepts_uploads(index_name)

        if MAIL_TOKEN in PUBLIC_MAIL_TOKENS:
            return jsonify({"error": "Set a private MAIL_TOKEN on both services to enable mail import"}), 503

        res = requests.post(f"{MAIL_PROCESSOR_URL}/email-task", json=data)
        return jsonify(res.json()), res.status_code
    except PermissionError as e:
        logger.warning(f"Forbidden email-task request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid email-task request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to start email task: {str(e)}"}), 500

@app.route("/stop-email-task", methods=["POST"])
def stop_email_task():
    try:
        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        if index_name:
            ensure_index_access(index_name, action="write")
        else:
            ensure_admin_access()

        res = requests.post(
            f"{MAIL_PROCESSOR_URL}/stop-email-task",
            json={"task_id": data.get("task_id"), "index_name": index_name},
        )
        return jsonify(res.json()), res.status_code
    except PermissionError as e:
        logger.warning(f"Forbidden stop-email-task request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid stop-email-task request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to stop email task: {str(e)}"}), 500

@app.route("/email-tasks", methods=["GET"])
def list_email_tasks():
    try:
        index_name = request.args.get("index_name")
        if index_name:
            ensure_index_access(index_name)
        elif not get_current_principal()["is_admin"]:
            return jsonify({"tasks": []}), 200

        res = requests.get(f"{MAIL_PROCESSOR_URL}/email-tasks", params=request.args)
        return jsonify(res.json()), res.status_code
    except PermissionError as e:
        logger.warning(f"Forbidden email-tasks request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid email-tasks request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to list email tasks: {str(e)}"}), 500

def process_single_file(file_path, index_name, ix=None, writer=None, coordinator_task_id=None):
    """
    Processes a single file: OCRs, compresses, indexes, and moves the file if successful.
    Deletes original only after all steps are successful.

    Args:
        file_path (str): The path to the PDF file to process.
        index_name (str): The name of the index.
        ix (whoosh.index.Index): Optional shared index instance.
        writer (whoosh.writing.IndexWriter): Optional shared writer instance for batching.

    Returns:
        True on success, or "duplicate" if skipped due to deduplication.
    """
    pdf_index_folder = os.path.join(PDF_FOLDER, index_name)
    os.makedirs(pdf_index_folder, exist_ok=True)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    original_file_path = file_path
    generated_ocr_path = None
    should_pause = make_coordinator_pause_checker(coordinator_task_id)
    if should_pause:
        should_pause()

    # Step 1: OCR if needed
    if not is_pdf_searchable(file_path):
        ocr_output_path = file_path.replace(".pdf", "_ocr.pdf")
        generated_ocr_path = ocr_output_path
        try:
            convert_pdf_to_searchable(file_path, ocr_output_path, should_pause=should_pause)
            file_path = ocr_output_path
        except CoordinatorPauseRequested:
            if os.path.exists(ocr_output_path):
                os.remove(ocr_output_path)
            raise
        except Exception as e:
            logger.error(f"OCR failed: {e}")
            raise RuntimeError(f"OCR failed for {file_path}: {e}")

    # Step 2: Compress
    compressed_path = os.path.join(TMP_FOLDER, os.path.basename(file_path))
    try:
        compress_pdf(file_path, compressed_path, should_pause=should_pause)
    except CoordinatorPauseRequested:
        if os.path.exists(compressed_path):
            os.remove(compressed_path)
        if generated_ocr_path and os.path.exists(generated_ocr_path):
            os.remove(generated_ocr_path)
        raise
    except Exception as e:
        logger.error(f"Compression failed: {e}")
        raise RuntimeError(f"Compression failed for {file_path}: {e}")

    # Step 3: Move to final location
    if should_pause:
        should_pause()
    final_path = move_with_unique_name(compressed_path, pdf_index_folder)

    try:
        # Step 4: Index it (shared or single writer)
        add_to_index(index_name, final_path, os.path.basename(final_path), ix=ix, writer=writer)
    except DuplicateDocumentError as e:
        logger.warning(f"Skipping indexing (duplicate): {e}")
        if os.path.exists(original_file_path):
            os.remove(original_file_path)
            logger.info(f"Original file deleted: {original_file_path}")
        return "duplicate"

    # Step 5: Cleanup original
    if os.path.exists(original_file_path):
        os.remove(original_file_path)
        logger.info(f"Original file deleted: {original_file_path}")

    return True

def move_with_unique_name(file_path, target_folder):
    """
    Moves a file to the target folder, ensuring a unique filename by appending numbers if needed.

    Args:
        file_path (str): Path to the file being moved.
        target_folder (str): Folder where the file should be moved.

    Returns:
        str: The final path of the moved file.
    """
    # Extract the base name and extension
    base_name, file_extension = os.path.splitext(os.path.basename(file_path))
    target_path = os.path.join(target_folder, f"{base_name}{file_extension}")

    # Check for existing files and modify the filename if needed
    counter = 1
    while os.path.exists(target_path):
        target_path = os.path.join(target_folder, f"{base_name}_{counter}{file_extension}")
        counter += 1

    # Ensure the target folder exists
    os.makedirs(target_folder, exist_ok=True)

    # Move the file
    shutil.move(file_path, target_path)
    logging.info(f"File moved to: {target_path}")
    return target_path

def compress_pdf_page(page_path, output_path):
    """
    Force-rasterize a single PDF page using Ghostscript at 300 DPI,
    and fallback to QPDF if Ghostscript fails.

    WARNING: This approach converts all vector/text to images.
    The resulting file may be larger and is no longer "vector-based".
    """

    try:
        # Ghostscript command to force rasterization
        gs_command = [
            "gs",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            "-r300",             # Render pages at 300 DPI
            "-dNOPAUSE",         # Don't pause after each page
            "-dBATCH",           # Run in batch mode
            "-dQUIET",           # Run in quietmode
            f"-sOutputFile={output_path}",
            page_path
        ]

        subprocess.run(gs_command, check=True)
        logger.info(f"Page successfully forced to raster at 300 DPI: {output_path}")
        return True

    except subprocess.CalledProcessError as e:
        logger.warning(f"Ghostscript failed for page {page_path}: {e}. Trying QPDF fallback...")

    # Fallback to QPDF (note: QPDF does NOT force rasterization)
    try:
        qpdf_command = [
            "qpdf", "--linearize", page_path, output_path
        ]
        subprocess.run(qpdf_command, check=True)
        logger.info(f"Page successfully processed using QPDF: {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"QPDF failed for page {page_path}: {e}")
        return False


def compress_pdf(input_path, output_path, should_pause=None):
    """
    Compress a PDF file or an image by:
      1) Converting any input images to single-page PDF,
      2) Splitting the PDF into pages,
      3) Force-rasterizing each page with Ghostscript,
      4) Merging all pages into the final output PDF.

    Args:
        input_path (str): Path to the input PDF or image file.
        output_path (str): Path to save the compressed PDF file.
    """
    # Create a temporary directory for processing
    temp_dir = tempfile.mkdtemp()
    pdf_input_path = None
    processed_pages = []  # To keep track of rasterized page file paths
    logger.info(f"Created temporary directory: {temp_dir}")

    try:
        if should_pause:
            should_pause()
        # --- STEP 1: Convert non-PDF files (PNG/JPG) to single-page PDF --- #
        # We’ll call the result 'pdf_input_path' for the next steps.
        if not input_path.lower().endswith(".pdf"):
            # Convert image to a single-page PDF using img2pdf
            converted_pdf = os.path.join(temp_dir, "converted_input.pdf")
            logger.info(f"Converting image {input_path} to PDF {converted_pdf}...")
            with open(converted_pdf, "wb") as f:
                # img2pdf.convert expects raw bytes from the image
                f.write(img2pdf.convert(input_path))
            pdf_input_path = converted_pdf
        else:
            # It's already a PDF
            pdf_input_path = input_path

        # --- STEP 2: Read the PDF and split into individual pages --- #
        reader = PdfReader(pdf_input_path)
        num_pages = len(reader.pages)
        logger.info(f"Processing PDF with {num_pages} pages: {pdf_input_path}")

        for i, page in enumerate(reader.pages):
            if should_pause:
                should_pause()
            page_num = i + 1
            page_path = os.path.join(temp_dir, f"page_{page_num}.pdf")
            compressed_page_path = os.path.join(temp_dir, f"compressed_page_{page_num}.pdf")

            # Write this single page to a temporary PDF
            page_writer = PdfWriter()
            page_writer.add_page(page)
            with open(page_path, "wb") as page_file:
                page_writer.write(page_file)
            logger.info(f"Saved page {page_num} to temporary file: {page_path}")

            # --- STEP 3: Force-rasterize each page with Ghostscript --- #
            success = compress_pdf_page(page_path, compressed_page_path)
            if success:
                processed_pages.append(compressed_page_path)
            else:
                logger.warning(f"Adding uncompressed page {page_num} to output.")
                processed_pages.append(page_path)
            if should_pause:
                should_pause()

        # --- STEP 4: Merge all processed pages into the final PDF --- #
        final_writer = PdfWriter()
        for page_file in processed_pages:
            with open(page_file, "rb") as pf:
                page_reader = PdfReader(pf)
                # Each "page_file" is a single-page PDF, so page_reader.pages[0] is that page
                final_writer.add_page(page_reader.pages[0])

        with open(output_path, "wb") as final_file:
            final_writer.write(final_file)
        logger.info(f"Successfully compressed PDF (with fallback) saved to: {output_path}")

    except Exception as e:
        logger.error(f"Unexpected error during PDF compression: {e}", exc_info=True)
        raise
    finally:
        # --- Cleanup temporary files and directory --- #
        for file_path in processed_pages:
            if os.path.exists(file_path):
                os.remove(file_path)
        if pdf_input_path and pdf_input_path != input_path and os.path.exists(pdf_input_path):
            os.remove(pdf_input_path)

        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"Cleaned up temporary directory: {temp_dir}")

@app.route('/find-hit-page', methods=['GET'])
@limit_preview_requests
def find_hit_page():
    try:
        pdf_path, _, _ = resolve_requested_pdf(request.args)
        query = request.args.get('q')

        if not query:
            return jsonify({"error": "Missing required parameters"}), 400

        if not PDF_PAGE_WORK.acquire(blocking=False):
            return pdf_work_busy()
        try:
            with open(pdf_path, "rb") as source:
                reader = PdfReader(source)
                needle = query.lower()
                for i, page in enumerate(reader.pages):
                    try:
                        text = page.extract_text() or ""
                    except Exception as e:
                        logger.warning(f"Text extraction failed on page {i + 1} of {pdf_path}: {e}")
                        continue
                    if needle in text.lower():
                        return jsonify({"page": i + 1})
        finally:
            PDF_PAGE_WORK.release()

        return jsonify({"page": -1})  # Not found
    except PermissionError as e:
        logger.warning(f"Forbidden find-hit-page request: {e}")
        return jsonify({"error": str(e)}), 403
    except FileNotFoundError as e:
        logger.warning(f"find-hit-page file not found: {e}")
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        logger.warning(f"Invalid find-hit-page request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /find-hit-page: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def is_low_data_preview_enabled():
    return request.args.get("low_data", "").lower() in {"1", "true", "yes", "on"}


def get_low_data_preview_cache_path(pdf_path, page_num, cache_folder):
    stat = os.stat(pdf_path)
    cache_key = hashlib.sha256(
        "|".join([
            os.path.realpath(pdf_path),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            str(page_num),
            str(LOW_DATA_PREVIEW_DPI),
            str(LOW_DATA_PREVIEW_JPEG_QUALITY),
        ]).encode("utf-8")
    ).hexdigest()
    return os.path.join(cache_folder, cache_key[:2], f"{cache_key}.jpg")


def render_low_data_preview_page(pdf_path, page_num, cache_path, index_name, cloud_config):
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    images = convert_from_path(
        pdf_path,
        dpi=LOW_DATA_PREVIEW_DPI,
        first_page=page_num,
        last_page=page_num,
        fmt="jpeg",
        thread_count=1,
        timeout=30,
    )
    if not images:
        raise ValueError("Unable to render preview page")

    image = images[0].convert("L")
    fd, temp_path = tempfile.mkstemp(
        prefix="low_data_",
        suffix=".jpg",
        dir=os.path.dirname(cache_path),
    )
    os.close(fd)

    try:
        image.save(
            temp_path,
            "JPEG",
            quality=LOW_DATA_PREVIEW_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        os.replace(temp_path, cache_path)
        if cloud_config:
            try:
                sync_low_data_preview_to_provider(index_name, cloud_config, cache_path)
            except Exception:
                logger.warning("Failed to sync low-data preview cache to cloud provider", exc_info=True)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

    return cache_path


def get_pdf_page_sizes(reader):
    page_sizes = []
    for page in reader.pages:
        try:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
        except Exception:
            width = 612.0
            height = 792.0

        if width <= 0 or height <= 0:
            width = 612.0
            height = 792.0

        page_sizes.append({
            "width": round(width, 2),
            "height": round(height, 2),
        })

    return page_sizes


def build_low_data_preview_html(document_params, pdf_path, reader):
    title = escape(os.path.basename(pdf_path))
    params_json = json.dumps(document_params).replace("</", "<\\/")
    page_sizes = get_pdf_page_sizes(reader)

    page_markup = "\n".join(
        (
            f'<section class="page" data-page="{idx}" '
            f'style="--page-width: {size["width"]}; --page-height: {size["height"]};">'
            f'<span class="page-label">{idx}</span>'
            f'<span class="page-status">Loading</span>'
            f'<img alt="Page {idx}">'
            f'</section>'
        )
        for idx, size in enumerate(page_sizes, start=1)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            margin: 0;
            background: #e9e9e9;
            color: #222;
            font-family: Arial, sans-serif;
        }}
        .toolbar {{
            position: sticky;
            top: 0;
            z-index: 2;
            display: flex;
            justify-content: space-between;
            gap: 10px;
            padding: 8px 12px;
            border-bottom: 1px solid #cfcfcf;
            background: #fff;
            font-size: 13px;
        }}
        .toolbar-title {{
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .pages {{
            padding: 12px 8px 24px;
        }}
        .page {{
            position: relative;
            width: min(100%, 650px);
            margin: 0 auto 14px;
            background: #fff;
            box-shadow: 0 1px 4px rgba(0, 0, 0, 0.22);
            overflow: hidden;
        }}
        .page::before {{
            content: "";
            display: block;
            padding-bottom: calc(var(--page-height) / var(--page-width) * 100%);
        }}
        .page img {{
            position: absolute;
            inset: 0;
            width: 100%;
            height: 100%;
            border: 0;
            object-fit: contain;
            opacity: 0;
            transition: opacity 120ms ease;
        }}
        .page.loaded img {{
            opacity: 1;
        }}
        .page-label {{
            position: absolute;
            top: 6px;
            right: 8px;
            z-index: 1;
            padding: 2px 6px;
            border-radius: 3px;
            background: rgba(255, 255, 255, 0.82);
            color: #555;
            font-size: 11px;
        }}
        .page-status {{
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #777;
            font-size: 13px;
        }}
        .page.loaded .page-status {{
            display: none;
        }}
        .page.failed .page-status {{
            color: #9b1c1c;
        }}
    </style>
</head>
<body>
    <div class="toolbar">
        <span class="toolbar-title">{title}</span>
        <span>{len(page_sizes)} pages</span>
    </div>
    <main class="pages">
        {page_markup}
    </main>
    <script>
        const documentParams = {params_json};

        function pageUrl(pageNumber) {{
            const params = new URLSearchParams({{
                ...documentParams,
                page: String(pageNumber),
                low_data: "1"
            }});
            return `${{window.location.pathname}}?${{params.toString()}}`;
        }}

        // One image in flight per viewer. Other viewers can share the server.
        const queue = [];
        let loading = false;

        async function loadNextPage() {{
            if (loading || !queue.length) return;
            loading = true;
            const page = queue.shift();
            const image = page.querySelector("img");
            const status = page.querySelector(".page-status");
            try {{
                let response = await fetch(pageUrl(page.dataset.page));
                for (let retry = 0; response.status === 503 && retry < 3; retry++) {{
                    await new Promise(resolve => setTimeout(resolve, 1000));
                    response = await fetch(pageUrl(page.dataset.page));
                }}
                if (!response.ok) throw new Error("Preview unavailable");
                const url = URL.createObjectURL(await response.blob());
                image.src = url;
                try {{
                    await image.decode();
                    page.classList.add("loaded");
                }} finally {{
                    URL.revokeObjectURL(url);
                }}
            }} catch (error) {{
                page.classList.add("failed");
                status.textContent = "Preview unavailable";
            }} finally {{
                loading = false;
                loadNextPage();
            }}
        }}

        function loadPage(page) {{
            if (page.dataset.queued) return;
            page.dataset.queued = "1";
            queue.push(page);
            loadNextPage();
        }}

        const pages = Array.from(document.querySelectorAll(".page"));
        pages.slice(0, 3).forEach(loadPage);

        if ("IntersectionObserver" in window) {{
            const observer = new IntersectionObserver((entries) => {{
                entries.forEach((entry) => {{
                    if (entry.isIntersecting) {{
                        loadPage(entry.target);
                        observer.unobserve(entry.target);
                    }}
                }});
            }}, {{ rootMargin: "1800px 0px" }});

            pages.forEach((page) => observer.observe(page));
        }} else {{
            pages.forEach(loadPage);
        }}
    </script>
</body>
</html>"""


@app.route('/preview', methods=['GET'])
@limit_preview_requests
def preview():
    """Preview an authorized full PDF or a single page."""
    try:
        full_path, index_name, doc_id = resolve_requested_pdf(request.args)
        page = request.args.get("page", "1")
        low_data = is_low_data_preview_enabled()

        if page == "all" and not low_data:
            response = send_file(full_path, as_attachment=False)
            response.cache_control.private = True
            return response

        if not PDF_PAGE_WORK.acquire(blocking=False):
            return pdf_work_busy()
        try:
            if page == "all":
                document_params = (
                    {"index_name": index_name, "id": doc_id}
                    if doc_id else {"path": request.args["path"]}
                )
                with open(full_path, "rb") as source:
                    return Response(
                        build_low_data_preview_html(document_params, full_path, PdfReader(source)),
                        mimetype="text/html",
                    )

            page_num = int(page)
            if low_data:
                config = load_index_config(index_name)
                cloud_config = None
                cache_folder = LOW_DATA_PREVIEW_CACHE_FOLDER
                if index_has_cloud_source(index_name, config) and path_within(
                    config["cloud"]["root_path"], full_path,
                ):
                    cloud_config = config
                    cache_folder = cloud_preview_folder(index_name, config)

                preview_path = get_low_data_preview_cache_path(full_path, page_num, cache_folder)
                if not os.path.exists(preview_path):
                    if not LOW_DATA_RENDER_LOCK.acquire(blocking=False):
                        return pdf_work_busy()
                    try:
                        if not os.path.exists(preview_path):
                            with open(full_path, "rb") as source:
                                if not 1 <= page_num <= len(PdfReader(source).pages):
                                    return "Page not found", 404
                            render_low_data_preview_page(full_path, page_num, preview_path, index_name, cloud_config)
                    finally:
                        LOW_DATA_RENDER_LOCK.release()
                response = send_file(preview_path, mimetype="image/jpeg", max_age=86400)
                response.headers["Cache-Control"] = "private, max-age=86400"
                response.headers["X-Low-Data-Preview"] = "1"
                return response

            with open(full_path, "rb") as source:
                reader = PdfReader(source)
                if not 1 <= page_num <= len(reader.pages):
                    return "Page not found", 404
                return send_extracted_pdf(reader, [page_num])
        finally:
            PDF_PAGE_WORK.release()

    except HTTPException:
        raise
    except PermissionError as e:
        logger.warning(f"Forbidden preview request: {e}")
        return jsonify({"error": str(e)}), 403
    except FileNotFoundError as e:
        logger.warning(f"Preview file not found: {e}")
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        logger.warning(f"Invalid preview request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Preview error: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500


def is_pdf_searchable(pdf_path):
    """
    Check if a PDF is already searchable. Bypass owner restrictions if needed.
    """
    try:
        reader = PdfReader(pdf_path)

        if reader.is_encrypted:
            try:
                # Attempt to decrypt the file with an empty string
                reader.decrypt("")
                logger.info(f"PDF '{pdf_path}' was owner-encrypted but successfully decrypted.")
            except Exception as e:
                logger.warning(f"Failed to decrypt PDF '{pdf_path}': {e}")
                return False  # Treat as not searchable if decryption fails

        # Check if text can be extracted from any page
        for page in reader.pages:
            if page.extract_text():
                return True  # PDF is searchable
    except Exception as e:
        logger.error(f"Error checking PDF readability: {e}")
    return False  # PDF is not searchable


def calculate_text_hash(file_path):
    """
    Calculate a hash based on the extracted text content and the file size of a PDF.
    """
    hash_sha256 = hashlib.sha256()
    try:
        reader = PdfReader(file_path)
        for i, page in enumerate(reader.pages):
            try:
                if not hasattr(page, "extract_text"):
                    logger.warning(f"Skipping non-standard page object at index {i} in {file_path}")
                    continue
                content = page.extract_text() or ""
                hash_sha256.update(content.encode("utf-8", errors="replace"))
            except Exception as e:
                logger.warning(f"Failed to extract text from page {i + 1} in {file_path}: {e}")
    except Exception as e:
        logger.error(f"Error extracting text or size for hash calculation: {e}")
    return hash_sha256.hexdigest()


SIMILARITY_STOP_WORDS = {
    "aber", "alle", "als", "also", "am", "an", "and", "auf", "aus", "bei", "bis", "by",
    "das", "dem", "den", "der", "des", "die", "dies", "diese", "dieser", "dieses",
    "ein", "eine", "einem", "einen", "einer", "eines", "for", "from", "fur", "für",
    "has", "hat", "hier", "ich", "im", "in", "ist", "it", "mit", "nach", "not",
    "oder", "of", "on", "or", "per", "sich", "sie", "sind", "the", "to", "und",
    "uns", "vom", "von", "war", "was", "we", "werden", "wie", "with", "you", "zum",
    "zur",
}

SIMILARITY_GENERIC_DOCUMENT_TERMS = {
    "adresse", "amount", "bank", "betrag", "brutto", "customer", "datum", "date",
    "dokument", "email", "fax", "gesamt", "iban", "invoice", "konto", "kunde",
    "kundennummer", "lieferdatum", "netto", "nummer", "page", "rechnung",
    "rechnungsdatum", "rechnungsnummer", "seite", "steuer", "summe", "telefon",
    "total", "ust", "ustid", "ust-id", "vat", "zahlbar", "zahlung",
}


def is_similarity_noise_term(term):
    if not term or len(term) < 3:
        return True
    if term in SIMILARITY_STOP_WORDS:
        return True
    if term.isdigit():
        return True
    if re.fullmatch(r"\d+[.,]\d+", term):
        return True
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", term):
        return True

    digit_count = sum(char.isdigit() for char in term)
    letter_count = sum(char.isalpha() for char in term)
    if digit_count and not letter_count:
        return True
    if digit_count >= 6 and digit_count / len(term) > 0.5:
        return True

    return False


def get_similarity_source_text(content):
    """
    Prefer stable document regions. In invoices, sender/header/footer text is usually
    more useful for similarity than middle-page line items or one-off totals.
    """
    if len(content) <= 4500:
        return content
    return f"{content[:3000]}\n{content[-1500:]}"


def get_document_frequency(searcher, field_name, term):
    try:
        return searcher.doc_frequency(field_name, term)
    except AttributeError:
        return searcher.reader().doc_frequency(field_name, term)


def select_similarity_terms(searcher, schema, content, max_terms=24):
    analyzer = schema["content"].analyzer
    source_text = get_similarity_source_text(content or "")
    terms = [
        token.text.lower()
        for token in analyzer(source_text)
        if not is_similarity_noise_term(token.text.lower())
    ]
    term_counts = Counter(terms)
    if not term_counts:
        return []

    doc_count = max(searcher.doc_count(), 1)
    scored_terms = []
    for term, frequency in term_counts.items():
        document_frequency = get_document_frequency(searcher, "content", term)
        if document_frequency and document_frequency / doc_count > 0.85:
            continue

        idf = math.log((doc_count + 1) / (document_frequency + 1)) + 1
        score = math.log(frequency + 1) * idf
        if term in SIMILARITY_GENERIC_DOCUMENT_TERMS:
            score *= 0.25
        scored_terms.append((score, frequency, term))

    scored_terms.sort(reverse=True)
    return [term for _, _, term in scored_terms[:max_terms]]


def format_search_hit(hit, index_name=None, metadata_documents=None):
    years = hit.get("years", "")
    metadata = {}
    if metadata_documents is not None:
        metadata = metadata_documents.get(hit["id"], {}) or {}
    elif index_name:
        try:
            metadata = get_document_metadata_entry(index_name, hit["id"]) or {}
        except Exception:
            metadata = {}

    return {
        "title": hit["title"],
        "path": hit["path"],
        "score": hit.score,
        "years": years.split(',') if years else [],
        "id": hit["id"],
        "source": metadata.get("source", "standalone"),
        "virtual_path": metadata.get("virtual_path") or build_standalone_virtual_path(hit["title"]),
        "size": metadata.get("size"),
        "modified_at": metadata.get("modified_at"),
        "indexed_at": metadata.get("indexed_at"),
        "cloud_relative_path": metadata.get("cloud_relative_path") or "",
    }


def find_similar_documents(ix, source_content, exclude_id=None, limit=10, index_name=None):
    results = []
    metadata_documents = None
    if index_name:
        metadata_documents = ensure_document_metadata(index_name, ix=ix).get("documents", {})

    with ix.searcher() as searcher:
        similarity_terms = select_similarity_terms(searcher, ix.schema, source_content)
        if not similarity_terms:
            return [], []

        combined_query = Or([
            field_query
            for term in similarity_terms
            for field_query in (Term("content", term), Term("title", term))
        ])

        search_limit = limit + 1 if exclude_id else limit
        hits = searcher.search(combined_query, limit=search_limit)
        for hit in hits:
            if exclude_id and hit.get("id") == exclude_id:
                continue
            results.append(format_search_hit(hit, index_name=index_name, metadata_documents=metadata_documents))
            if len(results) >= limit:
                break

    return similarity_terms, results


def read_pdf_text(file_path):
    reader = PdfReader(file_path)
    all_content = []

    for page_num, page in enumerate(reader.pages):
        try:
            all_content.append(page.extract_text() or "")
        except Exception as e:
            logger.warning(f"Text extraction failed on page {page_num + 1} of {file_path}: {e}")
            all_content.append("")

    return "\n".join(all_content)


def extract_pdf_text_content(file_path, file_title="", allow_ocr=False):
    text_content = read_pdf_text(file_path)
    ocr_output_path = None

    if allow_ocr and not text_content.strip():
        base_path, extension = os.path.splitext(file_path)
        ocr_output_path = f"{base_path}_similarity_ocr{extension}"
        try:
            convert_pdf_to_searchable(file_path, ocr_output_path)
            text_content = read_pdf_text(ocr_output_path)
        finally:
            if ocr_output_path and os.path.exists(ocr_output_path):
                os.remove(ocr_output_path)

    if not text_content.strip():
        return ""

    return text_content + f"\n{file_title}"


@app.route('/maintain-index', methods=['POST'])
def maintain_index():
    """
    Queues an index maintenance operation ("optimize" or "rebuild") for the given index.
    """
    try:
        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        action = data.get("action")

        if not index_name or action not in ["optimize", "rebuild"]:
            return jsonify({"error": "index_name and valid action (optimize/rebuild) are required"}), 400
        ensure_index_access(index_name, action="write")

        index_path = os.path.join(INDEX_FOLDER, index_name)
        pdf_folder = os.path.join(PDF_FOLDER, index_name)
        index_config = load_index_config(index_name)

        if not os.path.exists(index_path):
            return jsonify({"error": f"Index folder does not exist: {index_path}"}), 404

        if index_accepts_uploads(index_name, index_config) and not os.path.exists(pdf_folder):
            return jsonify({"error": f"PDF folder does not exist: {pdf_folder}"}), 404

        # Enqueue the task
        task_id = str(uuid.uuid4())
        update_task_status(task_id, "queued", message=f"Queued maintenance task '{action}",index_name=index_name)
        notify_coordinator("task_queued", index_name, task_id, f"maintenance_{action}", {"action": action})
        task_queue.enqueue(index_name, perform_maintenance_task, index_name, action, task_id)
        return jsonify({
            "message": f"Maintenance task '{action}' queued for index '{index_name}'",
            "task_id": task_id
        }), 202

    except PermissionError as e:
        logger.warning(f"Forbidden maintain-index request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid maintain-index request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /maintain-index: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500

def perform_maintenance_task(index_name, action, task_id):
    try:
        job_type = f"maintenance_{action}"
        wait_before_indexing_task(
            index_name,
            task_id,
            job_type,
            pause_state={
                "job_type": job_type,
                "phase": "before_start",
                "index_name": index_name,
                "action": action,
            },
            result_updates={"action": action},
        )
        notify_coordinator("task_started", index_name, task_id, job_type, {"action": action})
        update_task_status(task_id, "processing", message=f"{action.capitalize()} in progress...",index_name=index_name)
        index_path = os.path.join(INDEX_FOLDER, index_name)
        pdf_folder = os.path.join(PDF_FOLDER, index_name)
        index_config = load_index_config(index_name)

        if action == "optimize":
            ix = index.open_dir(index_path)
            writer = ix.writer()
            writer.commit(optimize=True)
            logger.info(f"✅ Optimized index '{index_name}'.")
            update_task_status(task_id, "done", message=f"{action.capitalize()} completed successfully.", result={
                "message": f"{action.capitalize()} completed successfully.",
                "action": action
            }, index_name=index_name)
            clear_paused_indexing_task(task_id)
            notify_coordinator("task_finished", index_name, task_id, job_type, {"action": action})

        elif action == "rebuild":
            shutil.rmtree(index_path)
            os.makedirs(index_path, exist_ok=True)
            save_index_config(index_name, index_config, validate=False)

            schema = Schema(
                id=ID(stored=True, unique=True),
                title=TEXT(stored=True),
                path=TEXT(stored=True),
                content=TEXT(stored=True),
                years=TEXT(stored=True)
            )
            ix = index.create_in(index_path, schema)

            writer = ix.writer()
            failed = []
            failed_details = []
            rebuilt_files = []
            durations = []
            pdf_files = []
            if index_accepts_uploads(index_name, index_config) and os.path.isdir(pdf_folder):
                pdf_files = sorted(
                    fname for fname in os.listdir(pdf_folder)
                    if fname.lower().endswith(".pdf")
                )

            for position, fname in enumerate(pdf_files):
                fpath = os.path.join(pdf_folder, fname)
                started_at = time.monotonic()
                try:
                    add_to_index(index_name, fpath, fname, ix=ix, writer=writer)
                    rebuilt_files.append(fname)
                except Exception as e:
                    logger.warning(f"❌ Failed to index {fpath}: {e}")
                    failed.append(fname)
                    failed_details.append({"file": fname, "error": str(e)})
                finally:
                    durations.append(time.monotonic() - started_at)

                remaining_names = pdf_files[position + 1:]
                remaining_files = [os.path.join(pdf_folder, name) for name in remaining_names]
                should_pause, status, estimated_seconds = should_pause_indexing_batch(
                    remaining_files,
                    durations,
                    task_id=task_id,
                )
                if status and status.get("restricted"):
                    result_updates = {
                        "action": action,
                        "processed_files": rebuilt_files,
                        "failed_files": failed,
                        "failed_details": failed_details,
                        **build_coordinator_result(status, remaining_files, estimated_seconds),
                    }
                    if should_pause:
                        writer.commit()
                        await_coordinator_open(
                            index_name,
                            task_id,
                            job_type,
                            blocked_status="paused",
                            blocked_message="Index rebuild paused after the current document because heavy jobs are restricted.",
                            resume_message="Coordinator cleared. Resuming index rebuild...",
                            pause_state={
                                "job_type": "maintenance_rebuild",
                                "phase": "remaining_documents",
                                "index_name": index_name,
                                "action": action,
                                "remaining_files": remaining_files,
                                "processed_files": rebuilt_files,
                                "failed_files": failed,
                                "failed_details": failed_details,
                            },
                            result_updates=result_updates,
                        )
                        ix = index.open_dir(index_path)
                        writer = ix.writer()
                    else:
                        update_task_status(
                            task_id,
                            "processing",
                            message="Coordinator restriction detected, but remaining rebuild work is below the wrap-up threshold.",
                            result=task_result_with_updates(task_id, **result_updates),
                            index_name=index_name,
                            coordinator=public_coordinator_status(status),
                        )

            writer.commit()
            ensure_document_metadata(index_name, ix=ix)
            cloud_result = None
            if index_has_cloud_source(index_name, index_config):
                update_task_status(task_id, "processing", message="Rebuild local store complete. Syncing cloud folder...", index_name=index_name)
                cloud_result = sync_cloud_documents(
                    index_name,
                    task_id,
                    reason=f"maintenance_{action}",
                    manage_coordinator=False,
                )
            logger.info(f"🔁 Rebuilt index '{index_name}' with {len(failed)} failed files.")
            update_task_status(task_id, "done", message=f"{action.capitalize()} completed successfully.", result={
                "message": f"{action.capitalize()} completed successfully.",
                "action": action,
                "processed_files": rebuilt_files,
                "failed_files": failed,
                "failed_details": failed_details,
                "cloud_sync": cloud_result
            }, index_name=index_name)
            clear_paused_indexing_task(task_id)
            notify_coordinator("task_finished", index_name, task_id, job_type, {
                "action": action,
                "processed_count": len(rebuilt_files),
                "failed_count": len(failed),
            })
    except Exception as e:
        logger.error(f"💥 Error during index maintenance: {e}", exc_info=True)
        update_task_status(task_id, "failed", message=str(e),index_name=index_name)
        notify_coordinator("task_failed", index_name, task_id, f"maintenance_{action}", {
            "action": action,
            "error": str(e),
        })



@app.route('/verify-index', methods=['POST'])
def verify_index():
    """
Verifies that all documents in the PDF folder of the current index are present
in the search index based on file paths. Missing documents are reindexed.
"""
    try:
        data = request.get_json(silent=True) or {}
        index_name = data.get("index_name")
        if not index_name:
            return jsonify({"error": "Index name is required"}), 400
        ensure_index_access(index_name, action="write")

        pdf_index_folder = os.path.join(PDF_FOLDER, index_name)
        index_config = load_index_config(index_name)
        if index_accepts_uploads(index_name, index_config) and not os.path.exists(pdf_index_folder):
            logger.warning(f"Index '{index_name}' does not exist.")
            return jsonify({"error": "Index does not exist: "+str(index_name)}), 400


        task_id = str(uuid.uuid4())
        update_task_status(task_id, "queued", message=f"Queued verification task",index_name=index_name)
        notify_coordinator("task_queued", index_name, task_id, "verify_index")
        task_queue.enqueue(index_name, run_verify_index_task, index_name, task_id)
        return jsonify({"message": f"Verification task enqueued for index '{index_name}'", "task_id": task_id}), 202

    except PermissionError as e:
        logger.warning(f"Forbidden verify-index request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid verify-index request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /verify-index: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500

def run_verify_index_task(index_name, task_id):
    try:
        wait_before_indexing_task(
            index_name,
            task_id,
            "verify_index",
            pause_state={
                "job_type": "verify_index",
                "phase": "before_start",
                "index_name": index_name,
            },
        )
        notify_coordinator("task_started", index_name, task_id, "verify_index")
        update_task_status(task_id, "processing", message="Verifying index...",index_name=index_name)
        pdf_index_folder = os.path.join(PDF_FOLDER, index_name)
        index_config = load_index_config(index_name)

        ix, _ = create_or_open_index(index_name,check_access=False)

        all_files = []
        if index_accepts_uploads(index_name, index_config) and os.path.isdir(pdf_index_folder):
            all_files = sorted([
                os.path.join(pdf_index_folder, f)
                for f in os.listdir(pdf_index_folder)
                if f.lower().endswith(".pdf")
            ])
        total_files = len(all_files)
        logger.info(f"🔍 Starting index verification for {index_name}. {total_files} files found.")

        indexed_paths = set()
        metadata_documents = ensure_document_metadata(index_name, ix=ix).get("documents", {})
        with ix.searcher() as searcher:
            for hit in searcher.documents():
                metadata = metadata_documents.get(hit.get("id"))
                if metadata and metadata.get("source") == "cloud":
                    continue
                indexed_paths.add(hit.get("path"))

        processed = 0
        reindexed_files = []
        failed_files = []
        failed_details = []
        durations = []
        for position, file_path in enumerate(all_files):
            file_name = os.path.basename(file_path)
            started_at = time.monotonic()

            if file_path not in indexed_paths:
                logger.info(f"⚠️ Missing from index, re-adding: {file_name}")
                try:
                    add_to_index(index_name, file_path, file_name)
                    reindexed_files.append(file_name)
                except Exception as e:
                    logger.error(f"❌ Failed to re-index {file_path}: {e}")
                    failed_files.append(file_name)
                    failed_details.append({"file": file_name, "error": str(e)})

            processed += 1
            durations.append(time.monotonic() - started_at)
            remaining_files = all_files[position + 1:]
            pause_indexing_batch(
                index_name,
                task_id,
                "verify_index",
                remaining_files,
                durations=durations,
                pause_state={
                    "job_type": "verify_index_remaining",
                    "phase": "remaining_documents",
                    "index_name": index_name,
                    "remaining_files": remaining_files,
                    "processed": processed,
                    "total_files": total_files,
                    "reindexed_files": reindexed_files,
                    "failed_files": failed_files,
                    "failed_details": failed_details,
                },
            )
        cloud_result = None
        if index_has_cloud_source(index_name, index_config):
            update_task_status(task_id, "processing", message="Local verification complete. Syncing cloud folder...", result={
                "checked_documents": processed,
                "total_documents": total_files,
                "reindexed_files": reindexed_files,
                "failed_files": failed_files,
                "failed_details": failed_details,
            }, index_name=index_name)
            cloud_result = sync_cloud_documents(
                index_name,
                task_id,
                reason="verify_index",
                manage_coordinator=False,
            )

        logger.info(f"✅ Index verification complete for {index_name}. {processed}/{total_files} checked.")
        clear_paused_indexing_task(task_id)
        update_task_status(task_id, "done", message="Verification completed.", result={
            "message": "Verification completed.",
            "checked_documents": processed,
            "total_documents": total_files,
            "reindexed_files": reindexed_files,
            "failed_files": failed_files,
            "failed_details": failed_details,
            "cloud_sync": cloud_result
        }, index_name=index_name)
        notify_coordinator("task_finished", index_name, task_id, "verify_index", {
            "checked_documents": processed,
            "reindexed_count": len(reindexed_files),
            "failed_count": len(failed_files),
        })
    except Exception as e:
        logger.error(f"💥 Error during verify-index task for {index_name}: {e}", exc_info=True)
        update_task_status(task_id, "failed", message=f"Verification failed: {e}",index_name=index_name)
        notify_coordinator("task_failed", index_name, task_id, "verify_index", {"error": str(e)})





# DEBUG !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
@app.route('/index-info', methods=['GET'])
def index_info():
    """
    Returns the total number of documents in the index.
    """
    try:
        ix, index_name = create_or_open_index(request.args.get("index_name"))
        total_docs = ix.doc_count()  # Get the total number of documents in the index
        return jsonify({"total_documents of index " + str(index_name) + " ": total_docs}), 200
    except PermissionError as e:
        logger.warning(f"Forbidden index-info request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid index-info request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error fetching index info: {e}")
        return jsonify({"error": "Failed to retrieve index information"}), 500





# DEBUG!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!


@app.route('/search', methods=['GET'])
def search():
    """
    Search for documents within a specific index with enhanced query flexibility,
    including fuzzy and partial matches, and support for title boosting.
    """
    query_str = request.args.get('q', '').strip()
    try:
        ix, index_name = create_or_open_index(request.args.get("index_name"))
        results = []
        metadata_documents = ensure_document_metadata(index_name, ix=ix).get("documents", {})

        with ix.searcher() as searcher:
            # Define a MultifieldParser for both title and content with field boosts
            qp = MultifieldParser(
                ["title", "content"],
                schema=ix.schema,
                fieldboosts={"title": 2.0, "content": 1.0}  # Boost title relevance
            )

            # Parse the main query
            main_query = qp.parse(query_str)

            # Create fuzzy queries for both title and content
            fuzzy_query_title = FuzzyTerm("title", query_str, maxdist=2)  # Fuzzy matching for title
            fuzzy_query_content = FuzzyTerm("content", query_str, maxdist=2)  # Fuzzy matching for content

            # Add partial match queries (e.g., searching for substrings)
            partial_query_title = Term("title", query_str.lower())
            partial_query_content = Term("content", query_str.lower())

            # Combine the main query with fuzzy and partial match queries
            combined_query = Or([
                main_query,
                fuzzy_query_title,
                fuzzy_query_content,
                partial_query_title,
                partial_query_content
            ])

            # Execute the search
            hits = searcher.search(combined_query, limit=50)
            for hit in hits:
                results.append(format_search_hit(hit, index_name=index_name, metadata_documents=metadata_documents))

        return jsonify(results)

    except PermissionError as e:
        logger.warning(f"Forbidden search request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid search request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /search: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500


@app.route('/similar-documents', methods=['GET'])
def similar_documents():
    """
    Finds documents that share high-signal terms with an already indexed document.
    This uses only fields that are already stored in the current index, so existing
    indexes do not need to be rebuilt.
    """
    doc_id = request.args.get('id', '').strip()
    limit = request.args.get('limit', default=10, type=int)
    limit = min(max(limit, 1), 25)

    if not doc_id:
        return jsonify({"error": "Missing required parameter: id"}), 400

    try:
        ix, index_name = create_or_open_index(request.args.get("index_name"))

        with ix.searcher() as searcher:
            target_doc = searcher.document(id=doc_id)
            if not target_doc:
                return jsonify({"error": "Document not found"}), 404

            target_title = target_doc.get("title", "")
            target_content = target_doc.get("content", "")

        similarity_terms, results = find_similar_documents(
            ix,
            target_content,
            exclude_id=doc_id,
            limit=limit,
            index_name=index_name,
        )

        return jsonify({
            "id": doc_id,
            "title": target_title,
            "terms": similarity_terms,
            "results": results
        })

    except PermissionError as e:
        logger.warning(f"Forbidden similar-documents request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid similar-documents request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /similar-documents: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500


@app.route('/similar-documents-from-file', methods=['POST'])
def similar_documents_from_file():
    """
    Finds indexed documents similar to an uploaded PDF without adding that PDF
    to the index. The uploaded file is saved temporarily, scanned, read, then
    deleted before returning the response.
    """
    temp_file_path = None

    try:
        index_name = request.form.get("index_name")
        if not index_name:
            return jsonify({"error": "Index name is required"}), 400
        ensure_index_access(index_name)

        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400
        if not file.filename.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF files are supported"}), 400

        filename = secure_filename(file.filename)
        upload_folder = os.path.join(TMP_FOLDER, validate_index_name(index_name), "similarity")
        os.makedirs(upload_folder, exist_ok=True)
        temp_file_path = os.path.join(upload_folder, f"{uuid.uuid4().hex}_{filename}")
        file.save(temp_file_path)

        if not getattr(g, "skip_antivirus", False):
            is_ready, readiness_message = wait_for_clamav(return_message=True)
            if not is_ready:
                return jsonify({"error": f"Antivirus not ready: {readiness_message}"}), 503

            is_clean, scan_message = scan_with_clamav(temp_file_path)
            if not is_clean:
                return jsonify({"error": scan_message}), 400

        source_content = extract_pdf_text_content(temp_file_path, filename, allow_ocr=True)
        if not source_content.strip():
            return jsonify({"error": "No text could be extracted from the uploaded PDF"}), 400

        ix, _ = create_or_open_index(index_name)
        similarity_terms, results = find_similar_documents(ix, source_content, limit=10, index_name=index_name)

        return jsonify({
            "title": filename,
            "terms": similarity_terms,
            "results": results
        })

    except PermissionError as e:
        logger.warning(f"Forbidden similar-documents-from-file request: {e}")
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        logger.warning(f"Invalid similar-documents-from-file request: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error in /similar-documents-from-file: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {e}"}), 500
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "backend healthy"}), 200


def get_clamav_client():
    return clamd.ClamdNetworkSocket(host=CLAMAV_HOST, port=CLAMAV_PORT)


#wait until AV daemon is accepting scan requests
def wait_for_clamav(max_wait=None, return_message=False):
    timeout = CLAMAV_WAIT_SECONDS if max_wait is None else max_wait
    timeout = max(0, timeout)
    retry_delay = max(0.1, CLAMAV_RETRY_SECONDS)
    deadline = time.monotonic() + timeout
    last_error = None
    attempts = 0

    while True:
        attempts += 1
        try:
            cd = get_clamav_client()
            if cd.ping():
                message = f"ClamAV ready at {CLAMAV_HOST}:{CLAMAV_PORT} after {attempts} attempt(s)"
                return (True, message) if return_message else True
            last_error = "ping returned an empty response"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(retry_delay, remaining))

    message = f"No response from {CLAMAV_HOST}:{CLAMAV_PORT} within {timeout} seconds"
    if last_error:
        message = f"{message}; last error: {last_error}"
    return (False, message) if return_message else False


#antivirus scanner for uploaded files
def scan_with_clamav(filepath):
    try:
        # Connect to ClamAV daemon in the ClamAV container
        cd = get_clamav_client()

        # Optional: test connection
        if not cd.ping():
            logger.error("ClamAV daemon is not responding.")
            return False, f"ClamAV not responding at {CLAMAV_HOST}:{CLAMAV_PORT}"

        # Perform scan
        result = cd.scan(filepath)
        logger.info(f"🦠 ClamAV scan result for {filepath}: {result}")

        if result and filepath in result:
            status, virus_name = result[filepath]
            if status == 'FOUND':
                alert_message = f"🚨 VIRUS FOUND IN DOCUMENT {virus_name}"
                logger.error(alert_message)
                send_alert_email("🚨 SECURITY ALERT: Virus found", alert_message)
                return False, f"Infected with {virus_name}"
        return True, "Clean"
    except clamd.ConnectionError as e:
        logger.exception("ClamAV is not reachable or not ready.")
        return False, f"Antivirus not reachable at {CLAMAV_HOST}:{CLAMAV_PORT}: {e}"
    except Exception as e:
        logger.exception("ClamAV scan failed")
        return False, f"ClamAV scan error: {str(e)}"

def send_alert_email(subject, body):
    try:
        to_email = os.environ.get("ALERT_EMAIL_TO")
        from_email = os.environ.get("ALERT_EMAIL_FROM")
        password = os.environ.get("ALERT_EMAIL_PASSWORD")
        smtp_server = os.environ.get("ALERT_EMAIL_SMTP", "smtp.gmail.com")
        smtp_port = int(os.environ.get("ALERT_EMAIL_PORT", 587))

        if not all([to_email, from_email, password]):
            logger.warning("🔕 Email alert not sent: missing config.")
            return

        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(from_email, password)
            server.send_message(msg)

        logger.info(f"📧 Alert email sent to {to_email}")
    except Exception as e:
        logger.error(f"❌ Failed to send alert email: {e}")


def resume_upload_zip_task(index_name, task_id, state):
    filename = state.get("filename") or "paused.zip"
    temp_file_path = state.get("temp_file_path")
    extract_dir = state.get("extract_dir")
    remaining_files = [
        path for path in (state.get("remaining_files") or [])
        if path and os.path.exists(path)
    ]
    processed_files = list(state.get("processed_files") or [])
    duplicate_files = list(state.get("duplicate_files") or [])
    failed_files = list(state.get("failed_files") or [])
    failed_details = list(state.get("failed_details") or [])
    duplicate_count = len(duplicate_files)
    processed_count = len(processed_files)
    durations = []
    writer = None

    try:
        wait_before_indexing_task(
            index_name,
            task_id,
            "upload_zip",
            pause_state=state,
            result_updates={
                "source_file": filename,
                "remaining_files": [os.path.basename(path) for path in remaining_files],
                "remaining_documents": len(remaining_files),
            },
        )
        notify_coordinator("task_resumed", index_name, task_id, "upload_zip", {"source_file": filename})
        update_task_status(
            task_id,
            "processing",
            message="Resuming paused ZIP indexing...",
            result=task_result_with_updates(
                task_id,
                source_file=filename,
                processed_files=processed_files,
                duplicates_skipped=duplicate_count,
                duplicate_files=duplicate_files,
                failed_files=failed_files,
                failed_details=failed_details,
                remaining_documents=len(remaining_files),
                remaining_files=[os.path.basename(path) for path in remaining_files],
            ),
            index_name=index_name,
        )

        ix, index_name = create_or_open_index(index_name, check_access=False)
        writer = ix.writer()

        position = 0
        while position < len(remaining_files):
            file_path = remaining_files[position]
            file_name = os.path.basename(file_path)
            started_at = time.monotonic()
            try:
                result = process_single_file(file_path, index_name, ix=ix, writer=writer, coordinator_task_id=task_id)
                if isinstance(result, str) and "duplicate" in result:
                    duplicate_count += 1
                    duplicate_files.append(file_name)
                else:
                    processed_count += 1
                    processed_files.append(file_name)
            except CoordinatorPauseRequested as pause:
                durations.append(time.monotonic() - started_at)
                next_remaining = remaining_files[position:]
                estimated_seconds = estimate_remaining_indexing_seconds(
                    len(next_remaining),
                    durations,
                )
                result_updates = {
                    "source_file": filename,
                    "processed_files": processed_files,
                    "duplicates_skipped": duplicate_count,
                    "duplicate_files": duplicate_files,
                    "failed_files": failed_files,
                    "failed_details": failed_details,
                    **build_coordinator_result(pause.status, next_remaining, estimated_seconds),
                }
                if writer:
                    writer.commit()
                    writer = None
                release_current_job_and_wait_for_resume(
                    index_name,
                    task_id,
                    "upload_zip",
                    pause.status,
                    next_remaining,
                    estimated_seconds,
                    {
                        "job_type": "upload_zip",
                        "phase": "remaining_documents",
                        "index_name": index_name,
                        "temp_file_path": temp_file_path,
                        "filename": filename,
                        "extract_dir": extract_dir,
                        "remaining_files": next_remaining,
                        "processed_files": processed_files,
                        "duplicate_files": duplicate_files,
                        "failed_files": failed_files,
                        "failed_details": failed_details,
                        "skip_av": state.get("skip_av", False),
                    },
                    result_updates,
                    "ZIP indexing paused after the current page because Jellyfin playback became active.",
                    "Coordinator cleared. Resuming ZIP indexing...",
                )
                ix, index_name = create_or_open_index(index_name, check_access=False)
                writer = ix.writer()
                continue
            except Exception as e:
                logger.error(f"Error processing resumed ZIP file {file_path}: {e}")
                failed_files.append(file_name)
                failed_details.append({"file": file_name, "error": str(e)})
                durations.append(time.monotonic() - started_at)
            else:
                durations.append(time.monotonic() - started_at)

            next_remaining = remaining_files[position + 1:]
            should_pause, status, estimated_seconds = should_pause_indexing_batch(
                next_remaining,
                durations,
                task_id=task_id,
            )
            if status and status.get("restricted"):
                result_updates = {
                    "source_file": filename,
                    "processed_files": processed_files,
                    "duplicates_skipped": duplicate_count,
                    "duplicate_files": duplicate_files,
                    "failed_files": failed_files,
                    "failed_details": failed_details,
                    **build_coordinator_result(status, next_remaining, estimated_seconds),
                }
                if should_pause:
                    writer.commit()
                    writer = None
                    release_current_job_and_wait_for_resume(
                        index_name,
                        task_id,
                        "upload_zip",
                        status,
                        next_remaining,
                        estimated_seconds,
                        {
                            "job_type": "upload_zip",
                            "phase": "remaining_documents",
                            "index_name": index_name,
                            "temp_file_path": temp_file_path,
                            "filename": filename,
                            "extract_dir": extract_dir,
                            "remaining_files": next_remaining,
                            "processed_files": processed_files,
                            "duplicate_files": duplicate_files,
                            "failed_files": failed_files,
                            "failed_details": failed_details,
                            "skip_av": state.get("skip_av", False),
                        },
                        result_updates,
                        "ZIP indexing paused after the current document because heavy jobs are restricted.",
                        "Coordinator cleared. Resuming ZIP indexing...",
                    )
                    ix, index_name = create_or_open_index(index_name, check_access=False)
                    writer = ix.writer()
                else:
                    update_task_status(
                        task_id,
                        "processing",
                        message="Coordinator restriction detected, but remaining ZIP indexing is below the wrap-up threshold.",
                        result=task_result_with_updates(task_id, **result_updates),
                        index_name=index_name,
                        coordinator=public_coordinator_status(status),
                    )
            position += 1

        if writer:
            writer.commit()
            writer = None
        clear_paused_indexing_task(task_id)
        if extract_dir:
            shutil.rmtree(extract_dir, ignore_errors=True)
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

        update_task_status(task_id, "done", result={
            "message": f"ZIP processed. {processed_count} files indexed, {duplicate_count} duplicates skipped, {len(failed_files)} failed.",
            "source_file": filename,
            "processed_files": processed_files,
            "duplicates_skipped": duplicate_count,
            "duplicate_files": duplicate_files,
            "failed_files": failed_files,
            "failed_details": failed_details,
        }, index_name=index_name)
        notify_coordinator("task_finished", index_name, task_id, "upload_zip", {
            "source_file": filename,
            "processed_count": processed_count,
            "duplicates_skipped": duplicate_count,
            "failed_count": len(failed_files),
        })
    except Exception as e:
        if writer:
            try:
                writer.cancel()
            except Exception:
                logger.warning("Failed to cancel resumed ZIP writer", exc_info=True)
        logger.error(f"Error resuming ZIP task: {e}", exc_info=True)
        update_task_status(task_id, "failed", message=f"Resume failed: {e}", result={
            "source_file": filename,
            "failed_files": failed_files or [filename],
            "failed_details": failed_details + [{"file": filename, "error": str(e)}],
        }, index_name=index_name)
        notify_coordinator("task_failed", index_name, task_id, "upload_zip", {
            "source_file": filename,
            "error": str(e),
        })


def resume_paused_indexing_task(task_id, state):
    index_name = state.get("index_name")
    job_type = state.get("job_type")
    if not index_name or not job_type:
        return False

    update_task_status(
        task_id,
        "queued",
        message="Queued paused indexing task for resume.",
        result={
            "source_file": state.get("filename") or state.get("append_filename"),
            "remaining_documents": len(state.get("remaining_files") or []),
            "remaining_files": [
                os.path.basename(path)
                for path in (state.get("remaining_files") or [])
            ],
        },
        index_name=index_name,
    )

    if job_type == "upload":
        task_queue.enqueue(
            index_name,
            process_uploaded_file,
            index_name,
            state.get("temp_file_path"),
            state.get("filename"),
            task_id,
            bool(state.get("skip_av", False)),
        )
        return True

    if job_type == "append":
        task_queue.enqueue(
            index_name,
            process_append_document,
            index_name,
            state.get("doc_id"),
            state.get("append_file_path"),
            state.get("append_filename"),
            task_id,
            bool(state.get("skip_av", False)),
        )
        return True

    if job_type in {"process_tmp", "process_tmp_remaining"}:
        task_queue.enqueue(
            index_name,
            process_tmp_folder,
            index_name,
            task_id,
            bool(state.get("skip_av", False)),
        )
        return True

    if job_type == "upload_zip":
        task_queue.enqueue(index_name, resume_upload_zip_task, index_name, task_id, state)
        return True

    if job_type in {"verify_index", "verify_index_remaining"}:
        task_queue.enqueue(index_name, run_verify_index_task, index_name, task_id)
        return True

    if job_type in {"cloud_sync", CLOUD_INDEX_JOB_TYPE}:
        task_queue.enqueue(index_name, run_cloud_sync_task, index_name, task_id, state.get("reason", "resumed"))
        return True

    if job_type.startswith("maintenance_"):
        task_queue.enqueue(
            index_name,
            perform_maintenance_task,
            index_name,
            state.get("action", "rebuild"),
            task_id,
        )
        return True

    logger.warning("Unsupported paused coordinator job type: %s", job_type)
    return False


def resume_paused_indexing_jobs_on_startup():
    paused_tasks = list_paused_indexing_tasks()
    for task_id, state in paused_tasks.items():
        try:
            if resume_paused_indexing_task(task_id, state):
                logger.info("Queued paused coordinator task for resume: %s", task_id)
        except Exception:
            logger.warning("Failed to queue paused coordinator task %s", task_id, exc_info=True)


resume_paused_indexing_jobs_on_startup()


if get_backup_config().get("enabled") and not parse_bool(os.environ.get("DISABLE_BACKUP_SCHEDULER")):
    ensure_backup_scheduler_running()


if not parse_bool(os.environ.get("DISABLE_CLOUD_SYNC_SCHEDULER")):
    ensure_cloud_sync_scheduler_running()


warn_about_insecure_test_settings()


if __name__ == "__main__":
    # Compose publishes the container port on the host's loopback only.
    app.run(host="0.0.0.0" if RUNNING_IN_DOCKER else "127.0.0.1", port=5050)
