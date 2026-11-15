# PDF Search Engine

A self-hosted PDF library with OCR, full-text search, document previews, email ingestion, cloud synchronization, and optional Node-RED job coordination. The repository contains reusable application code. Each installation keeps its own documents, credentials, access rules, and deployment settings locally.

## Quick start

Install Docker Engine and Docker Compose v2.20 or newer. From the repository root run:

```sh
docker compose up -d --build
```

Open [http://localhost:8080](http://localhost:8080). On the first run, ClamAV must download its virus definitions and become healthy before the backend starts; this can take several minutes. Check progress with `docker compose ps` and `docker compose logs -f clamav backend`. Open **Admin**, create an index, then upload a PDF to it.

This default is for one person using the computer running Docker. The frontend, backend and mail ports bind to the host's loopback interface. The backend grants local admin access to direct requests from the Docker host gateway when `LOCAL_SINGLE_USER_MODE=true`. It is not a public login system: anyone with sufficient access to that host or its Docker environment may be able to act as administrator. Do not expose these ports to an untrusted network or use a reverse proxy without configuring authentication and disabling local single-user mode.

Your data is stored under `app/data/`, excluded from Git. It contains PDF and cloud mirrors, indexes, settings, logs, mail task state, and backups; back it up separately. Removing containers does not delete these host directories. Some of this data, including mail and cloud credentials, can be sensitive.

## Private configuration

The application works without a configuration file. To customize an installation, copy `app/.env.example` to `app/.env` and edit it. Docker Compose reads this file when using the root Compose file or the `app` Compose file. Both `.env` and `app/data/` are ignored by Git; never force-add them. Environment variables exported in your shell override values in the file. Review `git status` and staged changes before committing.

Common options:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FRONTEND_PORT` | `8080` | Host port for the browser interface. |
| `BACKEND_PORT` | `5050` | Host port used by the browser for the API. |
| `MAIL_PROCESSOR_PORT` | `5000` | Local host port for the email processor. |
| `PDF_STORAGE_HOST_PATH` | `./data/ocr` | Converted and deleted PDFs. |
| `INDEX_STORAGE_HOST_PATH` | `./data/index` | Search indexes and cloud connection state. |
| `CONFIG_HOST_PATH` | `./data/config` | Local access and legacy role configuration. |
| `TMP_STORAGE_HOST_PATH`, `LOG_STORAGE_HOST_PATH` | `./data/tmp`, `./data/logs` | Temporary files and logs. |
| `CLOUD_STORAGE_HOST_PATH`, `EMAIL_CONFIG_HOST_PATH` | `./data/cloud`, `./data/email_configs` | Cloud mirrors and saved email configurations. |
| `MAIL_STATE_HOST_PATH`, `BACKUP_STORAGE_HOST_PATH` | `./data/mail_state`, `./data/backups` | Mail task state and backup snapshots. |
| `LOCAL_SINGLE_USER_MODE` | `true` | Allow direct requests from the Docker host gateway to act as a local admin. Set to `false` for authenticated multi-user access. |
| `PDFSEARCH_ALLOWED_ORIGINS` | Localhost and 127.0.0.1 on ports 8080, 80 and 5050 | Comma-separated exact frontend origins; update when changing the frontend port or using a reverse proxy. |
| `ADMIN_TAILSCALE_USERS` | empty | Comma-separated Tailscale login emails for initial administrators. |
| `MAIL_TOKEN` | `local-mail-token` | Shared internal mail-ingestion token. Set a private random value for an installation; both services receive the same value. |
| `TEST_BYPASS_TOKEN` | empty | Optional test-only admin bypass. Leave empty outside an isolated test environment. |

Relative storage paths are resolved relative to `app/`, even when Compose is invoked at the repository root. The backend automatically treats a missing access file as an empty configuration; `app/data/config/access_config.json` and `roles.json` are created when settings are saved. `role_indices.json` in the same directory optionally maps legacy IP role names to lists of index names. These files belong to the installation, not the source repository.

For settings that cannot be expressed as environment variables, create an ignored `app/docker-compose.override.yml`. Compose automatically combines it with the base configuration. For example, a coordinator token can be mounted read-only:

```yaml
services:
  backend:
    environment:
      NODERED_COORDINATOR_TOKEN_FILE: /run/secrets/coordinator-token
    volumes:
      - /absolute/path/to/private-token:/run/secrets/coordinator-token:ro
```

The file must contain a single nonempty token without whitespace. Set `NODERED_COORDINATOR_URL` in `app/.env` to enable coordination. Restart the affected services with `docker compose up -d` after configuration changes. Never commit the override, token, local `.env`, or data directory.

## Networked and multi-user installations

Tailscale Serve can proxy the loopback frontend and backend and provide a `Tailscale-User-Login` identity. Set `LOCAL_SINGLE_USER_MODE=false`, configure `ADMIN_TAILSCALE_USERS`, and add the exact HTTPS frontend origin to `PDFSEARCH_ALLOWED_ORIGINS`. Expose both the frontend and API ports through the trusted proxy; the browser uses the configured `BACKEND_PORT` on the same hostname. Keep direct backend access restricted to the trusted proxy and ensure it strips untrusted identity and forwarding headers before setting its own. A request with a forged identity header can otherwise impersonate a user. `roles.json` is a legacy IP-based fallback and must not grant broad network ranges administrator access. Use the Admin interface to manage users, per-index permissions, and indexes.

The origin policy rejects requests carrying an unapproved browser `Origin` and checks cross-site browser writes. It does not authenticate non-browser clients. Never use the default internal mail token or a test bypass token as an Internet-facing security boundary.

## Indexes, cloud, and backups

Each index can use standalone local storage, a cloud source, or a hybrid mode. The Admin and Cloud screens configure indexes, cloud connections, and synchronization. OneDrive, Google Drive, Dropbox, and mounted folders are supported; OAuth providers require an application registration and appropriate client credentials. Cloud connection tokens and settings live in the index data directory. Protect and back up that directory along with the PDFs and local configuration.

Set `BACKUP_ENABLED=true`, `BACKUP_INTERVAL_HOURS`, `BACKUP_RETENTION_COUNT`, and optionally `BACKUP_STORAGE_HOST_PATH` to enable scheduled snapshots, or use the Admin interface to run a backup. Snapshots can contain documents and credentials. Store them somewhere private and test restores independently; a backup on the same disk does not protect against disk loss.

## Development and tests

The Flask backend is in `app/backend`, the static frontend in `app/frontend`, and email processing in `app/email_processor`. The services build from Dockerfiles with pinned runtime images and dependency lock files. `app/docker-compose.yml` is the shared service definition; root `compose.yaml` includes it. `app/docker-compose.override.yml`, `.env` files, and `app/data/` are local-only. Change application code in Git, and place machine-specific settings in those local files.

To run the test suite against a disposable stack, set a unique `TEST_BYPASS_TOKEN` in an isolated environment and run:

```sh
TEST_BYPASS_TOKEN=choose-a-test-only-token docker compose --profile test run --rm test-runner
```

Integration tests create and delete test data. Do not run them against a production installation. The test service is in the `test` profile, so ordinary `docker compose up` does not run it. For unit tests only, install the test dependencies and run `pytest -m "not integration" app/tests` with `app/backend` on `PYTHONPATH`.

There is no active automatic deployment workflow. Updating Git does not update a running installation: review local settings, pull the desired version, and deliberately rebuild with `docker compose up -d --build`. See [migration guidance](docs/migration.md) when upgrading a previously customized installation.

## License

[MIT License](LICENSE).
