.PHONY: install test lint fmt-check type fmt all ci clean

install:
	pip install -e ".[dev]"

test:
	pytest -v

lint:
	ruff check src tests

fmt-check:
	ruff format --check src tests

type:
	mypy src

fmt:
	ruff format src tests
	ruff check --fix src tests

# Dev convenience: auto-fix formatting/lint, then verify.
all: fmt lint type test

# Mirrors the GitHub CI gate (read-only — no auto-fix). Run before pushing.
# lint / fmt-check / test block, exactly as CI does; mypy runs but does not block
# (CI's type-check step is continue-on-error until types are enforced at v0.8).
ci: lint fmt-check test
	-mypy src

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
