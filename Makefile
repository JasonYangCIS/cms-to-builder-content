.PHONY: test lint typecheck check dev-setup

# Uses the project-local .venv when it exists (created by `make dev-setup`), else the system python3.
PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# Dependency-free (standard library only). Integration tests that need exports/ (a local Contentful
# export, git-ignored) are skipped automatically when it is absent.
test:
	python3 -m unittest discover -s scripts/transform
	python3 -m unittest scripts/test_run_migration.py scripts/test_run_verification.py scripts/test_stage7.py scripts/test_report_html.py \
		scripts/test_manifest_bulk.py scripts/test_guard.py scripts/test_seed_contentful.py scripts/test_export_contentful.py scripts/test_migrate_assets.py scripts/test_docs.py scripts/test_propose_mapping.py
	python3 scripts/guard.py --selftest > /dev/null
	@echo "all tests passed"

# Developer checks (need `make dev-setup`; configuration lives in pyproject.toml).
lint:
	@$(PYTHON) -m ruff --version >/dev/null 2>&1 || { echo "ruff is not installed: run 'make dev-setup'"; exit 1; }
	$(PYTHON) -m ruff check scripts

typecheck:
	@$(PYTHON) -m mypy --version >/dev/null 2>&1 || { echo "mypy is not installed: run 'make dev-setup'"; exit 1; }
	$(PYTHON) -m mypy

# The full gate: tests + lint + type check.
check: test lint typecheck
	@echo "all checks passed"

dev-setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt
