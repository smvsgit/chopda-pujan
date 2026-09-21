#!/bin/sh
# Container entrypoint. This file is what the Dockerfile has always pointed at;
# it was missing from the project, so the image failed to build.
set -e

echo "=== SMVS Chopda-Pujan ==="

# Wait for Postgres. depends_on/service_healthy usually covers this, but a
# restart during a database upgrade can still race.
python - <<'PY'
import os, sys, time
from sqlalchemy import create_engine, text
url = os.environ.get('DATABASE_URL')
if not url:
    print("DATABASE_URL is not set"); sys.exit(1)
for attempt in range(1, 31):
    try:
        create_engine(url).connect().execute(text('SELECT 1'))
        print("Database reachable")
        break
    except Exception as e:
        print(f"  waiting for database ({attempt}/30): {e.__class__.__name__}")
        time.sleep(2)
else:
    print("Database unreachable after 60s"); sys.exit(1)
PY

echo "Running schema init / seed..."
python -c "from app import init_db; init_db()"

# GUNICORN_RELOAD=1 makes gunicorn watch the source and restart its workers
# when a .py file changes. Set by docker-compose.override.yml in development,
# absent in production.
RELOAD=""
if [ "${GUNICORN_RELOAD:-0}" = "1" ]; then
    RELOAD="--reload"
    echo "Live reload is ON - edit a .py file and gunicorn restarts itself."
fi

echo "Starting Gunicorn on :3000"
exec gunicorn \
    --bind 0.0.0.0:3000 \
    --workers "${WEB_CONCURRENCY:-3}" \
    --threads 2 \
    --timeout 60 \
    ${RELOAD} \
    --access-logfile - \
    --error-logfile - \
    app:app
