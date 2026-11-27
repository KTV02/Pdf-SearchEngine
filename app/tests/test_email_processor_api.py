import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace


def test_create_email_task_reuses_config_task_id(email_processor_api_module, monkeypatch):
    captured = {}

    def fake_start_task(mode, config_path, task_id=None):
        captured["mode"] = mode
        captured["config_path"] = config_path
        captured["task_id"] = task_id
        return task_id

    monkeypatch.setattr(email_processor_api_module, "start_task", fake_start_task)
    client = email_processor_api_module.app.test_client()

    response = client.post("/email-task", json={
        "index_name": "test_index",
        "mode": "monitor",
        "email": "person@example.com",
        "password": "secret",
        "imap_server": "imap.example.com",
        "imap_port": 993,
    })

    assert response.status_code == 200
    assert response.json["task_id"] == captured["task_id"]
    assert captured["mode"] == "monitor"

    config = json.loads(Path(captured["config_path"]).read_text())
    assert config["task_id"] == response.json["task_id"]
    assert config["index_name"] == "test_index"


def test_stop_email_task_uses_the_processors_stored_index(
    backend_module, email_processor_api_module, monkeypatch,
):
    from processor_module import cancel_flags, task_configs

    def stored_task(task_id, index_name):
        config_path = Path(email_processor_api_module.EMAIL_CONFIG_DIR) / f"{task_id}.json"
        config_path.write_text(json.dumps({"index_name": index_name}), encoding="utf-8")
        task_configs[task_id] = {"config_path": str(config_path), "mode": "monitor"}
        cancel_flags[task_id] = Event()
        return cancel_flags[task_id]

    own_event = stored_task("own-task", "test_index")
    other_event = stored_task("other-task", "second_index")
    mail_client = email_processor_api_module.app.test_client()

    def forward_stop(url, json):
        assert url.endswith("/stop-email-task")
        response = mail_client.post("/stop-email-task", json=json)
        return SimpleNamespace(status_code=response.status_code, json=response.get_json)

    monkeypatch.setattr(backend_module.requests, "post", forward_stop)
    client = backend_module.app.test_client()
    writer = {"Tailscale-User-Login": "writer@example.com"}

    denied = client.post("/stop-email-task", headers=writer, json={
        "task_id": "other-task", "index_name": "test_index",
    })
    assert denied.status_code == 404
    assert not other_event.is_set()
    assert "other-task" in task_configs

    assert client.post("/stop-email-task", headers=writer, json={"task_id": "other-task"}).status_code == 403
    assert not other_event.is_set()

    stopped = client.post("/stop-email-task", headers=writer, json={
        "task_id": "own-task", "index_name": "test_index",
    })
    assert stopped.status_code == 200
    assert own_event.is_set()

    admin = {"Tailscale-User-Login": "admin@example.com"}
    assert client.post("/stop-email-task", headers=admin, json={"task_id": "other-task"}).status_code == 200
    assert other_event.is_set()
