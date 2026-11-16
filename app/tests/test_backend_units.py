import io
import logging
import shutil
import zipfile
from pathlib import Path

import pytest


BYPASS_HEADERS = {"X-Test-Bypass": "unit-test-bypass-token-0123456789abcdef"}


def write_blank_pdf(path):
    from PyPDF2 import PdfWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as f:
        writer.write(f)


def write_text_pdf(path, text):
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, text)
    pdf.save()


def write_pdf_without_pages(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<< /Type /Catalog >>\n"
        b"endobj\n"
        b"xref\n"
        b"0 2\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"trailer\n"
        b"<< /Size 2 /Root 1 0 R >>\n"
        b"startxref\n"
        b"45\n"
        b"%%EOF\n"
    )


class FakeResponse:
    def __init__(self, status_code=200, json_payload=None, content=b""):
        self.status_code = status_code
        self._json_payload = json_payload if json_payload is not None else {}
        self.content = content
        self.text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)

    def json(self):
        return self._json_payload

    def iter_content(self, chunk_size=1024):
        yield self.content

    def close(self):
        return None


def test_extract_years_from_title_returns_full_years(backend_module):
    years = backend_module.extract_years_from_title("rechnung_2024_scan.23.pdf")

    assert 2024 in years
    assert 2023 in years
    assert 20 not in years


@pytest.mark.parametrize("index_name", ["../bad", "bad/name", "bad.name", "", None])
def test_validate_index_name_rejects_unsafe_values(backend_module, index_name):
    with pytest.raises(ValueError):
        backend_module.validate_index_name(index_name)


def test_resolve_index_pdf_path_accepts_authorized_index_file(backend_module):
    pdf_path = Path(backend_module.PDF_FOLDER) / "test_index" / "doc.pdf"
    write_blank_pdf(pdf_path)

    with backend_module.app.test_request_context(headers=BYPASS_HEADERS):
        resolved_path, index_name = backend_module.resolve_index_pdf_path(str(pdf_path))

    assert resolved_path == str(pdf_path)
    assert index_name == "test_index"


def test_resolve_index_pdf_path_rejects_path_outside_pdf_root(backend_module, tmp_path):
    outside_pdf = tmp_path / "outside.pdf"
    write_blank_pdf(outside_pdf)

    with backend_module.app.test_request_context(headers=BYPASS_HEADERS):
        with pytest.raises(PermissionError):
            backend_module.resolve_index_pdf_path(str(outside_pdf))


def test_tailscale_reader_only_sees_configured_indices(backend_module):
    client = backend_module.app.test_client()

    response = client.get(
        "/indices",
        headers={"Tailscale-User-Login": "reader@example.com"},
    )

    assert response.status_code == 200
    assert response.json["available_indices"] == ["test_index"]
    assert response.json["current_user"]["login"] == "reader@example.com"
    assert response.json["current_user"]["is_admin"] is False


def test_tailscale_reader_cannot_queue_write_task(backend_module):
    client = backend_module.app.test_client()

    response = client.post(
        "/process-tmp",
        json={"index_name": "test_index"},
        headers={"Tailscale-User-Login": "reader@example.com"},
    )

    assert response.status_code == 403
    assert "Access denied" in response.json["error"]


def test_repeated_task_status_requests_log_client_once(backend_module, caplog):
    backend_module.task_status["task-1"] = {
        "status": "processing",
        "index_name": "test_index",
    }
    caplog.set_level(logging.INFO, logger=backend_module.logger.name)
    client = backend_module.app.test_client()

    for _ in range(2):
        response = client.get(
            "/task-status/task-1",
            headers={"Tailscale-User-Login": "writer@example.com"},
        )
        assert response.status_code == 200

    messages = [record.getMessage() for record in caplog.records]
    connected_messages = [
        message
        for message in messages
        if message.startswith("Client connected:")
    ]
    assert connected_messages == [
        "Client connected: Tailscale user writer@example.com (127.0.0.1) via GET /task-status/task-1"
    ]
    assert not any("Testunit detected" in message for message in messages)


def test_tailscale_writer_can_queue_write_task(backend_module, monkeypatch):
    queued = []

    class FakeQueue:
        def enqueue(self, index_name, task_func, *args, **kwargs):
            queued.append((index_name, task_func, args, kwargs))

    monkeypatch.setattr(backend_module, "task_queue", FakeQueue())
    client = backend_module.app.test_client()

    response = client.post(
        "/process-tmp",
        json={"index_name": "test_index"},
        headers={"Tailscale-User-Login": "writer@example.com"},
    )

    assert response.status_code == 202
    assert response.json["task_id"]
    assert queued
    assert queued[0][0] == "test_index"


def test_non_admin_cannot_fetch_access_config(backend_module):
    client = backend_module.app.test_client()

    response = client.get(
        "/admin/access-config",
        headers={"Tailscale-User-Login": "reader@example.com"},
    )

    assert response.status_code == 403


def test_env_admin_tailscale_user_can_fetch_access_config(backend_module):
    client = backend_module.app.test_client()

    response = client.get(
        "/admin/access-config",
        headers={"Tailscale-User-Login": "env-admin@example.com"},
    )

    assert response.status_code == 200
    assert "config" in response.json


def test_admin_can_save_access_config(backend_module):
    client = backend_module.app.test_client()
    payload = {
        "config": {
            "indices": ["test_index", "example_index"],
            "users": {
                "person@example.com": {
                    "admin": False,
                    "read": ["example_index"],
                    "write": []
                }
            }
        }
    }

    response = client.put(
        "/admin/access-config",
        json=payload,
        headers={"Tailscale-User-Login": "admin@example.com"},
    )

    assert response.status_code == 200
    assert response.json["config"]["users"]["person@example.com"]["read"] == ["example_index"]


def test_admin_can_create_new_index(backend_module):
    client = backend_module.app.test_client()

    response = client.post(
        "/admin/indices",
        json={"index_name": "new_index"},
        headers={"Tailscale-User-Login": "admin@example.com"},
    )

    assert response.status_code == 201
    assert "new_index" in response.json["config"]["indices"]
    assert (Path(backend_module.INDEX_FOLDER) / "new_index").exists()
    assert (Path(backend_module.PDF_FOLDER) / "new_index").exists()


def test_index_config_defaults_to_standalone(backend_module):
    client = backend_module.app.test_client()

    response = client.get(
        "/index-config",
        query_string={"index_name": "test_index"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.json["mode"] == "standalone"
    assert response.json["capabilities"]["uploads_enabled"] is True
    assert response.json["capabilities"]["cloud_sync_enabled"] is False


def test_admin_can_convert_index_to_hybrid_with_cloud_folder(backend_module, tmp_path, monkeypatch):
    cloud_root = tmp_path / "cloud-drive"
    cloud_root.mkdir()
    queued = []

    class FakeQueue:
        def enqueue(self, index_name, task_func, *args, **kwargs):
            queued.append((index_name, task_func, args, kwargs))

    monkeypatch.setattr(backend_module, "task_queue", FakeQueue())
    client = backend_module.app.test_client()

    response = client.put(
        "/index-config",
        json={
            "index_name": "test_index",
            "index_config": {
                "mode": "hybrid",
                "cloud": {
                    "root_path": str(cloud_root),
                    "display_root_name": "drive",
                    "sync_interval_seconds": 60,
                    "sync_enabled": True,
                    "delete_standalone_duplicates": True,
                },
            },
        },
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.json["index_config"]["mode"] == "hybrid"
    assert response.json["index_config"]["capabilities"]["uploads_enabled"] is True
    assert response.json["index_config"]["capabilities"]["cloud_sync_enabled"] is True
    assert queued
    assert queued[0][1] is backend_module.run_cloud_sync_task


def test_admin_can_view_cloud_providers(backend_module):
    client = backend_module.app.test_client()

    response = client.get("/cloud-providers", headers=BYPASS_HEADERS)

    assert response.status_code == 200
    providers = {provider["id"]: provider for provider in response.json["providers"]}
    assert providers["mounted_folder"]["configured"] is True
    assert providers["onedrive"]["configured"] is False
    assert providers["onedrive"]["requires_login"] is True
    assert response.json["provider_settings"]["onedrive"]["configured"] is False
    assert response.json["connections"] == []


def test_admin_can_save_cloud_provider_registration(backend_module):
    client = backend_module.app.test_client()

    response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "onedrive",
            "settings": {
                "client_id": "client-id-from-ui",
                "tenant": "common",
                "scopes": "offline_access User.Read Files.ReadWrite",
                "request_timeout_seconds": 30,
            },
        },
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.json["provider_settings"]["onedrive"]["client_id"] == "client-id-from-ui"
    assert response.json["provider_settings"]["onedrive"]["configured"] is True
    assert Path(backend_module.CLOUD_PROVIDER_CONFIG_PATH).exists()

    providers_response = client.get("/cloud-providers", headers=BYPASS_HEADERS)
    providers = {provider["id"]: provider for provider in providers_response.json["providers"]}
    assert providers["onedrive"]["configured"] is True


def test_admin_can_start_onedrive_device_login_from_ui_saved_registration(backend_module, monkeypatch):
    client = backend_module.app.test_client()
    save_response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "onedrive",
            "settings": {
                "client_id": "client-id-from-ui",
                "tenant": "common",
                "scopes": "offline_access User.Read Files.ReadWrite",
                "request_timeout_seconds": 30,
            },
        },
        headers=BYPASS_HEADERS,
    )
    assert save_response.status_code == 200

    def fake_post(url, data=None, timeout=None):
        assert url.endswith("/oauth2/v2.0/devicecode")
        assert data["client_id"] == "client-id-from-ui"
        assert timeout == 30
        return FakeResponse(json_payload={
            "device_code": "device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "verification_uri_complete": "https://microsoft.com/devicelogin?code=ABCD-EFGH",
            "message": "Use code ABCD-EFGH",
            "expires_in": 900,
            "interval": 5,
        })

    started_threads = []
    monkeypatch.setattr(backend_module.requests, "post", fake_post)
    monkeypatch.setattr(backend_module, "start_cloud_auth_polling_thread", lambda connection_id: started_threads.append(connection_id))

    response = client.post(
        "/cloud-connections/start",
        json={"provider": "onedrive", "display_name": "Family OneDrive"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 202
    connection = response.json["connection"]
    assert connection["provider"] == "onedrive"
    assert connection["status"] == "pending"
    assert connection["user_code"] == "ABCD-EFGH"
    assert started_threads == [connection["id"]]
    assert backend_module.cloud_auth_sessions[connection["id"]]["device_code"] == "device-code"


def test_non_admin_writer_can_create_own_index(backend_module):
    client = backend_module.app.test_client()

    response = client.post(
        "/admin/indices",
        json={"index_name": "writer_index"},
        headers={"Tailscale-User-Login": "writer@example.com"},
    )

    assert response.status_code == 201
    assert response.json["config"] is None
    assert "writer_index" in response.json["available_indices"]

    indices_response = client.get(
        "/indices",
        headers={"Tailscale-User-Login": "writer@example.com"},
    )
    assert "writer_index" in indices_response.json["available_indices"]
    assert "writer_index" in indices_response.json["current_user"]["writable_indices"]


def test_non_admin_reader_cannot_create_existing_index_without_write_access(backend_module):
    client = backend_module.app.test_client()

    response = client.post(
        "/admin/indices",
        json={"index_name": "example_index"},
        headers={"Tailscale-User-Login": "reader@example.com"},
    )

    assert response.status_code == 403


def test_non_admin_writer_can_view_providers_but_not_save_registration(backend_module):
    client = backend_module.app.test_client()

    providers_response = client.get(
        "/cloud-providers",
        headers={"Tailscale-User-Login": "writer@example.com"},
    )
    assert providers_response.status_code == 200
    assert providers_response.json["can_manage_provider_settings"] is False
    provider_ids = {provider["id"] for provider in providers_response.json["providers"]}
    assert {"onedrive", "google_drive", "dropbox"}.issubset(provider_ids)

    save_response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "onedrive",
            "settings": {
                "client_id": "client-id-from-user",
                "tenant": "common",
                "scopes": "offline_access User.Read Files.ReadWrite",
                "request_timeout_seconds": 30,
            },
        },
        headers={"Tailscale-User-Login": "writer@example.com"},
    )
    assert save_response.status_code == 403


def test_non_admin_reader_cannot_start_cloud_login_for_read_only_index(backend_module):
    client = backend_module.app.test_client()

    response = client.post(
        "/cloud-connections/start",
        json={"provider": "onedrive", "index_name": "test_index"},
        headers={"Tailscale-User-Login": "reader@example.com"},
    )

    assert response.status_code == 403


def test_non_admin_writer_can_start_onedrive_login_for_writable_index(backend_module, monkeypatch):
    client = backend_module.app.test_client()
    save_response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "onedrive",
            "settings": {
                "client_id": "client-id-from-ui",
                "tenant": "common",
                "scopes": "offline_access User.Read Files.ReadWrite",
                "request_timeout_seconds": 30,
            },
        },
        headers=BYPASS_HEADERS,
    )
    assert save_response.status_code == 200

    monkeypatch.setattr(
        backend_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(json_payload={
            "device_code": "device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        }),
    )
    monkeypatch.setattr(backend_module, "start_cloud_auth_polling_thread", lambda connection_id: None)

    response = client.post(
        "/cloud-connections/start",
        json={"provider": "onedrive", "display_name": "My OneDrive", "index_name": "test_index"},
        headers={"Tailscale-User-Login": "writer@example.com"},
    )

    assert response.status_code == 202
    connection = response.json["connection"]
    assert connection["provider"] == "onedrive"
    assert backend_module.cloud_auth_sessions[connection["id"]]["owner"]["login"] == "writer@example.com"


def test_non_admin_writer_cannot_configure_mounted_host_folder(backend_module, tmp_path):
    mounted_root = tmp_path / "external_cloud"
    mounted_root.mkdir()
    client = backend_module.app.test_client()

    response = client.put(
        "/index-config",
        json={
            "index_name": "test_index",
            "index_config": {
                "mode": "cloud",
                "cloud": {
                    "provider": "mounted_folder",
                    "root_path": str(mounted_root),
                },
            },
        },
        headers={"Tailscale-User-Login": "writer@example.com"},
    )

    assert response.status_code == 403
    assert "mounted or synced host folder" in response.json["error"]


def test_admin_can_save_google_drive_and_dropbox_provider_registration(backend_module):
    client = backend_module.app.test_client()

    google_response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "google_drive",
            "settings": {
                "client_id": "google-client-id",
                "client_secret": "google-secret",
                "redirect_uri": "https://pdf.example.com/cloud-connections/callback/google_drive",
                "scopes": "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/userinfo.email",
                "request_timeout_seconds": 30,
            },
        },
        headers=BYPASS_HEADERS,
    )
    assert google_response.status_code == 200
    assert google_response.json["provider_settings"]["google_drive"]["configured"] is True
    assert google_response.json["provider_settings"]["google_drive"]["client_secret_saved"] is True

    dropbox_response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "dropbox",
            "settings": {
                "app_key": "dropbox-app-key",
                "app_secret": "dropbox-secret",
                "redirect_uri": "https://pdf.example.com/cloud-connections/callback/dropbox",
                "scopes": "account_info.read files.metadata.read files.content.read files.content.write",
                "request_timeout_seconds": 30,
            },
        },
        headers=BYPASS_HEADERS,
    )
    assert dropbox_response.status_code == 200
    assert dropbox_response.json["provider_settings"]["dropbox"]["configured"] is True
    assert dropbox_response.json["provider_settings"]["dropbox"]["app_secret_saved"] is True


def test_google_drive_browser_login_callback_saves_connection(backend_module, monkeypatch):
    client = backend_module.app.test_client()
    response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "google_drive",
            "settings": {
                "client_id": "google-client-id",
                "redirect_uri": "https://pdf.example.com/cloud-connections/callback/google_drive",
                "scopes": "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/userinfo.email",
                "request_timeout_seconds": 30,
            },
        },
        headers=BYPASS_HEADERS,
    )
    assert response.status_code == 200

    start_response = client.post(
        "/cloud-connections/start",
        json={"provider": "google_drive", "display_name": "Google", "index_name": "test_index"},
        headers=BYPASS_HEADERS,
    )
    assert start_response.status_code == 202
    session = start_response.json["connection"]
    assert "accounts.google.com" in session["authorization_url"]

    def fake_post(url, data=None, timeout=None):
        assert url == backend_module.GOOGLE_OAUTH_TOKEN_URL
        assert data["client_id"] == "google-client-id"
        assert data["grant_type"] == "authorization_code"
        assert data["code_verifier"]
        return FakeResponse(json_payload={
            "access_token": "google-access-token",
            "refresh_token": "google-refresh-token",
            "expires_in": 3600,
            "scope": data.get("scope", ""),
        })

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url == backend_module.GOOGLE_USERINFO_URL
        return FakeResponse(json_payload={
            "id": "google-user",
            "name": "Google User",
            "email": "google@example.com",
        })

    monkeypatch.setattr(backend_module.requests, "post", fake_post)
    monkeypatch.setattr(backend_module.requests, "get", fake_get)

    callback_response = client.get(
        "/cloud-connections/callback/google_drive",
        query_string={"state": backend_module.cloud_auth_sessions[session["id"]]["state"], "code": "auth-code"},
    )

    assert callback_response.status_code == 200
    saved_connection = backend_module.get_cloud_connection(session["id"])
    assert saved_connection["provider"] == "google_drive"
    assert saved_connection["token"]["refresh_token"] == "google-refresh-token"
    assert saved_connection["account"]["mail"] == "google@example.com"


def test_dropbox_browser_login_callback_saves_connection(backend_module, monkeypatch):
    client = backend_module.app.test_client()
    response = client.put(
        "/cloud-provider-settings",
        json={
            "provider": "dropbox",
            "settings": {
                "app_key": "dropbox-app-key",
                "redirect_uri": "https://pdf.example.com/cloud-connections/callback/dropbox",
                "scopes": "account_info.read files.metadata.read files.content.read files.content.write",
                "request_timeout_seconds": 30,
            },
        },
        headers=BYPASS_HEADERS,
    )
    assert response.status_code == 200

    start_response = client.post(
        "/cloud-connections/start",
        json={"provider": "dropbox", "display_name": "Dropbox", "index_name": "test_index"},
        headers=BYPASS_HEADERS,
    )
    assert start_response.status_code == 202
    session = start_response.json["connection"]
    assert "dropbox.com/oauth2/authorize" in session["authorization_url"]

    def fake_post(url, data=None, headers=None, timeout=None):
        if url == backend_module.DROPBOX_TOKEN_URL:
            assert data["client_id"] == "dropbox-app-key"
            assert data["grant_type"] == "authorization_code"
            assert data["code_verifier"]
            return FakeResponse(json_payload={
                "access_token": "dropbox-access-token",
                "refresh_token": "dropbox-refresh-token",
                "expires_in": 14400,
                "scope": "account_info.read files.metadata.read files.content.read files.content.write",
            })
        assert url.endswith("/users/get_current_account")
        return FakeResponse(json_payload={
            "account_id": "dbid:test",
            "email": "dropbox@example.com",
            "name": {"display_name": "Dropbox User"},
        })

    monkeypatch.setattr(backend_module.requests, "post", fake_post)

    callback_response = client.get(
        "/cloud-connections/callback/dropbox",
        query_string={"state": backend_module.cloud_auth_sessions[session["id"]]["state"], "code": "auth-code"},
    )

    assert callback_response.status_code == 200
    saved_connection = backend_module.get_cloud_connection(session["id"])
    assert saved_connection["provider"] == "dropbox"
    assert saved_connection["token"]["refresh_token"] == "dropbox-refresh-token"
    assert saved_connection["account"]["mail"] == "dropbox@example.com"


def test_admin_can_save_onedrive_cloud_config_with_managed_root(backend_module, monkeypatch):
    backend_module.MICROSOFT_GRAPH_CLIENT_ID = "client-id"
    backend_module.upsert_cloud_connection({
        "id": "onedrive-test",
        "provider": "onedrive",
        "display_name": "OneDrive Test",
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
        "account": {"userPrincipalName": "user@example.com"},
    })
    ensured_paths = []
    monkeypatch.setattr(
        backend_module,
        "ensure_onedrive_remote_folder_path",
        lambda connection, remote_path: ensured_paths.append(remote_path) or {"id": "folder-id"},
    )
    queued = []

    class FakeQueue:
        def enqueue(self, index_name, task_func, *args, **kwargs):
            queued.append((index_name, task_func, args, kwargs))

    monkeypatch.setattr(backend_module, "task_queue", FakeQueue())

    client = backend_module.app.test_client()
    response = client.put(
        "/index-config",
        json={
            "index_name": "test_index",
            "index_config": {
                "mode": "cloud",
                "cloud": {
                    "provider": "onedrive",
                    "connection_id": "onedrive-test",
                    "remote_path": "Documents/PDFs",
                    "sync_enabled": False,
                },
            },
        },
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    cloud = response.json["index_config"]["cloud"]
    managed_root = Path(cloud["managed_root_path"])
    assert managed_root.exists()
    assert managed_root.is_relative_to(Path(backend_module.MANAGED_CLOUD_FOLDER))
    assert cloud["root_path"] == cloud["managed_root_path"]
    assert Path(cloud["preview_folder"]).is_relative_to(managed_root / ".pdfsearchengine" / "previews")
    assert ensured_paths == ["Documents/PDFs", "Documents/PDFs/.pdfsearchengine/previews/test_index"]
    assert queued


def test_manual_cloud_sync_rejects_saved_disconnected_account(backend_module, monkeypatch):
    backend_module.MICROSOFT_GRAPH_CLIENT_ID = "client-id"
    connection = {
        "id": "onedrive-failed",
        "provider": "onedrive",
        "display_name": "Family OneDrive",
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
        "account": {"userPrincipalName": "user@example.com"},
    }
    backend_module.upsert_cloud_connection(connection)
    monkeypatch.setattr(
        backend_module,
        "ensure_onedrive_remote_folder_path",
        lambda connection, remote_path: {"id": "folder-id"},
    )
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": "onedrive",
                "connection_id": "onedrive-failed",
                "remote_path": "Documents/PDFs",
                "sync_enabled": True,
            },
        },
    )
    connection["status"] = "failed"
    backend_module.upsert_cloud_connection(connection)

    client = backend_module.app.test_client()
    response = client.post(
        "/sync-cloud",
        json={"index_name": "test_index"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 400
    assert "Family OneDrive" in response.json["error"]
    assert "save the index mode before syncing" in response.json["error"]


def test_manual_cloud_sync_allows_retryable_failed_account(backend_module, monkeypatch):
    backend_module.MICROSOFT_GRAPH_CLIENT_ID = "client-id"
    connection = {
        "id": "onedrive-retry",
        "provider": "onedrive",
        "display_name": "Retry OneDrive",
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
        "account": {"userPrincipalName": "user@example.com"},
    }
    backend_module.upsert_cloud_connection(connection)
    monkeypatch.setattr(
        backend_module,
        "ensure_onedrive_remote_folder_path",
        lambda connection, remote_path: {"id": "folder-id"},
    )
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": "onedrive",
                "connection_id": "onedrive-retry",
                "remote_path": "Documents/PDFs",
                "sync_enabled": True,
            },
        },
    )
    connection["status"] = "failed"
    connection["last_error"] = "Microsoft Graph request failed: 500 INTERNAL SERVER ERROR"
    backend_module.upsert_cloud_connection(connection)
    queued = []

    class FakeQueue:
        def enqueue(self, index_name, task_func, *args, **kwargs):
            queued.append((index_name, task_func, args, kwargs))

    monkeypatch.setattr(backend_module, "task_queue", FakeQueue())

    client = backend_module.app.test_client()
    response = client.post(
        "/sync-cloud",
        json={"index_name": "test_index"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 202
    assert response.json["task_id"]
    assert queued


def test_transient_refresh_error_does_not_mark_connection_failed(backend_module, monkeypatch):
    backend_module.MICROSOFT_GRAPH_CLIENT_ID = "client-id"
    connection = backend_module.upsert_cloud_connection({
        "id": "onedrive-refresh",
        "provider": "onedrive",
        "display_name": "Refresh OneDrive",
        "status": "connected",
        "token": {
            "access_token": "expired-access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(1),
        },
        "account": {"userPrincipalName": "user@example.com"},
    })
    monkeypatch.setattr(
        backend_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(
            status_code=500,
            json_payload={"error": {"message": "INTERNAL SERVER ERROR"}},
            content=b"INTERNAL SERVER ERROR",
        ),
    )

    with pytest.raises(RuntimeError):
        backend_module.refresh_onedrive_token_if_needed(connection, force=True)

    saved_connection = backend_module.get_cloud_connection("onedrive-refresh")
    assert saved_connection["status"] == "connected"
    assert "INTERNAL SERVER ERROR" in saved_connection["last_error"]


def test_non_admin_cannot_fetch_storage_config(backend_module):
    client = backend_module.app.test_client()

    response = client.get(
        "/admin/storage-config",
        headers={"Tailscale-User-Login": "reader@example.com"},
    )

    assert response.status_code == 403


def test_admin_can_fetch_storage_config(backend_module):
    client = backend_module.app.test_client()

    response = client.get(
        "/admin/storage-config",
        headers={"Tailscale-User-Login": "admin@example.com"},
    )

    assert response.status_code == 200
    assert response.json["storage"]["pdf_folder"] == backend_module.PDF_FOLDER
    assert response.json["storage"]["index_folder"] == backend_module.INDEX_FOLDER
    assert response.json["backup"]["target_folder"]


def test_admin_can_save_backup_config(backend_module, tmp_path):
    client = backend_module.app.test_client()
    backup_target = tmp_path / "onedrive-backups"

    response = client.put(
        "/admin/storage-config",
        json={
            "backup": {
                "enabled": True,
                "target_folder": str(backup_target),
                "interval_hours": 6,
                "retention_count": 3,
                "include_deleted": False,
                "include_configs": True,
            }
        },
        headers={"Tailscale-User-Login": "admin@example.com"},
    )

    assert response.status_code == 200
    assert response.json["backup"]["enabled"] is True
    assert response.json["backup"]["target_folder"] == str(backup_target)
    assert response.json["backup"]["interval_hours"] == 6
    assert response.json["backup"]["retention_count"] == 3
    assert Path(backend_module.STORAGE_CONFIG_PATH).exists()


def test_create_backup_snapshot_copies_pdf_and_index_data(backend_module, tmp_path):
    pdf_path = Path(backend_module.PDF_FOLDER) / "test_index" / "doc.pdf"
    write_blank_pdf(pdf_path)

    index_marker = Path(backend_module.INDEX_FOLDER) / "test_index" / "segments.txt"
    index_marker.parent.mkdir(parents=True, exist_ok=True)
    index_marker.write_text("index segment")

    backup_config = backend_module.normalize_backup_config({
        "target_folder": str(tmp_path / "backups"),
        "retention_count": 2,
        "include_deleted": True,
        "include_configs": True,
    })

    manifest = backend_module.create_backup_snapshot(backup_config, trigger="test")
    snapshot_path = Path(manifest["snapshot_path"])

    assert (snapshot_path / "pdfs" / "test_index" / "doc.pdf").exists()
    assert (snapshot_path / "index" / "test_index" / "segments.txt").read_text() == "index segment"
    assert (snapshot_path / "configs" / "access_config.json").exists()
    assert (snapshot_path / "manifest.json").exists()


def test_extract_zip_safely_rejects_zip_slip(backend_module, tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zip_file:
        zip_file.writestr("../../escape.pdf", b"not a real pdf")
    payload.seek(0)

    with zipfile.ZipFile(payload) as zip_file:
        with pytest.raises(ValueError):
            backend_module.extract_zip_safely(zip_file, tmp_path / "extract")


def test_download_rejects_outside_path(backend_module, tmp_path):
    outside_pdf = tmp_path / "outside.pdf"
    write_blank_pdf(outside_pdf)
    client = backend_module.app.test_client()

    response = client.post(
        "/download",
        json={"path": str(outside_pdf)},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 403


def test_download_returns_authorized_pdf(backend_module):
    pdf_path = Path(backend_module.PDF_FOLDER) / "test_index" / "doc.pdf"
    write_blank_pdf(pdf_path)
    client = backend_module.app.test_client()

    response = client.post(
        "/download",
        json={"path": str(pdf_path)},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"


def test_low_data_preview_page_returns_rendered_jpeg(backend_module, monkeypatch):
    pdf_path = Path(backend_module.PDF_FOLDER) / "test_index" / "doc.pdf"
    write_blank_pdf(pdf_path)
    preview_image = Path(backend_module.TMP_FOLDER) / "preview.jpg"
    preview_image.parent.mkdir(parents=True, exist_ok=True)
    preview_image.write_bytes(b"\xff\xd8preview\xff\xd9")

    def fake_render_low_data_preview_page(path, page_num):
        assert path == str(pdf_path)
        assert page_num == 1
        return str(preview_image)

    monkeypatch.setattr(
        backend_module,
        "render_low_data_preview_page",
        fake_render_low_data_preview_page,
    )
    client = backend_module.app.test_client()

    response = client.get(
        "/preview",
        query_string={"path": str(pdf_path), "page": "1", "low_data": "1"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.headers["X-Low-Data-Preview"] == "1"
    assert response.data.startswith(b"\xff\xd8")


def test_low_data_preview_all_returns_scrollable_viewer(backend_module):
    pdf_path = Path(backend_module.PDF_FOLDER) / "test_index" / "doc.pdf"
    write_blank_pdf(pdf_path)
    client = backend_module.app.test_client()

    response = client.get(
        "/preview",
        query_string={"path": str(pdf_path), "page": "all", "low_data": "1"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert 'data-page="1"' in html
    assert 'low_data: "1"' in html
    assert "IntersectionObserver" in html


def test_replace_file_via_target_folder_stages_replace_in_target_dir(backend_module, tmp_path, monkeypatch):
    source_path = tmp_path / "tmp_mount" / "merged.pdf"
    target_path = tmp_path / "ocr_mount" / "doc.pdf"
    source_path.parent.mkdir()
    target_path.parent.mkdir()
    source_path.write_bytes(b"new content")
    target_path.write_bytes(b"old content")

    original_replace = backend_module.os.replace
    replace_calls = []

    def recording_replace(source, target):
        replace_calls.append((Path(source), Path(target)))
        assert Path(source).parent == target_path.parent
        original_replace(source, target)

    monkeypatch.setattr(backend_module.os, "replace", recording_replace)

    backend_module.replace_file_via_target_folder(str(source_path), str(target_path))

    assert target_path.read_bytes() == b"new content"
    assert replace_calls
    assert replace_calls[0][1] == target_path
    assert source_path.exists()


def test_process_tmp_endpoint_queues_index_task(backend_module, monkeypatch):
    queued = []

    class FakeQueue:
        def enqueue(self, index_name, task_func, *args, **kwargs):
            queued.append((index_name, task_func, args, kwargs))

    monkeypatch.setattr(backend_module, "task_queue", FakeQueue())
    client = backend_module.app.test_client()

    response = client.post(
        "/process-tmp",
        json={"index_name": "test_index"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 202
    assert response.json["task_id"]
    assert queued
    assert queued[0][0] == "test_index"
    assert queued[0][1] is backend_module.process_tmp_folder


def test_append_document_endpoint_queues_index_task(backend_module, monkeypatch):
    queued = []

    class FakeQueue:
        def enqueue(self, index_name, task_func, *args, **kwargs):
            queued.append((index_name, task_func, args, kwargs))

    monkeypatch.setattr(backend_module, "task_queue", FakeQueue())
    client = backend_module.app.test_client()

    response = client.post(
        "/append-document",
        data={
            "index_name": "test_index",
            "id": "doc-1",
            "file": (io.BytesIO(b"%PDF-1.4\n"), "append.pdf"),
        },
        headers=BYPASS_HEADERS,
        content_type="multipart/form-data",
    )

    assert response.status_code == 202
    assert response.json["task_id"]
    assert queued
    assert queued[0][0] == "test_index"
    assert queued[0][1] is backend_module.process_append_document

    args = queued[0][2]
    assert args[0] == "test_index"
    assert args[1] == "doc-1"
    assert Path(args[2]).exists()
    assert args[3] == "append.pdf"
    assert args[4] == response.json["task_id"]
    assert args[5] is True


def test_append_document_endpoint_rejects_non_pdf(backend_module):
    client = backend_module.app.test_client()

    response = client.post(
        "/append-document",
        data={
            "index_name": "test_index",
            "id": "doc-1",
            "file": (io.BytesIO(b"not a pdf"), "append.txt"),
        },
        headers=BYPASS_HEADERS,
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.json["error"] == "Only PDF files can be appended"


def test_update_task_status_adds_timestamps(backend_module):
    backend_module.task_status.clear()

    backend_module.update_task_status("task-1", "queued", message="queued", index_name="test_index")
    created_at = backend_module.task_status["task-1"]["created_at"]

    backend_module.update_task_status("task-1", "done", message="done", index_name="test_index")
    task = backend_module.task_status["task-1"]

    assert task["created_at"] == created_at
    assert task["updated_at"] >= created_at
    assert task["status"] == "done"


def test_job_coordinator_is_disabled_by_default(backend_module):
    status = backend_module.job_coordinator.get_status()

    assert status["enabled"] is False
    assert status["restricted"] is False


def test_node_red_coordinator_accepts_common_status_shapes(backend_module):
    coordinator = backend_module.NodeRedJobCoordinator(base_url="http://node-red:1880")

    assert coordinator.normalize_status({"heavy_jobs_restricted": True})["restricted"] is True
    assert coordinator.normalize_status({"heavy_jobs_restricted": "false"})["restricted"] is False
    assert coordinator.normalize_status({"state": "locked"})["restricted"] is True
    assert coordinator.normalize_status({"can_start_heavy_jobs": False})["restricted"] is True
    assert coordinator.normalize_status({"can_start_heavy_jobs": "false"})["restricted"] is True
    assert coordinator.normalize_status({"state": "idle"})["restricted"] is False


def test_node_red_coordinator_defaults_to_health_in_job_api_mode(backend_module, monkeypatch):
    monkeypatch.setenv("NODERED_COORDINATOR_URL", "http://node-red:1881")
    monkeypatch.delenv("NODERED_COORDINATOR_STATUS_ENDPOINT", raising=False)
    monkeypatch.delenv("JOB_COORDINATOR_STATUS_ENDPOINT", raising=False)

    coordinator = backend_module.NodeRedJobCoordinator.from_env()
    assert coordinator.status_endpoint == "/coordinator/health"

    monkeypatch.setenv("NODERED_COORDINATOR_JOB_API_ENABLED", "false")
    legacy = backend_module.NodeRedJobCoordinator.from_env()
    assert legacy.status_endpoint == "/job-coordinator/status"


def test_job_coordinator_status_endpoint_reports_disabled_state(backend_module):
    client = backend_module.app.test_client()

    response = client.get(
        "/job-coordinator/status",
        query_string={"index_name": "test_index"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.json["enabled"] is False
    assert response.json["status"]["restricted"] is False


def test_wait_for_clamav_returns_readiness_details(backend_module, monkeypatch):
    def refusing_client():
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(backend_module, "get_clamav_client", refusing_client)

    is_ready, message = backend_module.wait_for_clamav(max_wait=0, return_message=True)

    assert is_ready is False
    assert "No response from" in message
    assert "connection refused" in message


def test_similar_documents_returns_same_company_invoice(backend_module):
    ix, _ = backend_module.create_or_open_index("test_index", check_access=False)
    writer = ix.writer()
    writer.update_document(
        id="target",
        title="acme_invoice_2024_001.pdf",
        path="/tmp/acme_invoice_2024_001.pdf",
        content=(
            "ACME GmbH Hauptstrasse 1 Berlin\n"
            "Rechnung 2024-001 Betrag 123,45 EUR\n"
            "Vielen Dank fuer Ihren Einkauf bei ACME GmbH"
        ),
        years="2024",
    )
    writer.update_document(
        id="same-company",
        title="acme_invoice_2024_002.pdf",
        path="/tmp/acme_invoice_2024_002.pdf",
        content=(
            "ACME GmbH Hauptstrasse 1 Berlin\n"
            "Rechnung 2024-002 Betrag 88,10 EUR\n"
            "Vielen Dank fuer Ihren Einkauf bei ACME GmbH"
        ),
        years="2024",
    )
    writer.update_document(
        id="different-company",
        title="other_invoice_2024.pdf",
        path="/tmp/other_invoice_2024.pdf",
        content=(
            "Other AG Industrieweg 9 Hamburg\n"
            "Rechnung 2024-777 Betrag 91,00 EUR\n"
            "Zahlung innerhalb von 14 Tagen"
        ),
        years="2024",
    )
    writer.commit()

    client = backend_module.app.test_client()
    response = client.get(
        "/similar-documents",
        query_string={"index_name": "test_index", "id": "target", "limit": 1},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert response.json["results"][0]["id"] == "same-company"
    assert response.json["results"][0]["id"] != "target"


def test_similar_documents_from_uploaded_pdf_returns_matches(backend_module, monkeypatch):
    ix, _ = backend_module.create_or_open_index("test_index", check_access=False)
    writer = ix.writer()
    writer.update_document(
        id="same-company",
        title="acme_invoice_2024_002.pdf",
        path="/tmp/acme_invoice_2024_002.pdf",
        content=(
            "ACME GmbH Hauptstrasse 1 Berlin\n"
            "Rechnung 2024-002 Betrag 88,10 EUR\n"
            "Vielen Dank fuer Ihren Einkauf bei ACME GmbH"
        ),
        years="2024",
    )
    writer.update_document(
        id="different-company",
        title="other_invoice_2024.pdf",
        path="/tmp/other_invoice_2024.pdf",
        content=(
            "Other AG Industrieweg 9 Hamburg\n"
            "Rechnung 2024-777 Betrag 91,00 EUR\n"
            "Zahlung innerhalb von 14 Tagen"
        ),
        years="2024",
    )
    writer.commit()

    monkeypatch.setattr(
        backend_module,
        "extract_pdf_text_content",
        lambda file_path, file_title="", allow_ocr=False: (
            "ACME GmbH Hauptstrasse 1 Berlin\n"
            "Rechnung 2024-001 Betrag 123,45 EUR\n"
            "Vielen Dank fuer Ihren Einkauf bei ACME GmbH"
        ),
    )

    client = backend_module.app.test_client()
    response = client.post(
        "/similar-documents-from-file",
        data={
            "index_name": "test_index",
            "file": (io.BytesIO(b"%PDF-1.4\n"), "external_acme.pdf"),
        },
        headers=BYPASS_HEADERS,
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.json["title"] == "external_acme.pdf"
    assert response.json["results"][0]["id"] == "same-company"


def test_cloud_sync_indexes_updates_and_removes_cloud_documents(backend_module, tmp_path):
    cloud_root = tmp_path / "drive"
    cloud_pdf = cloud_root / "one" / "test.pdf"
    write_text_pdf(cloud_pdf, "cloud sync unique alpha")
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "root_path": str(cloud_root),
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )
    backend_module.task_status.clear()

    backend_module.run_cloud_sync_task("test_index", "cloud-task-1", "test")

    assert backend_module.task_status["cloud-task-1"]["status"] == "done"
    client = backend_module.app.test_client()
    folder_response = client.get(
        "/folder-tree",
        query_string={"index_name": "test_index", "path": "drive/one"},
        headers=BYPASS_HEADERS,
    )

    assert folder_response.status_code == 200
    assert folder_response.json["files"][0]["title"] == "test.pdf"
    assert folder_response.json["files"][0]["source"] == "cloud"
    assert folder_response.json["files"][0]["virtual_path"] == "drive/one/test.pdf"
    assert folder_response.json["files"][0]["size"] > 0
    assert folder_response.json["files"][0]["modified_at"]

    search_response = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "alpha"},
        headers=BYPASS_HEADERS,
    )
    assert search_response.status_code == 200
    assert search_response.json[0]["source"] == "cloud"
    assert search_response.json[0]["virtual_path"] == "drive/one/test.pdf"

    write_text_pdf(cloud_pdf, "cloud sync unique beta")
    backend_module.run_cloud_sync_task("test_index", "cloud-task-2", "test")

    old_search = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "alpha"},
        headers=BYPASS_HEADERS,
    )
    new_search = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "beta"},
        headers=BYPASS_HEADERS,
    )
    assert old_search.status_code == 200
    assert old_search.json == []
    assert new_search.status_code == 200
    assert new_search.json[0]["virtual_path"] == "drive/one/test.pdf"

    cloud_pdf.unlink()
    backend_module.run_cloud_sync_task("test_index", "cloud-task-3", "test")

    deleted_search = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "beta"},
        headers=BYPASS_HEADERS,
    )
    assert deleted_search.status_code == 200
    assert deleted_search.json == []


def test_cloud_sync_skips_local_scan_when_provider_reports_no_changes(backend_module, tmp_path, monkeypatch):
    cloud_root = tmp_path / "drive"
    cloud_pdf = cloud_root / "one" / "test.pdf"
    write_text_pdf(cloud_pdf, "cloud sync unique alpha")
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "root_path": str(cloud_root),
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )

    backend_module.run_cloud_sync_task("test_index", "cloud-task-initial", "test")
    assert backend_module.task_status["cloud-task-initial"]["status"] == "done"

    hash_calls = []

    def fail_if_hash_recalculated(file_path, chunk_size=1024 * 1024):
        hash_calls.append(file_path)
        raise AssertionError("unchanged cloud file should not be re-hashed")

    def fail_if_local_cloud_walk(index_name, config=None):
        raise AssertionError("unchanged provider sync should not walk local cloud files")

    def fail_if_coordinator_waits(*args, **kwargs):
        raise AssertionError("unchanged provider sync should not request a coordinator lock")

    def fail_if_coordinator_notified(*args, **kwargs):
        raise AssertionError("unchanged provider sync should not notify the coordinator")

    monkeypatch.setattr(
        backend_module,
        "sync_cloud_provider_to_local",
        lambda index_name, config: {
            "checked_remote_files": 1,
            "downloaded_files": [],
            "removed_local_files": [],
            "unchanged_files": ["one/test.pdf"],
        },
    )
    monkeypatch.setattr(
        backend_module,
        "list_cloud_pdf_files",
        fail_if_local_cloud_walk,
    )
    monkeypatch.setattr(backend_module, "calculate_file_hash", fail_if_hash_recalculated)
    monkeypatch.setattr(backend_module, "wait_before_indexing_task", fail_if_coordinator_waits)
    monkeypatch.setattr(backend_module, "notify_coordinator", fail_if_coordinator_notified)

    backend_module.run_cloud_sync_task("test_index", "cloud-task-unchanged", "test")

    task = backend_module.task_status["cloud-task-unchanged"]
    assert task["status"] == "done"
    assert task["result"]["added_files"] == []
    assert task["result"]["updated_files"] == []
    assert task["result"]["unchanged_files"] == ["drive/one/test.pdf"]
    assert hash_calls == []


def test_cloud_sync_requests_coordinator_only_when_indexing_changes(backend_module, tmp_path, monkeypatch):
    cloud_root = tmp_path / "drive"
    cloud_pdf = cloud_root / "one" / "test.pdf"
    write_text_pdf(cloud_pdf, "cloud sync unique alpha")
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "root_path": str(cloud_root),
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )

    wait_calls = []
    coordinator_events = []

    def fake_wait_before_indexing_task(index_name, task_id, job_type, **kwargs):
        wait_calls.append((index_name, task_id, job_type, kwargs))

    def fake_notify_coordinator(action, index_name, task_id, job_type, extra=None):
        coordinator_events.append((action, job_type, extra or {}))
        return True

    monkeypatch.setattr(backend_module, "wait_before_indexing_task", fake_wait_before_indexing_task)
    monkeypatch.setattr(backend_module, "notify_coordinator", fake_notify_coordinator)

    backend_module.run_cloud_sync_task("test_index", "cloud-task-index", "test")

    assert [call[2] for call in wait_calls] == [backend_module.CLOUD_INDEX_JOB_TYPE]
    assert [
        (action, job_type)
        for action, job_type, _extra in coordinator_events
    ] == [
        ("task_started", backend_module.CLOUD_INDEX_JOB_TYPE),
        ("task_finished", backend_module.CLOUD_INDEX_JOB_TYPE),
    ]
    task = backend_module.task_status["cloud-task-index"]
    assert task["status"] == "done"
    assert task["result"]["added_files"] == ["drive/one/test.pdf"]
    assert task["result"]["coordinator_job_type"] == backend_module.CLOUD_INDEX_JOB_TYPE


def test_cloud_sync_indexes_pdf_without_pages_by_metadata(backend_module, tmp_path):
    cloud_root = tmp_path / "drive"
    cloud_pdf = cloud_root / "Studium" / "example-document.pdf"
    write_pdf_without_pages(cloud_pdf)
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "root_path": str(cloud_root),
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )
    backend_module.task_status.clear()

    backend_module.run_cloud_sync_task("test_index", "cloud-task-pageless", "test")

    task = backend_module.task_status["cloud-task-pageless"]
    assert task["status"] == "done"
    assert task["result"]["failed_files"] == []
    assert task["result"]["added_files"] == [
        "drive/Studium/example-document.pdf"
    ]

    client = backend_module.app.test_client()
    folder_response = client.get(
        "/folder-tree",
        query_string={"index_name": "test_index", "path": "drive/Studium"},
        headers=BYPASS_HEADERS,
    )
    assert folder_response.status_code == 200
    assert folder_response.json["files"][0]["title"] == "example-document.pdf"

    search_response = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "example"},
        headers=BYPASS_HEADERS,
    )
    assert search_response.status_code == 200
    assert search_response.json[0]["virtual_path"] == "drive/Studium/example-document.pdf"


def test_onedrive_cloud_sync_mirrors_remote_documents_before_indexing(backend_module, monkeypatch):
    backend_module.MICROSOFT_GRAPH_CLIENT_ID = "client-id"
    backend_module.upsert_cloud_connection({
        "id": "onedrive-sync",
        "provider": "onedrive",
        "display_name": "OneDrive Sync",
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
        "account": {"userPrincipalName": "user@example.com"},
    })
    monkeypatch.setattr(backend_module, "ensure_onedrive_remote_folder_path", lambda connection, remote_path: {"id": "folder-id"})
    remote_files = [{
        "remote_item_id": "remote-1",
        "relative_path": "one/test.pdf",
        "remote_path": "Tracked/one/test.pdf",
        "title": "test.pdf",
        "size": 120,
        "etag": "etag-1",
        "ctag": "ctag-1",
        "last_modified_at": "2026-01-01T00:00:00Z",
    }]

    def fake_download(connection, remote_item, local_path):
        write_text_pdf(Path(local_path), "onedrive sync unique alpha")

    monkeypatch.setattr(backend_module, "list_onedrive_pdf_files", lambda connection, remote_root_path: list(remote_files))
    monkeypatch.setattr(backend_module, "download_onedrive_file", fake_download)
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": "onedrive",
                "connection_id": "onedrive-sync",
                "remote_path": "Tracked",
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )

    backend_module.run_cloud_sync_task("test_index", "onedrive-sync-1", "test")

    assert backend_module.task_status["onedrive-sync-1"]["status"] == "done"
    config = backend_module.load_index_config("test_index")
    mirrored_pdf = Path(config["cloud"]["root_path"]) / "one" / "test.pdf"
    assert mirrored_pdf.exists()
    client = backend_module.app.test_client()
    search_response = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "onedrive sync unique"},
        headers=BYPASS_HEADERS,
    )
    assert search_response.status_code == 200
    assert search_response.json[0]["source"] == "cloud"
    assert search_response.json[0]["virtual_path"] == "drive/one/test.pdf"

    remote_files.clear()
    backend_module.run_cloud_sync_task("test_index", "onedrive-sync-2", "test")

    assert not mirrored_pdf.exists()
    deleted_search = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "onedrive sync unique"},
        headers=BYPASS_HEADERS,
    )
    assert deleted_search.status_code == 200
    assert deleted_search.json == []


@pytest.mark.parametrize(
    "provider,connection_id,ensure_func,list_func,download_func",
    [
        ("google_drive", "google-drive-sync", "ensure_google_drive_remote_folder_path", "list_google_drive_pdf_files", "download_google_drive_file"),
        ("dropbox", "dropbox-sync", "ensure_dropbox_remote_folder_path", "list_dropbox_pdf_files", "download_dropbox_file"),
    ],
)
def test_api_cloud_provider_sync_mirrors_remote_documents_before_indexing(
    backend_module,
    monkeypatch,
    provider,
    connection_id,
    ensure_func,
    list_func,
    download_func,
):
    backend_module.upsert_cloud_connection({
        "id": connection_id,
        "provider": provider,
        "display_name": provider,
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
    })
    monkeypatch.setattr(backend_module, ensure_func, lambda connection, remote_path: {"id": "folder-id"})
    remote_files = [{
        "remote_item_id": "remote-1",
        "relative_path": "one/test.pdf",
        "remote_path": "Tracked/one/test.pdf",
        "title": "test.pdf",
        "size": 120,
        "etag": "etag-1",
        "ctag": "ctag-1",
        "last_modified_at": "2026-01-01T00:00:00Z",
    }]

    def fake_download(connection, remote_item, local_path):
        write_text_pdf(Path(local_path), f"{provider} sync unique alpha")

    monkeypatch.setattr(backend_module, list_func, lambda connection, remote_root_path: list(remote_files))
    monkeypatch.setattr(backend_module, download_func, fake_download)
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": provider,
                "connection_id": connection_id,
                "remote_path": "Tracked",
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )

    backend_module.run_cloud_sync_task("test_index", f"{provider}-sync-1", "test")

    assert backend_module.task_status[f"{provider}-sync-1"]["status"] == "done"
    config = backend_module.load_index_config("test_index")
    mirrored_pdf = Path(config["cloud"]["root_path"]) / "one" / "test.pdf"
    assert mirrored_pdf.exists()
    client = backend_module.app.test_client()
    search_response = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": f"{provider} sync unique"},
        headers=BYPASS_HEADERS,
    )
    assert search_response.status_code == 200
    assert search_response.json[0]["source"] == "cloud"
    assert search_response.json[0]["virtual_path"] == "drive/one/test.pdf"

    remote_files.clear()
    backend_module.run_cloud_sync_task("test_index", f"{provider}-sync-2", "test")

    assert not mirrored_pdf.exists()


@pytest.mark.parametrize(
    "provider,connection_id,ensure_func,upload_func,delete_func",
    [
        ("google_drive", "google-drive-update", "ensure_google_drive_remote_folder_path", "upload_google_drive_file_to_path", "delete_google_drive_file_by_path"),
        ("dropbox", "dropbox-update", "ensure_dropbox_remote_folder_path", "upload_dropbox_file_to_path", "delete_dropbox_file_by_path"),
    ],
)
def test_api_cloud_provider_update_and_delete_use_remote_cloud_path(
    backend_module,
    monkeypatch,
    tmp_path,
    provider,
    connection_id,
    ensure_func,
    upload_func,
    delete_func,
):
    backend_module.upsert_cloud_connection({
        "id": connection_id,
        "provider": provider,
        "display_name": provider,
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
    })
    monkeypatch.setattr(backend_module, ensure_func, lambda connection, remote_path: {"id": "folder-id"})
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": provider,
                "connection_id": connection_id,
                "remote_path": "Tracked",
                "sync_enabled": False,
            },
        },
    )
    uploads = []
    deletes = []
    monkeypatch.setattr(
        backend_module,
        upload_func,
        lambda connection, remote_path, local_path, content_type="application/octet-stream": uploads.append(
            (remote_path, local_path, content_type)
        ) or {"remote_path": remote_path},
    )
    monkeypatch.setattr(
        backend_module,
        delete_func,
        lambda connection, remote_path: deletes.append(remote_path) or {"remote_path": remote_path, "deleted": True},
    )
    local_pdf = tmp_path / "updated.pdf"
    write_text_pdf(local_pdf, "updated document")

    update_result = backend_module.sync_cloud_provider_document_update(
        "test_index",
        {"source": "cloud", "cloud_relative_path": "one/test.pdf"},
        str(local_pdf),
    )
    delete_result = backend_module.delete_cloud_provider_document(
        "test_index",
        {"source": "cloud", "cloud_relative_path": "one/test.pdf"},
    )

    assert update_result == {"remote_path": "Tracked/one/test.pdf"}
    assert delete_result == {"remote_path": "Tracked/one/test.pdf", "deleted": True}
    assert uploads == [("Tracked/one/test.pdf", str(local_pdf), "application/pdf")]
    assert deletes == ["Tracked/one/test.pdf"]


def test_onedrive_provider_document_update_uses_remote_cloud_path(backend_module, monkeypatch, tmp_path):
    backend_module.upsert_cloud_connection({
        "id": "onedrive-update",
        "provider": "onedrive",
        "display_name": "OneDrive Update",
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
    })
    monkeypatch.setattr(backend_module, "ensure_onedrive_remote_folder_path", lambda connection, remote_path: {"id": "folder-id"})
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": "onedrive",
                "connection_id": "onedrive-update",
                "remote_path": "Tracked",
                "sync_enabled": False,
            },
        },
    )
    uploads = []
    monkeypatch.setattr(
        backend_module,
        "upload_onedrive_file_to_path",
        lambda connection, remote_path, local_path, content_type="application/octet-stream": uploads.append(
            (remote_path, local_path, content_type)
        ) or {"remote_path": remote_path},
    )
    local_pdf = tmp_path / "updated.pdf"
    write_text_pdf(local_pdf, "updated document")

    result = backend_module.sync_cloud_provider_document_update(
        "test_index",
        {"source": "cloud", "cloud_relative_path": "one/test.pdf"},
        str(local_pdf),
    )

    assert result == {"remote_path": "Tracked/one/test.pdf"}
    assert uploads == [("Tracked/one/test.pdf", str(local_pdf), "application/pdf")]


def test_onedrive_provider_delete_uses_remote_cloud_path(backend_module, monkeypatch):
    backend_module.upsert_cloud_connection({
        "id": "onedrive-delete",
        "provider": "onedrive",
        "display_name": "OneDrive Delete",
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
    })
    monkeypatch.setattr(backend_module, "ensure_onedrive_remote_folder_path", lambda connection, remote_path: {"id": "folder-id"})
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": "onedrive",
                "connection_id": "onedrive-delete",
                "remote_path": "Tracked",
                "sync_enabled": False,
            },
        },
    )
    deleted_paths = []
    monkeypatch.setattr(
        backend_module,
        "delete_onedrive_file_by_path",
        lambda connection, remote_path: deleted_paths.append(remote_path) or {"remote_path": remote_path, "deleted": True},
    )

    result = backend_module.delete_cloud_provider_document(
        "test_index",
        {"source": "cloud", "cloud_relative_path": "one/test.pdf"},
    )

    assert result == {"remote_path": "Tracked/one/test.pdf", "deleted": True}
    assert deleted_paths == ["Tracked/one/test.pdf"]


def test_onedrive_low_data_preview_uploads_hidden_cloud_preview(backend_module, monkeypatch, tmp_path):
    backend_module.upsert_cloud_connection({
        "id": "onedrive-preview",
        "provider": "onedrive",
        "display_name": "OneDrive Preview",
        "status": "connected",
        "token": {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": backend_module.utc_expires_at(3600),
        },
    })
    monkeypatch.setattr(backend_module, "ensure_onedrive_remote_folder_path", lambda connection, remote_path: {"id": "folder-id"})
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "provider": "onedrive",
                "connection_id": "onedrive-preview",
                "remote_path": "Tracked",
                "sync_enabled": False,
            },
        },
    )
    config = backend_module.load_index_config("test_index")
    pdf_path = Path(config["cloud"]["root_path"]) / "one" / "preview.pdf"
    write_text_pdf(pdf_path, "preview text")
    monkeypatch.setattr(
        backend_module,
        "find_document_metadata_by_path",
        lambda path: (
            "test_index",
            {
                "id": "cloud-doc",
                "title": "preview.pdf",
                "path": str(pdf_path),
                "source": "cloud",
                "virtual_path": "OneDrive/one/preview.pdf",
                "cloud_relative_path": "one/preview.pdf",
            },
        ),
    )
    uploads = []
    monkeypatch.setattr(
        backend_module,
        "upload_onedrive_file_to_path",
        lambda connection, remote_path, local_path, content_type="application/octet-stream": uploads.append(
            (remote_path, local_path, content_type)
        ) or {"remote_path": remote_path},
    )
    cache_path = Path(config["cloud"]["preview_folder"]) / "ab" / "cache.jpg"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"jpg")

    result = backend_module.sync_low_data_preview_to_provider(str(pdf_path), str(cache_path))

    assert result == {"remote_path": "Tracked/.pdfsearchengine/previews/test_index/ab/cache.jpg"}
    assert uploads == [("Tracked/.pdfsearchengine/previews/test_index/ab/cache.jpg", str(cache_path), "image/jpeg")]


def test_cloud_low_data_preview_cache_uses_hidden_cloud_folder(backend_module, tmp_path):
    cloud_root = tmp_path / "drive"
    cloud_pdf = cloud_root / "one" / "preview.pdf"
    write_text_pdf(cloud_pdf, "cloud preview cache text")
    backend_module.save_index_config(
        "test_index",
        {
            "mode": "cloud",
            "cloud": {
                "root_path": str(cloud_root),
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )
    backend_module.run_cloud_sync_task("test_index", "cloud-task-preview", "test")

    cache_path = Path(backend_module.get_low_data_preview_cache_path(str(cloud_pdf), 1))

    assert cache_path.is_relative_to(cloud_root / ".pdfsearchengine" / "previews" / "test_index")
    assert cache_path.suffix == ".jpg"


def test_hybrid_cloud_sync_prefers_identical_cloud_file_over_standalone_copy(backend_module, tmp_path):
    local_pdf = Path(backend_module.PDF_FOLDER) / "test_index" / "already_indexed.pdf"
    write_text_pdf(local_pdf, "hybrid duplicate unique text")
    backend_module.add_to_index("test_index", str(local_pdf), local_pdf.name)

    cloud_root = tmp_path / "drive"
    cloud_pdf = cloud_root / "one" / "already_indexed.pdf"
    cloud_pdf.parent.mkdir(parents=True)
    shutil.copy2(local_pdf, cloud_pdf)

    backend_module.save_index_config(
        "test_index",
        {
            "mode": "hybrid",
            "cloud": {
                "root_path": str(cloud_root),
                "display_root_name": "drive",
                "sync_enabled": False,
            },
        },
    )
    backend_module.run_cloud_sync_task("test_index", "cloud-task-dedupe", "test")

    client = backend_module.app.test_client()
    response = client.get(
        "/search",
        query_string={"index_name": "test_index", "q": "hybrid duplicate"},
        headers=BYPASS_HEADERS,
    )

    assert response.status_code == 200
    assert len(response.json) == 1
    assert response.json[0]["source"] == "cloud"
    assert response.json[0]["path"] == str(cloud_pdf)
    assert not local_pdf.exists()
    assert (
        Path(backend_module.DELETED_FOLDER)
        / "test_index"
        / "cloud-deduplicated"
        / "already_indexed.pdf"
    ).exists()


def test_upload_task_reports_antivirus_readiness_failure(backend_module, tmp_path, monkeypatch):
    upload_path = tmp_path / "upload.pdf"
    upload_path.write_bytes(b"%PDF-1.4\n")

    def fake_wait_for_clamav(max_wait=None, return_message=False):
        if return_message:
            return False, "socket refused"
        return False

    monkeypatch.setattr(backend_module, "wait_for_clamav", fake_wait_for_clamav)
    backend_module.task_status.clear()

    backend_module.process_uploaded_file(
        "test_index",
        str(upload_path),
        "upload.pdf",
        "task-av",
        skip_av=False,
    )

    task = backend_module.task_status["task-av"]

    assert task["status"] == "failed"
    assert task["message"] == "Antivirus not ready"
    assert task["result"]["failed_files"] == ["upload.pdf"]
    assert task["result"]["failed_details"] == [
        {"file": "upload.pdf", "error": "Antivirus not ready: socket refused"}
    ]
    assert not upload_path.exists()


def test_zip_upload_task_reports_processed_duplicate_and_failed_files(backend_module, tmp_path, monkeypatch):
    zip_path = tmp_path / "upload.zip"
    with zipfile.ZipFile(zip_path, "w") as zip_file:
        zip_file.writestr("good.pdf", b"good")
        zip_file.writestr("duplicate.pdf", b"duplicate")
        zip_file.writestr("failed.pdf", b"failed")

    class FakeWriter:
        def commit(self):
            pass

    class FakeIndex:
        def writer(self):
            return FakeWriter()

    def fake_process_single_file(file_path, index_name, ix=None, writer=None, coordinator_task_id=None):
        filename = Path(file_path).name
        if filename == "duplicate.pdf":
            return "duplicate"
        if filename == "failed.pdf":
            raise RuntimeError("cannot process")
        return True

    monkeypatch.setattr(backend_module, "create_or_open_index", lambda index_name, check_access=False: (FakeIndex(), index_name))
    monkeypatch.setattr(backend_module, "process_single_file", fake_process_single_file)
    backend_module.task_status.clear()

    backend_module.process_uploaded_file("test_index", str(zip_path), "upload.zip", "task-zip", skip_av=True)

    task = backend_module.task_status["task-zip"]
    result = task["result"]

    assert task["status"] == "done"
    assert result["processed_files"] == ["good.pdf"]
    assert result["duplicate_files"] == ["duplicate.pdf"]
    assert result["duplicates_skipped"] == 1
    assert result["failed_files"] == ["failed.pdf"]
    assert result["failed_details"] == [{"file": "failed.pdf", "error": "cannot process"}]


def test_zip_upload_pauses_after_current_document_when_coordinator_locks(backend_module, tmp_path, monkeypatch):
    zip_path = tmp_path / "upload.zip"
    with zipfile.ZipFile(zip_path, "w") as zip_file:
        zip_file.writestr("first.pdf", b"first")
        zip_file.writestr("second.pdf", b"second")

    events = []
    commits = []

    class FakeCoordinator:
        enabled = True
        poll_seconds = 0.01
        wrapup_seconds = 0
        default_seconds_per_document = 20

        def __init__(self):
            self.status_calls = 0

        def get_status(self):
            self.status_calls += 1
            if self.status_calls in {2, 3}:
                return {
                    "enabled": True,
                    "available": True,
                    "restricted": True,
                    "reason": "Jellyfin stream active",
                    "checked_at": "now",
                    "source": "test",
                    "error": None,
                }
            return {
                "enabled": True,
                "available": True,
                "restricted": False,
                "reason": "",
                "checked_at": "now",
                "source": "test",
                "error": None,
            }

        def notify(self, action, payload=None):
            events.append(action)
            return True

    class FakeWriter:
        def commit(self, *args, **kwargs):
            commits.append("commit")

        def cancel(self):
            pass

    class FakeIndex:
        def writer(self):
            return FakeWriter()

    def fake_process_single_file(file_path, index_name, ix=None, writer=None, coordinator_task_id=None):
        return True

    monkeypatch.setattr(backend_module, "job_coordinator", FakeCoordinator())
    monkeypatch.setattr(
        backend_module,
        "create_or_open_index",
        lambda index_name, check_access=False: (FakeIndex(), index_name),
    )
    monkeypatch.setattr(backend_module, "process_single_file", fake_process_single_file)
    monkeypatch.setattr(
        backend_module,
        "estimate_remaining_indexing_seconds",
        lambda remaining_count, durations=None: remaining_count * 60,
    )
    backend_module.task_status.clear()

    backend_module.process_uploaded_file(
        "test_index",
        str(zip_path),
        "upload.zip",
        "task-zip-pause",
        skip_av=True,
    )

    task = backend_module.task_status["task-zip-pause"]
    result = task["result"]
    state = backend_module.load_coordinator_state_file()

    assert task["status"] == "done"
    assert result["processed_files"] == ["first.pdf", "second.pdf"]
    assert "task_blocked" in events
    assert "task_resumed" in events
    assert len(commits) >= 2
    assert state["paused_tasks"] == {}


def test_local_single_user_mode_grants_only_direct_host_access(backend_module, monkeypatch):
    monkeypatch.setenv('LOCAL_SINGLE_USER_MODE', 'true')
    app = backend_module.app
    with app.test_request_context('/auth/me', environ_base={'REMOTE_ADDR': '127.0.0.1'}):
        principal = backend_module.get_current_principal()
        assert principal['type'] == 'local'
        assert principal['is_admin']
    with app.test_request_context('/auth/me', environ_base={'REMOTE_ADDR': '203.0.113.2'}):
        assert not backend_module.get_current_principal()['is_admin']
    with app.test_request_context('/auth/me', environ_base={'REMOTE_ADDR': '127.0.0.1'},
                                  headers={'X-Forwarded-For': '198.51.100.8'}):
        assert not backend_module.get_current_principal()['is_admin']


def test_empty_roles_and_private_legacy_mappings(backend_module, tmp_path, monkeypatch):
    roles_path = tmp_path / 'missing' / 'roles.json'
    mappings_path = tmp_path / 'role_indices.json'
    monkeypatch.setattr(backend_module, 'ROLE_CONFIG_PATH', str(roles_path))
    monkeypatch.setattr(backend_module, 'ROLE_INDEX_CONFIG_PATH', str(mappings_path))
    assert backend_module.load_roles() == {}
    assert backend_module.get_role_to_indices() == {}
    backend_module.save_roles({'198.51.100.1': 'reader'})
    assert backend_module.load_roles() == {'198.51.100.1': 'reader'}
    mappings_path.write_text('{"reader": ["documents"]}')
    assert backend_module.get_role_to_indices() == {'reader': ['documents']}


def test_test_bypass_requires_explicit_test_mode(backend_module, monkeypatch):
    monkeypatch.delenv("PDFSEARCH_TEST_MODE", raising=False)
    with backend_module.app.test_request_context(headers=BYPASS_HEADERS):
        assert backend_module.is_test_bypass_request() is False


def test_test_bypass_rejects_short_tokens_even_in_test_mode(backend_module, monkeypatch):
    monkeypatch.setenv("PDFSEARCH_TEST_MODE", "true")
    monkeypatch.setenv("TEST_BYPASS_TOKEN", "short-token")
    with backend_module.app.test_request_context(headers={"X-Test-Bypass": "short-token"}):
        assert backend_module.is_test_bypass_request() is False


def test_test_bypass_rejects_wrong_token(backend_module):
    with backend_module.app.test_request_context(headers={"X-Test-Bypass": "x" * 40}):
        assert backend_module.is_test_bypass_request() is False
    with backend_module.app.test_request_context(headers=BYPASS_HEADERS):
        assert backend_module.is_test_bypass_request() is True


def test_mail_token_comparison_rejects_missing_and_wrong_values(backend_module):
    assert backend_module.mail_token_matches("mail-token") is True
    assert backend_module.mail_token_matches("wrong") is False
    assert backend_module.mail_token_matches(None) is False
    assert backend_module.mail_token_matches("") is False


def test_missing_default_route_means_no_trusted_host_gateway(backend_module, monkeypatch):
    def no_route(*args, **kwargs):
        raise backend_module.subprocess.CalledProcessError(1, "ip route")

    monkeypatch.setattr(backend_module, "docker_gateway_ip_cache", None)
    monkeypatch.setattr(backend_module.subprocess, "check_output", no_route)
    assert backend_module.real_get_docker_gateway_ip() is None
    assert backend_module.docker_gateway_ip_cache is None
