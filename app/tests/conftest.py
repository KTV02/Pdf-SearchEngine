import importlib
import sys
from pathlib import Path

import pytest


APP_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = APP_ROOT / "backend"
EMAIL_PROCESSOR_DIR = APP_ROOT / "email_processor"


def _prepend_path(path):
    path_str = str(path)
    if path_str in sys.path:
        sys.path.remove(path_str)
    sys.path.insert(0, path_str)


@pytest.fixture
def backend_module(tmp_path, monkeypatch):
    monkeypatch.setenv("TMP_FOLDER", str(tmp_path / "tmp"))
    monkeypatch.setenv("LOG_FOLDER", str(tmp_path / "logs"))
    monkeypatch.setenv("PDF_FOLDER", str(tmp_path / "ocr" / "converted"))
    monkeypatch.setenv("DELETED_FOLDER", str(tmp_path / "ocr" / "deleted"))
    monkeypatch.setenv("INDEX_FOLDER", str(tmp_path / "index_folder"))
    monkeypatch.setenv("EMAIL_CONFIGS", str(tmp_path / "email_configs"))
    monkeypatch.setenv("MANAGED_CLOUD_FOLDER", str(tmp_path / "cloud" / "managed"))
    monkeypatch.setenv("CLOUD_CONNECTIONS_PATH", str(tmp_path / "index_folder" / "cloud_connections.json"))
    monkeypatch.setenv("CLOUD_PROVIDER_CONFIG_PATH", str(tmp_path / "index_folder" / "cloud_provider_config.json"))
    monkeypatch.setenv("ROLE_CONFIG_PATH", str(tmp_path / "roles.json"))
    monkeypatch.setenv("ROLE_INDEX_CONFIG_PATH", str(tmp_path / "role_indices.json"))
    monkeypatch.delenv("LOCAL_SINGLE_USER_MODE", raising=False)
    monkeypatch.setenv("ACCESS_CONFIG_PATH", str(tmp_path / "access_config.json"))
    monkeypatch.setenv("STORAGE_CONFIG_PATH", str(tmp_path / "index_folder" / "storage_config.json"))
    monkeypatch.setenv("BACKUP_TARGET_FOLDER", str(tmp_path / "backups"))
    monkeypatch.setenv("DISABLE_BACKUP_SCHEDULER", "true")
    monkeypatch.setenv("DISABLE_CLOUD_SYNC_SCHEDULER", "true")
    monkeypatch.delenv("BACKUP_ENABLED", raising=False)
    monkeypatch.setenv("TEST_BYPASS_TOKEN", "test-token")
    monkeypatch.setenv("MAIL_TOKEN", "mail-token")
    monkeypatch.setenv("ADMIN_TAILSCALE_USERS", "env-admin@example.com")
    monkeypatch.delenv("GOOGLE_DRIVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_REDIRECT_URI", raising=False)
    monkeypatch.delenv("DROPBOX_APP_KEY", raising=False)
    monkeypatch.delenv("DROPBOX_APP_SECRET", raising=False)
    monkeypatch.delenv("DROPBOX_REDIRECT_URI", raising=False)
    monkeypatch.delenv("NODERED_COORDINATOR_URL", raising=False)
    monkeypatch.delenv("NODE_RED_COORDINATOR_URL", raising=False)
    monkeypatch.delenv("JOB_COORDINATOR_URL", raising=False)
    monkeypatch.delenv("NODERED_COORDINATOR_STATUS_ENDPOINT", raising=False)
    monkeypatch.delenv("NODERED_COORDINATOR_ACTION_ENDPOINT", raising=False)
    monkeypatch.delenv("NODERED_COORDINATOR_ACTION_ENDPOINTS", raising=False)
    monkeypatch.delenv("JOB_COORDINATOR_STATE_PATH", raising=False)

    (tmp_path / "roles.json").write_text('{"127.0.0.1": "admin"}')
    (tmp_path / "access_config.json").write_text(
        '{"indices": ["test_index", "example_index", "second_index"], '
        '"users": {'
        '"admin@example.com": {"admin": true, "read": [], "write": []}, '
        '"reader@example.com": {"admin": false, "read": ["test_index"], "write": []}, '
        '"writer@example.com": {"admin": false, "read": ["test_index"], "write": ["test_index"]}'
        '}}'
    )

    _prepend_path(BACKEND_DIR)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True)
    monkeypatch.setattr(module, "get_docker_gateway_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(module, "send_alert_email", lambda *args, **kwargs: None)
    yield module
    sys.modules.pop("app", None)


@pytest.fixture
def email_processor_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_BASE_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("STATUS_DIR", str(tmp_path / "processor_state"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MAIL_TOKEN", "mail-token")

    _prepend_path(EMAIL_PROCESSOR_DIR)
    sys.modules.pop("processor_module", None)
    module = importlib.import_module("processor_module")
    module.active_tasks.clear()
    module.task_configs.clear()
    module.cancel_flags.clear()
    yield module
    sys.modules.pop("processor_module", None)


@pytest.fixture
def email_processor_api_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_BASE_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("STATUS_DIR", str(tmp_path / "processor_state"))
    monkeypatch.setenv("EMAIL_CONFIG_DIR", str(tmp_path / "email_configs"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MAIL_TOKEN", "mail-token")

    _prepend_path(EMAIL_PROCESSOR_DIR)
    sys.modules.pop("processor_api", None)
    sys.modules.pop("processor_module", None)
    module = importlib.import_module("processor_api")
    module.app.config.update(TESTING=True)
    yield module
    sys.modules.pop("processor_api", None)
    sys.modules.pop("processor_module", None)
