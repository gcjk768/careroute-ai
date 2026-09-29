#!/usr/bin/env bash
# Start the CareRoute backend + frontend together for local development, both
# with hot-reload. Ctrl+C stops both. For a production-like run (or to include
# Prometheus/Grafana) use `docker compose up --build` instead.
#
#   ./dev.sh
#
#   Backend  -> http://localhost:8000   FastAPI (/api/*, /metrics), auto-reload
#   Frontend -> http://localhost:5173   Next.js dev, proxies /api to the backend
#
# The frontend's /api proxy defaults to http://localhost:8000 (next.config.mjs),
# so no extra wiring is needed. Backend Python deps must be installed first
# (pip install -r backend/requirements.txt [-r backend/requirements-dev.txt]);
# a backend/.venv is auto-detected if you keep one.
set -euo pipefail
cd "$(dirname "$0")"

# Stop both child processes when this script exits (Ctrl+C, error, kill).
trap 'kill 0' EXIT

# --- Backend: uvicorn with --reload -------------------------------------------
(
  cd backend
  if   [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe"   # Windows venv
  elif [ -x ".venv/bin/python" ];        then PY=".venv/bin/python"            # POSIX venv
  else                                        PY="python"                       # system
  fi
  echo "[backend]  $PY -m uvicorn app.main:app --reload --port 8000"
  exec "$PY" -m uvicorn app.main:app --reload --port 8000
) &

# --- Frontend: next dev (installs deps on first run) --------------------------
(
  cd frontend
  [ -d node_modules ] || { echo "[frontend] installing deps..."; npm install; }
  echo "[frontend] npm run dev  (http://localhost:5173)"
  exec npm run dev
) &

wait
