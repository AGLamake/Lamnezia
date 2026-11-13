.PHONY: install dev services css css-watch messages compilemessages messages-check migrate test coverage lint format

install:
	uv sync

css:
	uv run python scripts/tailwind.py

css-watch:
	uv run python scripts/tailwind.py --watch

dev: migrate css compilemessages
	uv run uvicorn config.asgi:application --reload

services:
	docker compose -f compose.dev.yaml up -d

# Interface strings (GNU gettext required). `messages` refreshes the catalogs after code changes.
messages:
	uv run python manage.py makemessages -l ru -e html,py,txt --no-location --no-obsolete --ignore=staticfiles

compilemessages:
	uv run python manage.py compilemessages --ignore=.venv

messages-check: messages
	uv run python scripts/check_messages.py

migrate:
	uv run python manage.py migrate

test:
	uv run pytest

coverage:
	uv run pytest amnezia_core --cov

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy .

format:
	uv run ruff check --fix .
	uv run ruff format .
