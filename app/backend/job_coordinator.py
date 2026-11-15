import json
import os
import threading
from datetime import datetime
from urllib.parse import quote, urljoin

import requests


RESTRICTED_STATES = {
    "busy",
    "blocked",
    "locked",
    "paused",
    "restricted",
    "unavailable",
}

OPEN_STATES = {
    "allowed",
    "available",
    "clear",
    "free",
    "idle",
    "ok",
    "open",
    "ready",
    "unlocked",
}

RUNNING_DECISIONS = {"granted", "running"}
QUEUED_DECISIONS = {"queued"}
TERMINAL_DECISIONS = {"succeeded", "failed", "cancelled", "rejected"}


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_env_float(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def get_env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def utc_now():
    return datetime.utcnow().isoformat() + "Z"


def first_present(raw, keys):
    if not isinstance(raw, dict):
        return None
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def as_seconds(value):
    try:
        parsed = float(value)
        if parsed >= 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return None


def coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"} | RESTRICTED_STATES:
            return True
        if normalized in {"0", "false", "no", "off"} | OPEN_STATES:
            return False
    return bool(value)


def compact_text(value, default="", max_length=800):
    if value is None:
        return default
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    text = text.strip()
    if not text:
        return default
    if len(text) > max_length:
        return text[: max_length - 3] + "..."
    return text


class NodeRedJobCoordinator:
    """
    Optional HTTP integration for an external heavy-job coordinator.

    The coordinator is intentionally schema-tolerant because the local Pi project
    did not include a concrete Node-RED flow export. When disabled or unreachable,
    callers receive an open/fail-open status so standalone PDFSearch behavior
    remains unchanged.
    """

    def __init__(
        self,
        base_url="",
        status_endpoint="/job-coordinator/status",
        action_endpoint="",
        action_endpoints=None,
        request_timeout=2.0,
        poll_seconds=15.0,
        wrapup_seconds=45.0,
        default_seconds_per_document=20.0,
        fail_open=True,
        job_api_enabled=True,
        job_type="pdfsearchengine_index",
        job_label="PDFSearchEngine Index",
        request_defer=True,
        heartbeat_seconds=30.0,
        heartbeat_timeout_seconds=300,
        avoid_jellyfin_playback=True,
        logger=None,
        auth_token="",
    ):
        self.auth_token = auth_token
        self.base_url = (base_url or "").strip().rstrip("/")
        self.status_endpoint = status_endpoint or "/job-coordinator/status"
        self.action_endpoint = action_endpoint or ""
        self.action_endpoints = action_endpoints or {}
        self.request_timeout = max(0.1, float(request_timeout))
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.wrapup_seconds = max(0.0, float(wrapup_seconds))
        self.default_seconds_per_document = max(0.1, float(default_seconds_per_document))
        self.fail_open = bool(fail_open)
        self.job_api_enabled = bool(job_api_enabled)
        self.job_type = job_type or "pdfsearchengine_index"
        self.job_label = job_label or "PDFSearchEngine Index"
        self.request_defer = bool(request_defer)
        self.heartbeat_seconds = max(5.0, float(heartbeat_seconds))
        self.heartbeat_timeout_seconds = max(0, int(heartbeat_timeout_seconds))
        self.avoid_jellyfin_playback = bool(avoid_jellyfin_playback)
        self.logger = logger
        self._lease_lock = threading.Lock()
        self._leases_by_task = {}

    @classmethod
    def from_env(cls, logger=None):
        base_url = (
            os.environ.get("NODERED_COORDINATOR_URL")
            or os.environ.get("NODE_RED_COORDINATOR_URL")
            or os.environ.get("JOB_COORDINATOR_URL")
            or ""
        )
        job_api_enabled = parse_bool(
            os.environ.get("NODERED_COORDINATOR_JOB_API_ENABLED"),
            True,
        )
        default_status_endpoint = (
            "/coordinator/health" if job_api_enabled else "/job-coordinator/status"
        )
        action_endpoints = {}
        raw_action_endpoints = os.environ.get("NODERED_COORDINATOR_ACTION_ENDPOINTS", "")
        if raw_action_endpoints:
            try:
                parsed = json.loads(raw_action_endpoints)
                if isinstance(parsed, dict):
                    action_endpoints = {
                        str(action): str(endpoint)
                        for action, endpoint in parsed.items()
                        if endpoint
                    }
            except ValueError:
                if logger:
                    logger.warning("Invalid NODERED_COORDINATOR_ACTION_ENDPOINTS JSON; ignoring")

        token_file = os.environ.get("NODERED_COORDINATOR_TOKEN_FILE", "")
        auth_token = ""
        if token_file:
            with open(token_file, encoding="ascii") as token_stream:
                auth_token = token_stream.read(4097).strip()
            if not auth_token or len(auth_token) > 4096 or any(c.isspace() for c in auth_token):
                raise ValueError("Invalid coordinator token file")

        return cls(
            auth_token=auth_token,
            base_url=base_url,
            status_endpoint=(
                os.environ.get("NODERED_COORDINATOR_STATUS_ENDPOINT")
                or os.environ.get("JOB_COORDINATOR_STATUS_ENDPOINT")
                or default_status_endpoint
            ),
            action_endpoint=os.environ.get(
                "NODERED_COORDINATOR_ACTION_ENDPOINT",
                os.environ.get("JOB_COORDINATOR_ACTION_ENDPOINT", ""),
            ),
            action_endpoints=action_endpoints,
            request_timeout=get_env_float("NODERED_COORDINATOR_TIMEOUT_SECONDS", 2.0),
            poll_seconds=get_env_float("NODERED_COORDINATOR_POLL_SECONDS", 15.0),
            wrapup_seconds=get_env_float("NODERED_COORDINATOR_WRAPUP_SECONDS", 45.0),
            default_seconds_per_document=get_env_float(
                "NODERED_COORDINATOR_DEFAULT_SECONDS_PER_DOCUMENT",
                20.0,
            ),
            fail_open=parse_bool(os.environ.get("NODERED_COORDINATOR_FAIL_OPEN"), True),
            job_api_enabled=job_api_enabled,
            job_type=os.environ.get(
                "NODERED_COORDINATOR_JOB_TYPE",
                os.environ.get("JOB_COORDINATOR_JOB_TYPE", "pdfsearchengine_index"),
            ),
            job_label=os.environ.get(
                "NODERED_COORDINATOR_JOB_LABEL",
                os.environ.get("JOB_COORDINATOR_JOB_LABEL", "PDFSearchEngine Index"),
            ),
            request_defer=parse_bool(os.environ.get("NODERED_COORDINATOR_DEFER"), True),
            heartbeat_seconds=get_env_float("NODERED_COORDINATOR_HEARTBEAT_SECONDS", 30.0),
            heartbeat_timeout_seconds=get_env_int(
                "NODERED_COORDINATOR_HEARTBEAT_TIMEOUT_SECONDS",
                300,
            ),
            avoid_jellyfin_playback=parse_bool(
                os.environ.get("NODERED_COORDINATOR_AVOID_JELLYFIN_PLAYBACK"),
                True,
            ),
            logger=logger,
        )

    @property
    def enabled(self):
        return bool(self.base_url)

    @property
    def job_api_active(self):
        return self.enabled and self.job_api_enabled

    def url_for(self, endpoint):
        if not endpoint:
            return ""
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return urljoin(f"{self.base_url}/", endpoint.lstrip("/"))

    def _unavailable_status(self, source, error, reason=None):
        return {
            "enabled": self.enabled,
            "available": False,
            "restricted": False if self.fail_open else True,
            "reason": reason
            or (
                "Coordinator unavailable; continuing without external pause"
                if self.fail_open else
                "Coordinator unavailable; failing closed by configuration"
            ),
            "resume_after_seconds": None,
            "checked_at": utc_now(),
            "source": source,
            "raw": None,
            "error": str(error),
        }

    def _disabled_status(self):
        return {
            "enabled": False,
            "available": False,
            "restricted": False,
            "reason": "No external job coordinator configured",
            "checked_at": utc_now(),
            "source": None,
            "raw": None,
            "error": None,
        }

    def _auth_options(self):
        if not self.auth_token:
            return {}
        return {"headers": {"Authorization": "Bearer " + self.auth_token}, "allow_redirects": False}

    def _request_json(self, method, endpoint, **kwargs):
        url = self.url_for(endpoint)
        options = self._auth_options()
        if "headers" in kwargs and self.auth_token:
            kwargs["headers"] = {**kwargs["headers"], **options.pop("headers")}
        response = requests.request(method, url, timeout=self.request_timeout, **options, **kwargs)
        if response.status_code in (401, 403):
            response.raise_for_status()
        raw_text = response.text or ""
        try:
            raw = response.json()
        except ValueError:
            raw = {"message": raw_text.strip() or response.reason}

        if isinstance(raw, dict):
            raw.setdefault("statusCode", response.status_code)
        if response.status_code >= 400 and not isinstance(raw, dict):
            response.raise_for_status()
        return raw, url

    def _normalize_job_api_status(self, raw, source=None):
        raw = raw if isinstance(raw, dict) else {}
        job = raw.get("job") if isinstance(raw.get("job"), dict) else {}
        decision = raw.get("decision") or job.get("status") or ""
        decision = str(decision).strip().lower()
        try:
            status_code = int(raw.get("statusCode", 200))
        except (TypeError, ValueError):
            status_code = 200
        if not decision and (raw.get("ok") is False or status_code >= 400):
            decision = "cancelled" if status_code == 404 else "rejected"
        job_id = job.get("id") or raw.get("jobId")
        cancel_requested = bool(
            decision == "cancel_requested"
            or job.get("cancelRequested")
            or raw.get("cancelRequested")
        )
        restricted = decision in QUEUED_DECISIONS or cancel_requested
        reason = raw.get("message") or job.get("cancelReason") or job.get("result") or ""

        return {
            "enabled": self.enabled,
            "available": True,
            "restricted": restricted,
            "reason": str(reason),
            "resume_after_seconds": None,
            "checked_at": utc_now(),
            "source": source,
            "raw": raw,
            "error": None,
            "decision": decision,
            "job_id": job_id,
            "job": job,
            "cancel_requested": cancel_requested,
            "cancel_reason": job.get("cancelReason") or "",
        }

    def normalize_status(self, raw, source=None):
        if isinstance(raw, bool):
            restricted = raw
            reason = "Coordinator boolean status"
            resume_after_seconds = None
        elif isinstance(raw, dict):
            restricted = self._extract_restricted(raw)
            reason = self._extract_reason(raw)
            resume_after_seconds = self._extract_resume_after_seconds(raw)
        else:
            restricted = False
            reason = "Unrecognized coordinator status payload"
            resume_after_seconds = None

        return {
            "enabled": self.enabled,
            "available": True,
            "restricted": bool(restricted),
            "reason": reason,
            "resume_after_seconds": resume_after_seconds,
            "checked_at": utc_now(),
            "source": source or self.url_for(self.status_endpoint),
            "raw": raw,
            "error": None,
        }

    def _extract_restricted(self, raw):
        direct = first_present(raw, [
            "heavy_jobs_restricted",
            "heavy_jobs_locked",
            "restricted",
            "locked",
            "blocked",
            "paused",
            "heavy_job_lock",
        ])
        if direct is not None:
            return coerce_bool(direct)

        allowed = first_present(raw, [
            "allow_heavy_jobs",
            "heavy_jobs_allowed",
            "can_start_heavy_jobs",
            "can_start",
            "available",
        ])
        if allowed is not None:
            return not coerce_bool(allowed)

        state = first_present(raw, ["status", "state", "heavy_jobs_status", "job_status"])
        if state is not None:
            normalized = str(state).strip().lower()
            if normalized in RESTRICTED_STATES:
                return True
            if normalized in OPEN_STATES:
                return False

        for nested_key in ("heavy_jobs", "heavyJobs", "coordinator", "job_coordinator"):
            nested = raw.get(nested_key)
            if isinstance(nested, dict):
                return self._extract_restricted(nested)

        return False

    def _extract_reason(self, raw):
        if not isinstance(raw, dict):
            return ""
        reason = first_present(raw, ["reason", "message", "detail", "lock_reason"])
        if reason:
            return str(reason)
        for nested_key in ("heavy_jobs", "heavyJobs", "coordinator", "job_coordinator"):
            nested = raw.get(nested_key)
            nested_reason = self._extract_reason(nested)
            if nested_reason:
                return nested_reason
        return ""

    def _extract_resume_after_seconds(self, raw):
        if not isinstance(raw, dict):
            return None
        value = first_present(raw, [
            "resume_after_seconds",
            "retry_after_seconds",
            "retry_after",
            "estimated_lock_seconds",
        ])
        parsed = as_seconds(value)
        if parsed is not None:
            return parsed
        for nested_key in ("heavy_jobs", "heavyJobs", "coordinator", "job_coordinator"):
            parsed = self._extract_resume_after_seconds(raw.get(nested_key))
            if parsed is not None:
                return parsed
        return None

    def get_status(self):
        if not self.enabled:
            return self._disabled_status()

        status_url = self.url_for(self.status_endpoint)
        try:
            response = requests.get(status_url, timeout=self.request_timeout, **self._auth_options())
            response.raise_for_status()
            return self.normalize_status(response.json(), source=status_url)
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator status unavailable: %s", e)
            return self._unavailable_status(status_url, e)

    def get_overview_status(self):
        if not self.enabled:
            return self._disabled_status()
        if self.job_api_active:
            return self.get_job_api_overview_status()
        return self.get_status()

    def get_job_api_overview_status(self):
        endpoint = "/coordinator/health"
        source_url = self.url_for(endpoint)
        try:
            raw, source = self._request_json("GET", endpoint)
            state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
            health = raw.get("health") if isinstance(raw.get("health"), dict) else {}
            conditions = state.get("conditions") if isinstance(state.get("conditions"), dict) else {}
            jellyfin = conditions.get("jellyfin") if isinstance(conditions.get("jellyfin"), dict) else {}
            active_playback_count = jellyfin.get("activePlaybackCount") or 0
            active_jobs = state.get("activeJobs") if isinstance(state.get("activeJobs"), list) else []
            queue = state.get("queue") if isinstance(state.get("queue"), list) else []
            locks = state.get("locks") if isinstance(state.get("locks"), list) else []
            overall = health.get("overall") if isinstance(health.get("overall"), dict) else {}

            restricted = False
            reason = overall.get("message") or raw.get("message") or "Coordinator reachable"
            if active_playback_count:
                restricted = True
                reason = f"{active_playback_count} active Jellyfin playback session(s)"
            elif queue:
                restricted = True
                reason = f"{len(queue)} queued heavy job(s)"
            elif locks:
                restricted = True
                reason = f"{len(active_jobs)} running heavy job(s)"

            return {
                "enabled": True,
                "available": True,
                "restricted": restricted,
                "reason": reason,
                "resume_after_seconds": None,
                "checked_at": utc_now(),
                "source": source,
                "raw": raw,
                "error": None,
                "mode": "job_api",
                "active_jobs": len(active_jobs),
                "queued_jobs": len(queue),
                "locks": len(locks),
                "jellyfin_active_playback_count": active_playback_count,
            }
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator job API overview unavailable: %s", e)
            return self._unavailable_status(source_url, e)

    def notify(self, action, payload=None):
        if not self.enabled:
            return False

        endpoint = self.action_endpoints.get(action) or self.action_endpoint
        if not endpoint:
            return False

        event_url = self.url_for(endpoint)
        event_payload = dict(payload or {})
        event_payload.setdefault("action", action)
        event_payload.setdefault("sent_at", utc_now())

        try:
            response = requests.post(event_url, json=event_payload, timeout=self.request_timeout, **self._auth_options())
            response.raise_for_status()
            return True
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator action %s failed: %s", action, e)
            return False

    def _lease_snapshot(self, task_id):
        with self._lease_lock:
            lease = self._leases_by_task.get(task_id)
            return dict(lease) if lease else None

    def task_has_lease(self, task_id):
        return bool(self._lease_snapshot(task_id))

    def _set_lease(self, task_id, job_id, local_job_type, status):
        if not task_id or not job_id:
            return
        with self._lease_lock:
            lease = self._leases_by_task.get(task_id, {})
            lease.update({
                "task_id": task_id,
                "job_id": job_id,
                "local_job_type": local_job_type,
                "status": status,
                "cancel_requested": bool(status.get("cancel_requested")),
                "cancel_reason": status.get("cancel_reason") or "",
                "stop_event": lease.get("stop_event"),
                "thread": lease.get("thread"),
            })
            self._leases_by_task[task_id] = lease

    def _update_lease_status(self, task_id, status):
        if not task_id:
            return
        with self._lease_lock:
            lease = self._leases_by_task.get(task_id)
            if not lease:
                return
            lease["status"] = status
            lease["cancel_requested"] = bool(status.get("cancel_requested"))
            lease["cancel_reason"] = status.get("cancel_reason") or lease.get("cancel_reason") or ""

    def _remove_lease(self, task_id):
        with self._lease_lock:
            return self._leases_by_task.pop(task_id, None)

    def request_job(self, index_name, task_id, local_job_type, extra=None):
        if not self.job_api_active:
            return self._disabled_status()

        payload = {
            "jobType": self.job_type,
            "label": f"{self.job_label}: {index_name}",
            "source": "pdfsearch",
            "durationSec": get_env_int("NODERED_COORDINATOR_DURATION_SECONDS", 3600),
            "heartbeatTimeoutSec": self.heartbeat_timeout_seconds,
            "avoidJellyfinPlayback": self.avoid_jellyfin_playback,
            "defer": self.request_defer,
            "notes": compact_text({
                "task_id": task_id,
                "index_name": index_name,
                "pdfsearch_job_type": local_job_type,
                **(extra or {}),
            }),
        }

        try:
            raw, source = self._request_json("POST", "/coordinator/jobs/request", json=payload)
            status = self._normalize_job_api_status(raw, source=source)
            if status.get("decision") in TERMINAL_DECISIONS and not status.get("job_id"):
                raise RuntimeError(status.get("reason") or "Coordinator rejected job request")
            if status.get("job_id"):
                self._set_lease(task_id, status["job_id"], local_job_type, status)
            if status.get("decision") in RUNNING_DECISIONS:
                self._start_heartbeat(task_id)
            return status
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator job request failed: %s", e)
            return self._unavailable_status(
                self.url_for("/coordinator/jobs/request"),
                e,
            )

    def _refresh_state(self):
        try:
            self._request_json("GET", "/coordinator/state")
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator state refresh failed: %s", e)

    def poll_task_job(self, task_id):
        lease = self._lease_snapshot(task_id)
        if not lease:
            return self._disabled_status()

        job_id = lease.get("job_id")
        try:
            self._refresh_state()
            raw, source = self._request_json(
                "GET",
                f"/coordinator/jobs/{quote(str(job_id), safe='')}",
            )
            status = self._normalize_job_api_status(raw, source=source)
            self._update_lease_status(task_id, status)
            if status.get("decision") in RUNNING_DECISIONS:
                self._start_heartbeat(task_id)
            return status
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator job poll failed: %s", e)
            return self._unavailable_status(
                self.url_for(f"/coordinator/jobs/{quote(str(job_id), safe='')}"),
                e,
            )

    def heartbeat_task(self, task_id, progress=None, notes=""):
        lease = self._lease_snapshot(task_id)
        if not lease:
            return self._disabled_status()

        payload = {
            "source": "pdfsearch",
            "jobId": lease.get("job_id"),
        }
        if progress is not None:
            payload["progress"] = progress
        if notes:
            payload["notes"] = compact_text(notes, max_length=500)

        try:
            raw, source = self._request_json("POST", "/coordinator/jobs/heartbeat", json=payload)
            status = self._normalize_job_api_status(raw, source=source)
            self._update_lease_status(task_id, status)
            return status
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator heartbeat failed: %s", e)
            return self._unavailable_status(
                self.url_for("/coordinator/jobs/heartbeat"),
                e,
            )

    def _start_heartbeat(self, task_id):
        if not self.job_api_active or not task_id or self.heartbeat_seconds <= 0:
            return

        with self._lease_lock:
            lease = self._leases_by_task.get(task_id)
            if not lease:
                return
            thread = lease.get("thread")
            if thread and thread.is_alive():
                return
            stop_event = threading.Event()
            lease["stop_event"] = stop_event
            thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(task_id, stop_event),
                name=f"node-red-heartbeat-{task_id}",
                daemon=True,
            )
            lease["thread"] = thread
            thread.start()

    def _heartbeat_loop(self, task_id, stop_event):
        while not stop_event.wait(self.heartbeat_seconds):
            status = self.heartbeat_task(task_id)
            if status.get("decision") in TERMINAL_DECISIONS:
                break

    def _stop_heartbeat(self, task_id):
        lease = self._lease_snapshot(task_id)
        stop_event = lease.get("stop_event") if lease else None
        if stop_event:
            stop_event.set()

    def get_task_runtime_status(self, task_id):
        if not self.job_api_active:
            return self.get_status()
        lease = self._lease_snapshot(task_id)
        if not lease:
            return self._disabled_status()
        return self.heartbeat_task(task_id)

    def complete_task(self, task_id, result=None):
        return self._finish_task(task_id, "/coordinator/jobs/complete", result or "task finished")

    def fail_task(self, task_id, result=None):
        return self._finish_task(task_id, "/coordinator/jobs/fail", result or "task failed")

    def _finish_task(self, task_id, endpoint, result):
        if not self.job_api_active:
            return False
        lease = self._lease_snapshot(task_id)
        if not lease:
            return False

        self._stop_heartbeat(task_id)
        payload = {
            "source": "pdfsearch",
            "jobId": lease.get("job_id"),
            "result": compact_text(result, default="task finished"),
        }

        try:
            raw, _ = self._request_json("POST", endpoint, json=payload)
            if isinstance(raw, dict) and raw.get("ok") is False:
                cancel_payload = {
                    "source": "pdfsearch",
                    "jobId": lease.get("job_id"),
                    "reason": compact_text(result, default="task ended"),
                }
                self._request_json("POST", "/coordinator/jobs/cancel", json=cancel_payload)
            return True
        except Exception as e:
            if self.logger:
                self.logger.warning("Node-RED coordinator finish call failed: %s", e)
            return False
        finally:
            self._remove_lease(task_id)
