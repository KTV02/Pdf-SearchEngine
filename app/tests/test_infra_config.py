from pathlib import Path


def test_compose_uses_health_checked_official_clamav():
    compose_file = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = compose_file.read_text()

    assert "image: clamav/clamav-debian:stable" in compose
    assert "clamav-db:/var/lib/clamav" in compose
    assert "condition: service_healthy" in compose
    assert "clamdcheck.sh" in compose


def test_compose_exposes_optional_node_red_coordinator_config():
    compose_file = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = compose_file.read_text()

    assert "NODERED_COORDINATOR_URL=${NODERED_COORDINATOR_URL:-}" in compose
    assert "NODERED_COORDINATOR_STATUS_ENDPOINT" in compose
    assert "NODERED_COORDINATOR_STATUS_ENDPOINT=${NODERED_COORDINATOR_STATUS_ENDPOINT:-}" in compose
    assert "NODERED_COORDINATOR_ACTION_ENDPOINT" in compose
    assert "JOB_COORDINATOR_STATE_PATH" in compose


def test_compose_exposes_cloud_mount_and_sync_config():
    compose_file = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    # YAML folds continuation lines in plain scalars into spaces.
    compose = " ".join(compose_file.read_text().split())

    assert "${CLOUD_STORAGE_HOST_PATH:-./data/cloud}:/app/cloud" in compose
    assert "CLOUD_SYNC_INTERVAL_SECONDS=${CLOUD_SYNC_INTERVAL_SECONDS:-300}" in compose
    assert "CLOUD_SYNC_SCHEDULER_POLL_SECONDS=${CLOUD_SYNC_SCHEDULER_POLL_SECONDS:-30}" in compose
    assert "MANAGED_CLOUD_FOLDER=${MANAGED_CLOUD_FOLDER:-/app/cloud/managed}" in compose
    assert "CLOUD_CONNECTIONS_PATH=${CLOUD_CONNECTIONS_PATH:-/app/index_folder/cloud_connections.json}" in compose
    assert "CLOUD_PROVIDER_CONFIG_PATH=${CLOUD_PROVIDER_CONFIG_PATH:-/app/index_folder/cloud_provider_config.json}" in compose
    assert "MICROSOFT_GRAPH_CLIENT_ID=${MICROSOFT_GRAPH_CLIENT_ID:-}" in compose
    assert "MICROSOFT_GRAPH_SCOPES=${MICROSOFT_GRAPH_SCOPES:-offline_access User.Read Files.ReadWrite}" in compose
    assert "GOOGLE_DRIVE_CLIENT_ID=${GOOGLE_DRIVE_CLIENT_ID:-}" in compose
    assert "GOOGLE_DRIVE_SCOPES=${GOOGLE_DRIVE_SCOPES:-https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile}" in compose
    assert "DROPBOX_APP_KEY=${DROPBOX_APP_KEY:-}" in compose
    assert "DROPBOX_SCOPES=${DROPBOX_SCOPES:-account_info.read files.metadata.read files.content.read files.content.write}" in compose


def test_compose_exposes_integration_test_timing_config():
    compose_file = Path(__file__).resolve().parents[1] / "compose.integration.yml"
    compose = compose_file.read_text()

    assert "INTEGRATION_REQUEST_TIMEOUT" in compose
    assert "INTEGRATION_TASK_TIMEOUT" in compose
    assert "INTEGRATION_SEARCH_TIMEOUT" in compose
    assert "INTEGRATION_SKIP_COORDINATOR_BLOCKED" in compose


def test_installation_compose_never_enables_the_test_bypass():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()

    assert "TEST_BYPASS_TOKEN" not in compose
    assert "PDFSEARCH_TEST_MODE" not in compose
    assert "test-runner" not in compose


def test_integration_stack_is_isolated_and_uses_generated_tokens():
    compose = (Path(__file__).resolve().parents[1] / "compose.integration.yml").read_text()

    assert "name: pdfsearch-integration" in compose
    assert "internal: true" in compose
    assert "ports:" not in compose
    assert "${INTEGRATION_BYPASS_TOKEN:?" in compose
    assert "${INTEGRATION_MAIL_TOKEN:?" in compose
    assert "./data" not in compose and "HOST_PATH" not in compose


def test_frontend_third_party_scripts_are_pinned_with_integrity():
    import re
    import pytest
    page = Path(__file__).resolve().parents[1] / "frontend" / "index.html"
    if not page.exists():
        pytest.skip("frontend is not part of the isolated test image")
    html = page.read_text()
    external = re.findall(r'<script[^>]+src="https?://[^"]+"[^>]*>', html)
    assert external
    for tag in external:
        assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag
        assert re.search(r'@\d+\.\d+\.\d+/', tag)
