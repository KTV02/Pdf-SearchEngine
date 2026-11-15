import threading
import queue
import logging

logger = logging.getLogger(__name__)

class TaskQueueManager:
    def __init__(self):
        self.queues = {}  # index_name -> Queue
        self.locks = {}   # index_name -> Lock
        self.threads = {}  # index_name -> Thread
        self.manager_lock = threading.Lock()

    def _ensure_queue(self, index_name):
        with self.manager_lock:
            if index_name in self.queues:
                return

            self.queues[index_name] = queue.Queue()
            self.locks[index_name] = threading.Lock()
            thread = threading.Thread(target=self._worker, args=(index_name,), daemon=True)
            self.threads[index_name] = thread
            thread.start()

    def get_lock(self, index_name):
        self._ensure_queue(index_name)
        return self.locks[index_name]

    def enqueue(self, index_name, task_func, *args, **kwargs):
        self._ensure_queue(index_name)
        self.queues[index_name].put((task_func, args, kwargs))

    def _worker(self, index_name):
        q = self.queues[index_name]
        while True:
            try:
                task_func, args, kwargs = q.get()
                with self.locks[index_name]:
                    task_func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Task execution failed for index '{index_name}': {e}", exc_info=True)
            finally:
                q.task_done()
