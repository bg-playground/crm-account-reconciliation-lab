#!/usr/bin/env bash
# Local equivalent of the CI workflow: boundary check, offline unit tests, gitleaks (if installed).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
echo "== boundary check"
"$PY" scripts/boundary_check.py
echo "== unit tests (offline; API key unset)"
OPENAI_API_KEY="" "$PY" -m pytest -q
echo "== published evidence verification (offline)"
OPENAI_API_KEY="" "$PY" -m recon_lab.cli verify
echo "== gitleaks"
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks git --no-banner --redact .
else
  echo "gitleaks not installed; skipped"
fi
