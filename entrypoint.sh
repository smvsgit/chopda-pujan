#!/bin/sh
set -eu

echo "=== SMVS Chopda-Pujan ==="

# Wait for the approved external PostgreSQL service. This performs no schema
# change and makes transient DB/network starts fail clearly.
python - <<'PY'
import os, sys, time
from sqlalchemy import create_engine, text
url = os.environ.get('DATABASE_URL')
if not url:
    print('DATABASE_URL is not set'); sys.exit(1)
for attempt in range(1, 31):
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        print('Database reachable')
        break
    except Exception as e:
        print(f'  waiting for database ({attempt}/30): {e.__class__.__name__}')
        time.sleep(2)
else:
    print('Database unreachable after 60s'); sys.exit(1)
PY

# Schema changes are explicit. For a brand-new, empty dedicated database set
# DB_SCHEMA_MODE=bootstrap for the first controlled deployment only. After it
# succeeds, set DB_SCHEMA_MODE=none. Do not use bootstrap on an existing DB
# without reviewing its migration state first.
case "${DB_SCHEMA_MODE:-none}" in
  bootstrap)
    echo "Applying Alembic schema to NEW EMPTY database..."
    flask --app app db upgrade
    echo "Running initial seed..."
    python -c "from app import init_db; init_db()"
    ;;
  none)
    echo "Schema mutation disabled (DB_SCHEMA_MODE=none)"
    ;;
  *)
    echo "Invalid DB_SCHEMA_MODE: ${DB_SCHEMA_MODE}" >&2
    exit 2
    ;;
esac

RELOAD=""
if [ "${GUNICORN_RELOAD:-0}" = "1" ]; then
    RELOAD="--reload"
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
