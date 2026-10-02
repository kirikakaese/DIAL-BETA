# PET - Portable Event Telephone
# Single image for web (gunicorn), celery worker and celery beat; the role is
# selected by the entrypoint (deploy/entrypoint.sh): web | worker | beat | <cmd>.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=pet.settings.prod

# libpq5: runtime lib for psycopg (binary wheels ship their own libpq, but keep
# it for the pg_isready-free entrypoint and tooling).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install runtime dependencies straight from pyproject.toml (layer-cached).
# `pip install .` is intentionally avoided: the repo is a flat layout with two
# top-level packages (apps/, pet/) and no [tool.setuptools] config, so a wheel
# build would fail. The app runs from /app via manage.py anyway.
COPY pyproject.toml ./
RUN python -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(d['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY . /app

# Build-time Django steps need a settings module that can import; the values
# are throwaway (the sqlite file is deleted afterwards).
RUN SECRET_KEY=build DATABASE_URL=sqlite:///build.sqlite3 \
    python manage.py collectstatic --noinput \
    && rm -f build.sqlite3

# Non-root runtime user. /var/spool/asterisk/voicemail is the path the Asterisk
# container writes voicemail files to (shared volume; apps.voicemail imports
# messages by that absolute file_path).
RUN groupadd --system pet && useradd --system --gid pet --home-dir /app --shell /usr/sbin/nologin pet \
    && mkdir -p /app/media /var/spool/asterisk/voicemail \
    && chmod +x /app/deploy/entrypoint.sh \
    && chown -R pet:pet /app /var/spool/asterisk/voicemail

USER pet

EXPOSE 8000

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["web"]
