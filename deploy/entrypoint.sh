#!/bin/sh
# DIAL container entrypoint.
#
#   web     gunicorn (runs migrate + optional seed_demo first)
#   worker  celery worker
#   beat    celery beat
#   *       exec "$@" (e.g. `manage.py shell`, `sh`)
#
# Environment:
#   DATABASE_URL       PostgreSQL/SQLite URL (waited for before anything else)
#   DIAL_SEED_DEMO      1 -> run `manage.py seed_demo` after migrate (idempotent)
#   DIAL_MIGRATE        1 -> also run migrate in worker/beat (default: web only)
#   WEB_CONCURRENCY    gunicorn workers (default 3)
#   DIAL_DB_WAIT        seconds to wait for the database (default 60)
set -eu

cd /app

ROLE="${1:-web}"
DB_WAIT="${DIAL_DB_WAIT:-60}"

wait_for_db() {
    echo "[dial] waiting for database..."
    i=0
    until python manage.py check --database default >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge "$DB_WAIT" ]; then
            echo "[dial] database not reachable after ${DB_WAIT}s, giving up" >&2
            exit 1
        fi
        sleep 1
    done
    echo "[dial] database is up"
}

wait_for_migrations() {
    # worker/beat start once web has applied all migrations (avoids concurrent
    # `migrate` runs against the same PostgreSQL database).
    i=0
    until python manage.py migrate --check >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge "$DB_WAIT" ]; then
            echo "[dial] migrations still pending after ${DB_WAIT}s, starting anyway" >&2
            return 0
        fi
        [ "$i" -eq 1 ] && echo "[dial] waiting for migrations to be applied..."
        sleep 2
    done
}

migrate() {
    echo "[dial] applying migrations"
    python manage.py migrate --noinput
    if [ "${DIAL_SEED_DEMO:-0}" = "1" ]; then
        echo "[dial] seeding demo data (admin@dial.local / admin, event 'demo')"
        python manage.py seed_demo
    fi
}

case "$ROLE" in
    web)
        wait_for_db
        migrate
        echo "[dial] starting gunicorn"
        exec gunicorn dial.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "${WEB_CONCURRENCY:-3}" \
            --timeout 60 \
            --access-logfile - \
            --error-logfile -
        ;;
    worker)
        wait_for_db
        if [ "${DIAL_MIGRATE:-0}" = "1" ]; then migrate; else wait_for_migrations; fi
        echo "[dial] starting celery worker"
        exec celery -A dial worker -l info
        ;;
    beat)
        wait_for_db
        if [ "${DIAL_MIGRATE:-0}" = "1" ]; then migrate; else wait_for_migrations; fi
        echo "[dial] starting celery beat"
        exec celery -A dial beat -l info -s /tmp/celerybeat-schedule
        ;;
    *)
        exec "$@"
        ;;
esac
