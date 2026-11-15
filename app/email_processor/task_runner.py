# task_runner.py
import json
import time
import logging
from processor import process_full_inbox, monitor_inbox

TASK_FILE = "task_queue.json"
LOG_FILE = "processor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

def load_tasks():
    try:
        with open(TASK_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def save_tasks(tasks):
    with open(TASK_FILE, 'w') as f:
        json.dump(tasks, f, indent=2)

def run():
    logging.info("📬 Email Processor started.")
    while True:
        tasks = load_tasks()
        remaining_tasks = []

        for task in tasks:
            email_config = task.get("config")
            action = task.get("action")

            if not email_config or not action:
                logging.warning("⚠️ Invalid task format, skipping...")
                continue

            if action == "digitalize":
                logging.info(f"📂 Running full inbox processing for {email_config['email']}")
                process_full_inbox(email_config)
            elif action == "monitor":
                logging.info(f"📡 Starting live monitoring for {email_config['email']}")
                monitor_inbox(email_config)
            else:
                logging.warning(f"❌ Unknown action: {action}")
                continue

        time.sleep(10)

if __name__ == "__main__":
    run()
