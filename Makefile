.PHONY: help install dev up down logs test test-unit test-contract test-int lint fmt typecheck boundaries routes worker check migrate revision clean web-install web-dev web-test web-build web-api

SHELL := /bin/bash

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install dependencies into a local venv
	uv sync --all-extras --dev

dev:  ## Run the API locally with hot reload (CAIRN_ROLE=all)
	CAIRN_ROLE=all uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000

up:  ## Start the full stack
	docker compose up -d

down:  ## Stop the stack (volumes preserved)
	docker compose down

logs:  ## Follow the bootstrap/migrate logs (the admin password is printed here)
	docker compose logs -f migrate api-control

migrate:  ## Apply migrations locally
	uv run alembic upgrade head

revision:  ## Create a migration:  make revision M="add widget table"
	uv run alembic revision --autogenerate -m "$(M)"

# --- quality gates -----------------------------------------------------------

lint:  ## ruff check
	uv run ruff check src apps tests
	uv run ruff format --check src apps tests

fmt:  ## ruff format (writes)
	uv run ruff format src apps tests
	uv run ruff check --fix src apps tests

typecheck:  ## mypy --strict
	uv run mypy

boundaries:  ## import-linter (NFR-M-01, NFR-M-02)
	uv run lint-imports --config .importlinter

routes:  ## Every route declares an authorization dependency (T-OPS-08)
	uv run python scripts/check_route_authz.py

worker:  ## Run a worker locally:  make worker Q=parse
	CAIRN_ROLE=worker CAIRN_TASKS__QUEUE=$(Q) uv run python -m apps.worker.main

test-unit:  ## Unit + contract tests (no Docker required)
	uv run pytest tests/unit tests/contract

test-contract:  ## Driver conformance across every implementation
	uv run pytest tests/contract -v

test-int:  ## Integration tests (requires Docker)
	uv run pytest tests/integration

test:  ## Full suite with coverage gate
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=80

# --- frontend ----------------------------------------------------------------

web-install:  ## Install web dependencies
	cd apps/web && npm ci --no-audit --no-fund

web-dev:  ## Vite dev server (proxies /v1 to localhost:8000)
	cd apps/web && npm run dev

web-api:  ## Regenerate the API client from the running spec
	CAIRN_DATABASE_URL=postgresql+asyncpg://c:c@localhost:5432/c 	CAIRN_REDIS_URL=redis://localhost:6379/0 	CAIRN_MASTER_KEY=dGVzdC1tYXN0ZXIta2V5LWZvci1jaS1vbmx5LTMyYnl0ZXM= 	uv run python -c "import json;from apps.api.main import create_app;	open('apps/web/openapi.json','w').write(json.dumps(create_app().openapi(), indent=2))"
	cd apps/web && npm run api:generate

web-test:  ## Web typecheck + unit tests
	cd apps/web && npx vue-tsc --noEmit && npx vitest run

web-build:  ## Production build
	cd apps/web && npm run build

check: lint typecheck boundaries routes test web-test  ## Everything CI runs

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
