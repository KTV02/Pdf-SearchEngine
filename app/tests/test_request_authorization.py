import io
import json
from pathlib import Path

import pytest

import request_identity


ADMIN = {"Tailscale-User-Login": "admin@example.com"}
READER = {"Tailscale-User-Login": "reader@example.com"}
WRITER = {"Tailscale-User-Login": "writer@example.com"}
UNKNOWN = {"Tailscale-User-Login": "unregistered@example.com"}
OTHER_PEER = {"REMOTE_ADDR": "198.51.100.20"}


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/auth/me"),
    ("GET", "/indices"),
    ("GET", "/admin/access-config"),
    ("GET", "/admin/storage-config"),
    ("POST", "/admin/backup/run"),
    ("GET", "/tasks"),
    ("GET", "/task-status/test-task"),
    ("GET", "/job-coordinator/status"),
    ("GET", "/email-tasks"),
    ("GET", "/cloud-providers"),
    ("GET", "/cloud-provider-settings"),
    ("GET", "/cloud-connections/test-connection"),
    ("GET", "/cloud-connections/callback/google_drive"),
    ("GET", "/search"),
    ("GET", "/preview"),
    ("POST", "/download"),
])
def test_shared_routes_require_a_login_even_from_the_local_peer(backend_module, method, path):
    # The fixture's archived IP configuration lists the loopback peer as an admin.
    response = backend_module.app.test_client().open(path, method=method)
    assert response.status_code == 403
    assert response.json == {"error": "Access denied"}
    assert response.cache_control.no_store


def test_untrusted_peer_and_forwarding_headers_do_not_grant_access(backend_module, mocker):
    backend_module.save_roles({OTHER_PEER["REMOTE_ADDR"]: "admin", "192.0.2.0/24": "admin"})
    read = mocker.spy(backend_module, "read_access_config")
    response = backend_module.app.test_client().get(
        "/admin/access-config", environ_overrides=OTHER_PEER,
        headers={**ADMIN, "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "192.0.2.20"},
    )
    assert response.status_code == 403
    read.assert_not_called()


@pytest.mark.parametrize("headers", [
    {"Tailscale-User-Login": ""},
    {"Tailscale-User-Login": " \t "},
    {"Tailscale-User-Login": "missing-domain@"},
    {"Tailscale-User-Login": "tag:service"},
    {"Tailscale-User-Login": "admin@example.com, reader@example.com"},
    [("Tailscale-User-Login", "admin@example.com"), ("Tailscale-User-Login", "admin@example.com")],
    {"Tailscale-User-Login": "=?utf-8?q?admin@example.com=2Creader@example.com?="},
    {"Tailscale-User-Name": "admin@example.com"},
])
def test_missing_or_ambiguous_login_is_denied(backend_module, headers):
    response = backend_module.app.test_client().get("/auth/me", headers=headers)
    assert response.status_code == 403


def test_trusted_gateway_uses_normalized_login_in_shared_mode(backend_module, monkeypatch):
    monkeypatch.setattr(request_identity, "get_docker_gateway_ip", lambda: request_identity.parse_peer("192.0.2.20"))
    client = backend_module.app.test_client()
    peer = {"REMOTE_ADDR": "192.0.2.20"}
    assert client.get("/auth/me", environ_overrides=peer).status_code == 403

    response = client.get("/auth/me", environ_overrides=peer, headers={
        "Tailscale-User-Login": " Writer@Example.COM ",
        "X-Forwarded-For": "198.51.100.20",
    })
    assert response.status_code == 200
    assert response.json["login"] == "writer@example.com"
    assert response.json["writable_indices"] == ["test_index"]


def test_explicit_proxy_ip_replaces_the_default_trust(backend_module, monkeypatch):
    monkeypatch.setenv("PDFSEARCH_TRUSTED_PROXY_IP", "192.0.2.20")
    request_identity.configure_request_identity(backend_module.app)
    client = backend_module.app.test_client()
    assert client.get("/auth/me", headers=ADMIN).status_code == 403
    assert client.get("/auth/me", headers={**ADMIN, "X-Forwarded-For": "192.0.2.20"},
                      environ_overrides=OTHER_PEER).status_code == 403
    assert client.get("/auth/me", headers=ADMIN,
                      environ_overrides={"REMOTE_ADDR": "192.0.2.20"}).json["is_admin"] is True


@pytest.mark.parametrize(("setting", "value"), [
    ("LOCAL_SINGLE_USER_MODE", ""),
    ("LOCAL_SINGLE_USER_MODE", "yes"),
    ("PDFSEARCH_TRUSTED_PROXY_IP", "192.0.2.0/24"),
    ("PDFSEARCH_TRUSTED_PROXY_IP", "192.0.2.20,198.51.100.20"),
    ("PDFSEARCH_TRUSTED_PROXY_IP", "0.0.0.0"),
])
def test_malformed_proxy_configuration_fails_at_startup(backend_module, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match=setting):
        request_identity.configure_request_identity(backend_module.app)


@pytest.mark.parametrize("routes", [
    "", "default dev eth0", "default via invalid dev eth0",
    "default via 192.0.2.20 dev eth0\ndefault via 198.51.100.20 dev eth1",
])
def test_ambiguous_or_missing_docker_gateway_has_no_trust(monkeypatch, routes):
    monkeypatch.setattr(request_identity, "RUNNING_IN_CONTAINER", True)
    monkeypatch.setattr(request_identity.subprocess, "check_output", lambda *args, **kwargs: routes)
    request_identity._gateway_for_minute.cache_clear()
    try:
        assert request_identity.get_docker_gateway_ip() is None
    finally:
        request_identity._gateway_for_minute.cache_clear()


def test_docker_gateway_detection_requires_a_container_and_one_route(monkeypatch):
    def route_query(command, **kwargs):
        assert command == ["ip", "-4", "route", "show", "default"]
        return "default via 192.0.2.20 dev eth0\n"

    monkeypatch.setattr(request_identity.subprocess, "check_output", route_query)
    request_identity._gateway_for_minute.cache_clear()
    try:
        monkeypatch.setattr(request_identity, "RUNNING_IN_CONTAINER", False)
        assert request_identity.get_docker_gateway_ip() is None
        request_identity._gateway_for_minute.cache_clear()
        monkeypatch.setattr(request_identity, "RUNNING_IN_CONTAINER", True)
        assert request_identity.get_docker_gateway_ip() == request_identity.parse_peer("192.0.2.20")
    finally:
        request_identity._gateway_for_minute.cache_clear()


def test_local_mode_accepts_direct_loopback_and_gateway_access_only(backend_module, monkeypatch):
    monkeypatch.setenv("LOCAL_SINGLE_USER_MODE", "true")
    request_identity.configure_request_identity(backend_module.app)
    monkeypatch.setattr(request_identity, "get_docker_gateway_ip", lambda: request_identity.parse_peer("192.0.2.20"))
    client = backend_module.app.test_client()

    for addr, host in [("127.0.0.1", "localhost:5050"), ("::1", "[::1]"), ("192.0.2.20", "127.0.0.1")]:
        response = client.get("/auth/me", headers={"Host": host}, environ_overrides={"REMOTE_ADDR": addr})
        assert response.status_code == 200
        assert response.json["type"] == "local"
        assert response.json["is_admin"] is True
    for headers in [ADMIN, {"Tailscale-User-Login": ""}, {"X-Forwarded-For": ""},
                    {"Forwarded": "for=198.51.100.20"}, {"Host": "untrusted.example.com"}]:
        assert client.get("/auth/me", headers=headers).status_code == 403
    assert client.get("/auth/me", environ_overrides=OTHER_PEER).status_code == 403
    assert client.post("/admin/indices", json={"index_name": "local_index"}).status_code == 201


@pytest.mark.parametrize(("login", "header"), [
    ("reader@github", "Reader@GitHub"),
    ("reader@passkey", "reader@passkey"),
    ("müller@example.com", "=?utf-8?q?M=C3=BCller@example.com?="),
])
def test_serve_login_formats_preserve_registered_grants(backend_module, login, header):
    backend_module.save_access_config({"indices": ["test_index"], "users": {login: ["test_index"]}})
    response = backend_module.app.test_client().get("/indices", headers={"Tailscale-User-Login": header})
    assert response.status_code == 200
    assert response.json["current_user"]["login"] == login
    assert response.json["available_indices"] == ["test_index"]
    assert response.json["current_user"]["writable_indices"] == []


def test_unknown_login_only_gets_empty_frontend_bootstrap(backend_module):
    client = backend_module.app.test_client()
    for path in ["/auth/me", "/indices"]:
        response = client.get(path, headers=UNKNOWN)
        assert response.status_code == 200
        assert response.json["available_indices"] == []
    assert client.get("/cloud-providers", headers=UNKNOWN).status_code == 403
    assert client.post("/admin/indices", headers=UNKNOWN, json={"index_name": "unclaimed"}).status_code == 403
    assert not (Path(backend_module.INDEX_FOLDER) / "unclaimed").exists()


def test_cloud_session_access_and_return_require_the_owner(backend_module, mocker):
    client = backend_module.app.test_client()
    session = {
        "id": "test-connection", "provider": "google_drive", "state": "test-state",
        "status": "waiting_for_login",
        "owner": {"type": "tailscale", "id": "writer@example.com", "login": "writer@example.com"},
    }
    backend_module.cloud_auth_sessions[session["id"]] = session
    exchange = mocker.patch.object(backend_module.requests, "post")
    status_path = "/cloud-connections/test-connection"
    callback_path = "/cloud-connections/callback/google_drive?state=test-state&code=test-code"

    for headers in [{}, READER, UNKNOWN]:
        assert client.get(status_path, headers=headers).status_code == 403
        assert client.get(callback_path, headers=headers).status_code == 403
    assert client.get(status_path, headers=WRITER).status_code == 200
    assert client.get(callback_path, headers=ADMIN).status_code == 403
    assert session["status"] == "waiting_for_login"
    exchange.assert_not_called()


def test_removed_cloud_owner_cannot_use_stored_ownership(backend_module):
    backend_module.upsert_cloud_connection({
        "id": "test-connection", "provider": "google_drive",
        "owner": {"type": "tailscale", "login": UNKNOWN["Tailscale-User-Login"]},
    })
    response = backend_module.app.test_client().get("/cloud-connections/test-connection", headers=UNKNOWN)
    assert response.status_code == 403


def test_access_policy_is_read_once_per_request_and_not_reused(backend_module, mocker):
    client = backend_module.app.test_client()
    resolve = mocker.spy(backend_module, "resolve_request_principal")
    load = mocker.spy(backend_module, "read_access_config")
    discover = mocker.spy(backend_module, "discover_indices")

    with backend_module.app.app_context():
        assert client.get("/admin/access-config", headers=ADMIN).status_code == 200
        assert client.get("/indices", headers=WRITER).json["available_indices"] == ["test_index"]
        backend_module.save_access_config({"indices": ["test_index"], "users": {}})
        assert client.get("/indices", headers=WRITER).json["available_indices"] == []
    assert resolve.call_count == load.call_count == discover.call_count == 3


@pytest.mark.parametrize("users", [
    [], {"reader@example.com": {"admin": "false"}},
    {"reader@example.com": {"write": "test_index"}},
    {"Reader@Example.COM": {"admin": True}, "reader@example.com": []},
])
def test_malformed_access_config_is_rejected_and_fails_closed_on_disk(backend_module, users):
    client = backend_module.app.test_client()
    config = {"indices": ["test_index"], "users": users}
    invalid_update = client.put("/admin/access-config", json={"config": config}, headers=ADMIN)
    assert invalid_update.status_code == 400
    assert client.get("/admin/access-config", headers=ADMIN).status_code == 200

    Path(backend_module.ACCESS_CONFIG_PATH).write_text(json.dumps(config), encoding="utf-8")
    assert client.get("/auth/me", headers=READER).status_code == 503
    assert client.get("/health", environ_overrides=OTHER_PEER).json == {"status": "backend healthy"}


def test_duplicate_access_fields_are_rejected(backend_module):
    client = backend_module.app.test_client()
    config = '{"users": {"reader@example.com": {"admin": false, "admin": true}}}'
    assert client.put("/admin/access-config", data=config, content_type="application/json",
                      headers=ADMIN).status_code == 400
    Path(backend_module.ACCESS_CONFIG_PATH).write_text(config, encoding="utf-8")
    assert client.get("/auth/me", headers=READER).status_code == 503


def test_health_and_automatic_preflight_do_not_resolve_identity(backend_module, mocker):
    client = backend_module.app.test_client()
    resolve = mocker.spy(backend_module, "resolve_request_principal")
    assert client.get("/health", environ_overrides=OTHER_PEER).status_code == 200
    origin = "http://localhost"
    preflight = {"Origin": origin, "Access-Control-Request-Method": "PUT"}
    response = client.options("/admin/access-config", environ_overrides=OTHER_PEER, headers=preflight)
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert client.options("/admin/access-config", headers={**preflight, "Origin": "https://other.example.com"}).status_code == 403
    resolve.assert_not_called()


def test_mail_credential_only_authorizes_ingestion_to_an_existing_index(backend_module, mocker):
    enqueue = mocker.patch.object(backend_module.task_queue, "enqueue")
    client = backend_module.app.test_client()
    token = {"X-Mail-Token": "mail-token"}

    def upload(index_name="test_index", headers=token):
        return client.post("/add-document", environ_overrides=OTHER_PEER, headers=headers, data={
            "index_name": index_name, "file": (io.BytesIO(b"synthetic file"), "test.pdf"),
        })

    assert upload(headers={}).status_code == 403
    assert upload(headers={"X-Mail-Token": "wrong"}).status_code == 403
    assert upload(headers={**token, "Origin": "http://localhost"}).status_code == 403
    assert upload(headers={**token, **ADMIN}).status_code == 403
    assert upload("unconfigured_index").status_code == 403
    assert client.get("/tasks", environ_overrides=OTHER_PEER, headers=token).status_code == 403
    assert client.post("/append-document", environ_overrides=OTHER_PEER, headers=token).status_code == 403
    enqueue.assert_not_called()

    assert upload().status_code == 202
    enqueue.assert_called_once()
    assert enqueue.call_args.args[0] == "test_index"
    assert enqueue.call_args.args[-1] is False  # The mail credential leaves scanning enabled.


@pytest.mark.parametrize("token", ["", "local-mail-token", "default_mail_token", "replace-with-a-private-random-value"])
def test_public_mail_credentials_never_authorize_ingestion(backend_module, monkeypatch, token):
    monkeypatch.setattr(backend_module, "MAIL_TOKEN", token)
    response = backend_module.app.test_client().post(
        "/add-document", environ_overrides=OTHER_PEER, headers={"X-Mail-Token": token},
    )
    assert response.status_code == 403


def test_only_the_isolated_test_credential_can_clean_the_test_index(backend_module, monkeypatch):
    client = backend_module.app.test_client()
    bypass = {"X-Test-Bypass": "unit-test-bypass-token-0123456789abcdef"}
    index_dir = Path(backend_module.INDEX_FOLDER) / "test_index"
    index_dir.mkdir()

    assert client.post("/clean-test-index", headers=WRITER).status_code == 403
    assert client.post("/clean-test-index", headers=ADMIN).status_code == 403
    monkeypatch.setenv("PDFSEARCH_TEST_MODE", "false")
    assert client.post("/clean-test-index", headers=bypass, environ_overrides=OTHER_PEER).status_code == 403
    assert index_dir.exists()
    monkeypatch.setenv("PDFSEARCH_TEST_MODE", "true")
    assert client.post("/clean-test-index", headers=bypass, environ_overrides=OTHER_PEER).status_code == 200
    assert not index_dir.exists()


def test_shared_login_requires_a_configured_service_hostname(backend_module):
    client = backend_module.app.test_client()
    assert client.get("/auth/me", headers={**ADMIN, "Host": "alternate.example.com"}).status_code == 403
    assert client.get("/auth/me", headers={**ADMIN, "Host": "localhost:5050"}).status_code == 200


def test_unicode_and_ascii_logins_remain_distinct():
    assert request_identity.normalize_login("Kate@example.com") != request_identity.normalize_login("Kate@example.com")


def test_failed_gateway_lookup_recovers_at_next_refresh(monkeypatch):
    monkeypatch.setattr(request_identity, "RUNNING_IN_CONTAINER", True)
    clock = [0]
    monkeypatch.setattr(request_identity.time, "monotonic", lambda: clock[0])
    responses = iter(["", "default via 192.0.2.20 dev eth0"])
    monkeypatch.setattr(request_identity.subprocess, "check_output", lambda *a, **kw: next(responses))
    request_identity._gateway_for_minute.cache_clear()
    try:
        assert request_identity.get_docker_gateway_ip() is None
        assert request_identity.get_docker_gateway_ip() is None
        clock[0] = 60
        assert request_identity.get_docker_gateway_ip() == request_identity.parse_peer("192.0.2.20")
    finally:
        request_identity._gateway_for_minute.cache_clear()


def test_unconfigured_mail_import_is_reported_before_starting_a_job(backend_module, monkeypatch, mocker):
    monkeypatch.setattr(backend_module, "MAIL_TOKEN", "")
    start = mocker.patch.object(backend_module.requests, "post")
    response = backend_module.app.test_client().post(
        "/email-task", headers=WRITER, json={"mode": "monitor", "index_name": "test_index"})
    assert response.status_code == 503
    assert "MAIL_TOKEN" in response.json["error"]
    start.assert_not_called()
