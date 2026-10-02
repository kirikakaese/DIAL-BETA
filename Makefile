# DIAL - DECT & IP Administration Layer
PY      := .venv/bin/python
PIP     := .venv/bin/pip
MANAGE  := $(PY) manage.py
COMPOSE := docker compose

# Dependencies are read from pyproject.toml (flat layout with two top-level
# packages -> not pip-installable as a wheel without extra setuptools config).
DEPS_CMD = $(PY) -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(d['dependencies'] + d['optional-dependencies']['dev']))"

.PHONY: dev run worker beat test lint migrate makemigrations seed openapi \
        check up down logs shell dbshell build clean

dev:
	test -d .venv || python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(DEPS_CMD) | $(PIP) install -r /dev/stdin
	test -f .env || cp .env.example .env

run:
	$(MANAGE) runserver 0.0.0.0:8000

worker:
	.venv/bin/celery -A dial worker -l info

beat:
	.venv/bin/celery -A dial beat -l info

test:
	$(PY) -m pytest -p no:cacheprovider

lint:
	.venv/bin/ruff check apps dial

migrate:
	$(MANAGE) migrate --noinput

makemigrations:
	$(MANAGE) makemigrations

seed:
	$(MANAGE) seed_demo

openapi:
	mkdir -p docs/api
	$(MANAGE) spectacular --file docs/api/openapi.yaml

check:
	SECRET_KEY=x DATABASE_URL=sqlite:///build.sqlite3 $(MANAGE) check --settings=dial.settings.prod --deploy
	rm -f build.sqlite3

# --- Docker Compose ---------------------------------------------------------
up:
	test -f .env || cp .env.example .env
	$(COMPOSE) up --build -d

down:
	$(COMPOSE) down

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f --tail=200

shell:
	$(COMPOSE) exec web python manage.py shell

dbshell:
	$(COMPOSE) exec db psql -U dial dial

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .ruff_cache .pytest_cache staticfiles build.sqlite3
