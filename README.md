# PDF Search Engine

A self-hosted PDF library with OCR, full-text search, document previews, email ingestion, cloud synchronization, and optional Node-RED job coordination. The repository contains reusable application code. Each installation keeps its own documents, credentials, access rules, and deployment settings locally.

## Quick start

Install Docker Engine and Docker Compose v2.20 or newer. From the repository root run:

```sh
docker compose up -d --build
```

Open [http://localhost:8080](http://localhost:8080). On the first run, ClamAV must download its virus definitions and become healthy before the backend starts; this can take several minutes. Check progress with `docker compose ps` and `docker compose logs -f clamav backend`. Open **Admin**, create an index, then upload a PDF to it.

This default is for one person using the computer running Docker. Compose binds the frontend, backend, and mail ports to the host's loopback interface. With `LOCAL_SINGLE_USER_MODE=true`, the backend grants admin access to direct localhost requests received from loopback or the Docker host gateway. Keep the loopback bindings and use this mode only on a trusted host. Use shared mode for access through a proxy.

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
| `LOCAL_SINGLE_USER_MODE` | `true` in Compose | `true` allows direct localhost administration. `false` requires the shared-mode proxy login. |
| `PDFSEARCH_TRUSTED_PROXY_IP` | empty | Optional exact address of the immediate proxy peer in shared mode. When set, only this address is trusted. |
| `PDFSEARCH_ALLOWED_ORIGINS` | Localhost and 127.0.0.1 on ports 8080, 80 and 5050 | Comma-separated exact frontend origins; update when changing the frontend port or using a reverse proxy. |
| `ADMIN_TAILSCALE_USERS` | empty | Comma-separated Tailscale logins for initial administrators. |
| `MAIL_TOKEN` | empty | Set a private random value to enable mail ingestion. Compose passes it to the backend and the mail processor. |

Relative storage paths are resolved relative to `app/`, even when Compose is invoked at the repository root. The backend treats a missing access file as an empty configuration and creates `app/data/config/access_config.json` when access settings are saved. Keep this file private.

For settings that cannot be expressed as environment variables, create an ignored `compose.override.yaml` in the repository root. Compose automatically combines it with the base configuration when invoked from the repository root. For example, a coordinator token can be mounted read-only:

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

Set `LOCAL_SINGLE_USER_MODE=false`, configure `ADMIN_TAILSCALE_USERS` with actual Tailscale logins, and add the exact HTTPS frontend origin to `PDFSEARCH_ALLOWED_ORIGINS`. Use Tailscale Serve for the frontend and API ports on the same hostname. The browser connects to the configured `BACKEND_PORT`. Serve replaces incoming `Tailscale-User-Login` headers with the authenticated user's login. Keep the direct backend and mail ports private, and limit access to the proxy's backend path to trusted host processes. See [Tailscale Serve identity headers](https://tailscale.com/docs/features/tailscale-serve#identity-headers).

The backend checks the immediate connection peer before accepting the login. By default it accepts loopback or the single IPv4 default gateway detected inside the Docker container. For a different proxy layout, use `PDFSEARCH_TRUSTED_PROXY_IP` with the proxy's exact address as seen by the backend and restrict network access to that proxy. Use the Admin interface to register users and grant index permissions. An unregistered login sees an empty index list until an administrator grants access. For existing IP-based grants, follow the [migration guide](docs/migration.md).

Configure Google Drive and Dropbox return URLs on the same API endpoint the browser uses through Serve. The browser must return under the Tailscale identity that started the connection. Mail ingestion uses the private `MAIL_TOKEN` for the mail processor's uploads to existing indexes. Keep the mail processor's control port on loopback.

## Indexes, cloud, and backups

Each index can use standalone local storage, a cloud source, or a hybrid mode. The Admin and Cloud screens configure indexes, cloud connections, and synchronization. OneDrive, Google Drive, Dropbox, and mounted folders are supported; OAuth providers require an application registration and appropriate client credentials. Cloud connection tokens and settings live in the index data directory. Protect and back up that directory along with the PDFs and local configuration.

Set `BACKUP_ENABLED=true`, `BACKUP_INTERVAL_HOURS`, `BACKUP_RETENTION_COUNT`, and optionally `BACKUP_STORAGE_HOST_PATH` to enable scheduled snapshots, or use the Admin interface to run a backup. Snapshots can contain documents and credentials. Store them somewhere private and test restores independently; a backup on the same disk does not protect against disk loss.

## PDF previews

**Preview PDF** uses the browser's PDF viewer and streams the original file with HTTP byte ranges. The backend checks index access on each request and reuses the document path while the Whoosh index directory revision remains current. **Preview Page** finds the keyword in the PDF and opens the matching page. **Low Data Preview** shows grayscale page images and requests them one at a time as you scroll. The existing PDF download actions are available in either mode.

Each backend process serves up to 12 active preview and hit-page requests, including open PDF streams. Up to two requests can parse or extract PDF pages at a time, and only one can render a low-data cache miss. Busy requests return HTTP 503 with a one-second retry delay. The low-data viewer retries busy page images automatically. Reopen a native or single-page preview to retry it. Closing a preview releases the file stream when the server observes the disconnect. Page work already running on the server can finish within the same concurrency limit.

## Safe development with local customization

Install the publication guards in each development checkout:

```sh
python3 scripts/install_privacy_guards.py
```

Keep installation-specific changes in ignored environment/Compose overrides and private data directories. Commits check staged source and pushes check reachable history, including credentials and private paths. New source modules need explicit Docker input-list updates. The repository root is deliberately excluded as a Docker build context; use the existing Compose service contexts. See [private configuration and publication workflow](docs/privacy.md) for setup, feature development, local private-value checks, and publication review. Pushing source does not deploy or restart your installation.

## Development and tests

The Flask backend is in `app/backend`, the static frontend in `app/frontend`, and email processing in `app/email_processor`. The services build from Dockerfiles with pinned runtime images and dependency lock files. `app/docker-compose.yml` is the shared service definition; root `compose.yaml` includes it. `compose.override.yaml`, `.env` files, and `app/data/` are local-only. Change application code in Git, and place machine-specific settings in those local files.

To run the full test suite, including integration tests, against a disposable stack:

```sh
scripts/run_integration_tests.sh
```

The script starts the separate `pdfsearch-integration` Compose project from `app/compose.integration.yml`. It uses its own throwaway volumes and an internal network without published ports, generates fresh random tokens for the run, and removes its containers and volumes afterwards. It never mounts installation data. The installation stack in `app/docker-compose.yml` has no test bypass: the backend honors the integration-test bypass only when `PDFSEARCH_TEST_MODE=true` and a token of at least 32 characters are both set, which only the isolated stack does. For unit tests only, install the test dependencies and run `pytest -m "not integration" app/tests` with `app/backend` on `PYTHONPATH`.

There is no active automatic deployment workflow. A Git push does not rebuild or restart services. Files bind-mounted into containers, including frontend assets, can take effect as soon as they change; develop and test features in a separate source-only checkout. For a planned update, review local settings, pull the desired version, and deliberately rebuild with `docker compose up -d --build`. See [migration guidance](docs/migration.md) when upgrading a previously customized installation.

## License

[MIT License](LICENSE).
