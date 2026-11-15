import os
import time
import uuid
from contextlib import suppress

import fitz  # PyMuPDF
import pytest
import requests
from fpdf import FPDF


BASE_URL = os.environ.get("BASE_URL", "http://backend:5050").rstrip("/")
TEST_INDEX = os.environ.get("INTEGRATION_TEST_INDEX", "test_index")
REQUEST_TIMEOUT = float(os.environ.get("INTEGRATION_REQUEST_TIMEOUT", "10"))
TASK_TIMEOUT = float(os.environ.get("INTEGRATION_TASK_TIMEOUT", "90"))
SEARCH_TIMEOUT = float(os.environ.get("INTEGRATION_SEARCH_TIMEOUT", "30"))
POLL_INTERVAL = float(os.environ.get("INTEGRATION_POLL_INTERVAL", "0.5"))
SKIP_COORDINATOR_BLOCKED = os.environ.get("INTEGRATION_SKIP_COORDINATOR_BLOCKED", "true").lower() not in {
    "0",
    "false",
    "no",
    "off",
}

pytestmark = pytest.mark.integration


def bypass_headers():
    token = os.getenv("TEST_BYPASS_TOKEN")
    return {"X-Test-Bypass": token} if token else {}


def response_summary(response):
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    return f"HTTP {response.status_code}: {payload}"


def task_failure_summary(task_id, payload):
    return f"Task {task_id} ended with status {payload.get('status')}: {payload}"


@pytest.fixture(scope="session")
def http():
    session = requests.Session()
    session.headers.update(bypass_headers())
    try:
        response = session.get(f"{BASE_URL}/health", timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        pytest.skip(f"Live backend is not available at {BASE_URL}: {exc}")
    if response.status_code != 200:
        pytest.skip(f"Live backend health check failed at {BASE_URL}: {response_summary(response)}")
    return session


def request(http, method, path, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    return http.request(method, f"{BASE_URL}{path}", **kwargs)


def wait_for_task_completion(http, task_id, timeout=TASK_TIMEOUT, interval=POLL_INTERVAL):
    start_time = time.monotonic()
    last_payload = None
    while time.monotonic() - start_time < timeout:
        response = request(http, "GET", f"/task-status/{task_id}")
        if response.status_code == 404:
            time.sleep(interval)
            continue
        assert response.status_code == 200, response_summary(response)

        payload = response.json()
        last_payload = payload
        status = payload.get("status")
        if status == "done":
            return payload
        if status == "failed":
            pytest.fail(task_failure_summary(task_id, payload))
        if status in {"blocked", "paused"} and SKIP_COORDINATOR_BLOCKED:
            coordinator = payload.get("coordinator") or (payload.get("result") or {}).get("coordinator") or {}
            reason = coordinator.get("reason") or payload.get("message") or status
            pytest.skip(f"Task {task_id} is {status} by the external job coordinator: {reason}")

        time.sleep(interval)

    if last_payload and last_payload.get("status") in {"blocked", "paused"} and SKIP_COORDINATOR_BLOCKED:
        coordinator = last_payload.get("coordinator") or (last_payload.get("result") or {}).get("coordinator") or {}
        reason = coordinator.get("reason") or last_payload.get("message") or last_payload.get("status")
        pytest.skip(f"Task {task_id} is still {last_payload.get('status')} by the external job coordinator: {reason}")

    pytest.fail(f"Task {task_id} did not complete within {timeout} seconds. Last status: {last_payload}")


def wait_for_search_result(http, unique_text, timeout=SEARCH_TIMEOUT, interval=POLL_INTERVAL):
    last_payload = None
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        response = request(
            http,
            "GET",
            "/search",
            params={"q": unique_text, "index_name": TEST_INDEX},
        )
        assert response.status_code == 200, response_summary(response)
        last_payload = response.json()
        if last_payload:
            return last_payload[0]
        time.sleep(interval)
    pytest.fail(f"No search result appeared for {unique_text!r} within {timeout} seconds. Last payload: {last_payload}")


def wait_for_no_search_results(http, unique_text, timeout=SEARCH_TIMEOUT, interval=POLL_INTERVAL):
    last_payload = None
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        response = request(
            http,
            "GET",
            "/search",
            params={"q": unique_text, "index_name": TEST_INDEX},
        )
        assert response.status_code == 200, response_summary(response)
        last_payload = response.json()
        if not last_payload:
            return
        time.sleep(interval)
    pytest.fail(f"Search still returned results for {unique_text!r} within {timeout} seconds: {last_payload}")


def generate_test_pdf(path, content):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=content, ln=True)
    pdf.output(str(path))


def clean_test_index(http):
    response = request(http, "POST", "/clean-test-index")
    assert response.status_code == 200, response_summary(response)


def upload_pdf(http, pdf_path, unique_text):
    with open(pdf_path, "rb") as handle:
        response = request(
            http,
            "POST",
            "/add-document",
            files={"file": (pdf_path.name, handle, "application/pdf")},
            data={"index_name": TEST_INDEX},
        )
    assert response.status_code == 202, response_summary(response)
    task_id = response.json().get("task_id")
    assert task_id, f"No task_id returned: {response_summary(response)}"
    upload_task = wait_for_task_completion(http, task_id)
    result = wait_for_search_result(http, unique_text)
    return {
        "id": result["id"],
        "path": result["path"],
        "title": result.get("title") or pdf_path.name,
        "unique_text": unique_text,
        "upload_task": upload_task,
    }


def delete_document(http, document_id):
    response = request(
        http,
        "POST",
        "/delete-document",
        json={"index_name": TEST_INDEX, "id": document_id},
    )
    assert response.status_code == 202, response_summary(response)
    task_id = response.json().get("task_id")
    assert task_id, f"No task_id returned: {response_summary(response)}"
    return wait_for_task_completion(http, task_id)


@pytest.fixture
def clean_index(http):
    clean_test_index(http)
    yield
    with suppress(Exception):
        clean_test_index(http)


@pytest.fixture
def uploaded_document(http, clean_index, tmp_path):
    unique_text = f"this_is_unique_test_content_{uuid.uuid4().hex}"
    pdf_path = tmp_path / "sample_test.pdf"
    generate_test_pdf(pdf_path, unique_text)
    document = upload_pdf(http, pdf_path, unique_text)
    yield document
    with suppress(Exception):
        delete_document(http, document["id"])


def test_list_indices(http):
    response = request(http, "GET", "/indices")
    assert response.status_code == 200, response_summary(response)
    payload = response.json()
    assert "available_indices" in payload
    assert isinstance(payload["available_indices"], list)
    assert "current_user" in payload


def test_verify_invalid_index(http):
    response = request(http, "POST", "/verify-index", json={"index_name": "nonexistent_index"})
    assert response.status_code in (400, 404), response_summary(response)


def test_download_invalid_path(http):
    response = request(http, "POST", "/download", json={"path": "/non/existing/file.pdf"})
    assert response.status_code in (400, 403, 404), response_summary(response)


def test_preview_page_fail(http):
    response = request(
        http,
        "GET",
        "/find-hit-page",
        params={"path": "/fake/path.pdf", "q": "anything"},
    )
    assert response.status_code in (400, 403, 404), response_summary(response)


def test_invalid_task_status(http):
    response = request(http, "GET", "/task-status/invalid-task-id-123")
    assert response.status_code in (400, 404), response_summary(response)


def test_clean_test_index(http):
    clean_test_index(http)


def test_upload_document(uploaded_document):
    assert uploaded_document["id"]
    assert uploaded_document["path"]
    assert uploaded_document["upload_task"]["status"] == "done"


def test_search_document(http, uploaded_document, tmp_path):
    result = wait_for_search_result(http, uploaded_document["unique_text"])
    assert result["id"] == uploaded_document["id"]

    download_response = request(
        http,
        "POST",
        "/download",
        json={"index_name": TEST_INDEX, "id": uploaded_document["id"]},
        stream=True,
    )
    assert download_response.status_code == 200, response_summary(download_response)

    downloaded_pdf_path = tmp_path / "downloaded.pdf"
    with open(downloaded_pdf_path, "wb") as handle:
        for chunk in download_response.iter_content(chunk_size=8192):
            handle.write(chunk)

    with fitz.open(downloaded_pdf_path) as document:
        text = "\n".join(page.get_text() for page in document)

    assert uploaded_document["unique_text"] in text


def test_folder_tree_lists_uploaded_document_metadata(http, uploaded_document):
    root_response = request(http, "GET", "/folder-tree", params={"index_name": TEST_INDEX, "path": ""})
    assert root_response.status_code == 200, response_summary(root_response)
    root_payload = root_response.json()
    uploaded_folder = next(
        (folder for folder in root_payload["folders"] if folder["name"] == "Uploaded documents"),
        None,
    )
    assert uploaded_folder, root_payload

    folder_response = request(
        http,
        "GET",
        "/folder-tree",
        params={"index_name": TEST_INDEX, "path": uploaded_folder["path"]},
    )
    assert folder_response.status_code == 200, response_summary(folder_response)
    files = folder_response.json()["files"]
    file_payload = next((item for item in files if item["id"] == uploaded_document["id"]), None)

    assert file_payload, files
    assert file_payload["source"] == "standalone"
    assert file_payload["size"] and file_payload["size"] > 0
    assert file_payload["modified_at"]


def test_delete_document(http, uploaded_document):
    delete_task = delete_document(http, uploaded_document["id"])
    assert delete_task["status"] == "done"
    wait_for_no_search_results(http, uploaded_document["unique_text"])


def test_search_after_delete(http, clean_index, tmp_path):
    unique_text = f"this_is_unique_deleted_content_{uuid.uuid4().hex}"
    pdf_path = tmp_path / "sample_deleted_test.pdf"
    generate_test_pdf(pdf_path, unique_text)
    document = upload_pdf(http, pdf_path, unique_text)

    delete_document(http, document["id"])
    wait_for_no_search_results(http, unique_text)
