import json
from pathlib import Path


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
