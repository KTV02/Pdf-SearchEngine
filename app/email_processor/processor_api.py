# processor_api.py
from flask import Flask, request, jsonify
from processor_module import start_task, stop_task, list_tasks
import os
import json
import uuid
import logging
from logging.handlers import RotatingFileHandler


app = Flask(__name__)
EMAIL_CONFIG_DIR = os.environ.get("EMAIL_CONFIG_DIR", "/app/email_configs")
os.makedirs(EMAIL_CONFIG_DIR, exist_ok=True)

LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
LOG_FILE = os.path.join(LOG_DIR, "email_processor.log")
os.makedirs(LOG_DIR, exist_ok=True)

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=5)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        file_handler,
        logging.StreamHandler()
    ]
)

@app.route("/email-task", methods=["POST"])
def create_email_task():
    try:
        data = request.get_json()
        index_name = data.get("index_name")
        mode = data.get("mode")

        if not index_name or not mode:
            return jsonify({"error": "Missing index_name or mode"}), 400

        # Generate unique config path and save config
        task_id = str(uuid.uuid4())
        config_path = os.path.join(EMAIL_CONFIG_DIR, f"{task_id}.json")
        data["task_id"] = task_id
        with open(config_path, "w") as f:
            json.dump(data, f)

        task_id = start_task(mode, config_path, task_id=task_id)
        return jsonify({"message": f"Started {mode} task", "task_id": task_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stop-email-task", methods=["POST"])
def stop_email_task():
    try:
        data = request.get_json()
        task_id = data.get("task_id")
        if not task_id:
            return jsonify({"error": "Missing task_id"}), 400

        msg = stop_task(task_id)
        return jsonify({"message": msg}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/email-tasks", methods=["GET"])
def list_email_tasks():
    try:
        index_name = request.args.get("index_name")
        return jsonify({"tasks": list_tasks(index_name)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
