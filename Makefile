-include .env
export

.PHONY: dev validate test lint fmt fmt-check check schedule

dev:            ## Run the app locally (data/ served at :8000)
	uv run motorcal serve --config data --state state/state.yaml

validate:       ## Validate data/ without starting the server
	uv run motorcal validate-config --config data

test:           ## Run the test suite
	uv run pytest

lint:           ## Lint
	uvx ruff check

fmt:            ## Auto-format
	uvx ruff format

fmt-check:      ## Check formatting without changing files
	uvx ruff format --check

check: lint fmt-check test validate  ## Everything CI runs
	uv run pre-commit run --all-files

schedule:       ## Print the local feed's events (URL=... to override)
	uv run scripts/view_ics.py $(or $(URL),http://localhost:8000/events.ics)
