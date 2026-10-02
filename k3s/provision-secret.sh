#!/usr/bin/env bash
# Create or update the cycle-runner-adk Secret from this checkout's .env.
#
#   bash k3s/provision-secret.sh
#
# .env holds literals and op://agents/... references; `op run` resolves them
# with the agent service account (no desktop prompt). Values go from the
# environment to `kubectl apply` over stdin only: never into argv, a file or
# this script's output.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -r .env ] || { echo "no .env in $(pwd)" >&2; exit 1; }

op run --env-file .env -- python3 - <<'PY' | kubectl apply -f -
import base64, json, os, sys

KEYS = ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS")
missing = [k for k in KEYS if not os.environ.get(k, "").strip() or os.environ[k].startswith("op://")]
if missing:
    sys.exit(f"unresolved or empty in .env: {', '.join(missing)}")
json.dump({
    "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
    "metadata": {"name": "cycle-runner-adk", "namespace": "cycle-runner-adk"},
    "data": {k: base64.b64encode(os.environ[k].strip().encode()).decode() for k in KEYS},
}, sys.stdout)
PY
