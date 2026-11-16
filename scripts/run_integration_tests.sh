#!/bin/sh
# Run the integration suite against a disposable, isolated stack. Fresh random
# tokens exist only in this process environment and are never printed or stored.
set -eu
cd "$(dirname "$0")/../app"
INTEGRATION_BYPASS_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
INTEGRATION_MAIL_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
export INTEGRATION_BYPASS_TOKEN INTEGRATION_MAIL_TOKEN
compose="docker compose --project-name pdfsearch-integration --file compose.integration.yml"
cleanup() { $compose down --volumes --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
status=0
$compose up --build --abort-on-container-exit --exit-code-from test-runner test-runner || status=$?
exit "$status"
