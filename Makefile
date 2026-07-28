.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Setup ----------------------------------------------------------------

.PHONY: setup
setup: guard-env $(VENV) migrate ## Create venv, install deps, run migrations
	@echo "Setup complete. 'make dev' to start."

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev,quant]"

.PHONY: guard-env
guard-env: ## Refuse to proceed if .env enables broker execution
	@if [ -f .env ] && grep -qE '^MERIDIAN_BROKER_EXECUTION_ENABLED=true' .env; then \
		echo "REFUSING: .env sets MERIDIAN_BROKER_EXECUTION_ENABLED=true."; \
		echo "No broker adapter exists in this build (docs/architecture.md §9)."; \
		exit 1; \
	fi
	@if [ -f .env ] && grep -qE '^MERIDIAN_MODE=broker' .env; then \
		echo "REFUSING: .env sets MERIDIAN_MODE=broker, which is not implemented."; \
		exit 1; \
	fi

.PHONY: web-install
web-install: ## Install frontend dependencies
	cd apps/web && npm install

# --- Database -------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply migrations to head
	$(VENV)/bin/alembic upgrade head

.PHONY: migrate-down
migrate-down: ## Roll back one migration
	$(VENV)/bin/alembic downgrade -1

.PHONY: migration
migration: ## Autogenerate a migration: make migration M="description"
	$(VENV)/bin/alembic revision --autogenerate -m "$(M)"

.PHONY: reset-db
reset-db: ## Delete the local database and rebuild it
	rm -f var/meridian.db var/meridian.db-wal var/meridian.db-shm
	$(MAKE) migrate

# --- Run ------------------------------------------------------------------

.PHONY: dev
dev: ## Start the API with reload
	$(VENV)/bin/uvicorn meridian_api.app:app --reload --host 127.0.0.1 --port 8787

.PHONY: web
web: ## Start the frontend
	cd apps/web && npm run dev

# --- Test and check -------------------------------------------------------

.PHONY: test
test: ## Run the full suite
	$(PY) -m pytest tests/ -q

.PHONY: test-risk
test-risk: ## Risk-engine tests only — run before any risk change
	$(PY) -m pytest tests/ -q -m risk

.PHONY: test-determinism
test-determinism: ## Replay-determinism tests
	$(PY) -m pytest tests/ -q -m determinism

.PHONY: test-postgres
test-postgres: ## Run the suite against Postgres (needs MERIDIAN_TEST_POSTGRES_URL)
	@if [ -z "$$MERIDIAN_TEST_POSTGRES_URL" ]; then \
		echo "MERIDIAN_TEST_POSTGRES_URL is unset — skipping."; exit 1; fi
	$(PY) -m pytest tests/ -q

# Every source root, and the reason the list is spelled out: `mypy packages apps`
# silently excluded services/ — the risk engine, paper broker, backtest engine,
# market data and feature pipeline went unchecked. tests/meta/test_type_coverage.py
# fails if a source root or package is ever added without landing here.
SOURCE_ROOTS := packages services apps

.PHONY: lint
lint: ## ruff + mypy + import boundaries
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy $(SOURCE_ROOTS)

.PHONY: format
format: ## Apply formatting
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

.PHONY: boundaries
boundaries: ## Verify architectural import boundaries (ADR-0004)
	$(VENV)/bin/lint-imports

.PHONY: audit-verify
audit-verify: ## Verify the audit hash chain
	$(PY) -m meridian_db.verify

# Redaction tests must contain secret-shaped strings in order to prove the
# redactor removes them, so those files are excluded by exact path. The
# exclusion is deliberately per-file, never a blanket tests/ exemption — a real
# credential committed to any other test would still fail this scan.
SECRET_SCAN_EXCLUDES := ':!tests/config/test_redaction.py' ':!tests/db/test_audit_chain.py' \
                       ':!tests/router/test_provider.py'

.PHONY: secret-scan
secret-scan: ## Scan tracked content for credential patterns
	@if git grep -nEI --cached \
		-e 'sk-[A-Za-z0-9]{16,}' -e 'AKIA[0-9A-Z]{16}' \
		-e '-----BEGIN [A-Z ]*PRIVATE KEY-----' -e 'ghp_[A-Za-z0-9]{30,}' \
		-- . $(SECRET_SCAN_EXCLUDES); then \
		echo "SECRET DETECTED — do not commit."; exit 1; \
	else echo "secret scan: clean"; fi

.PHONY: check
check: lint test secret-scan ## The pre-commit gate

.PHONY: clean
clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
