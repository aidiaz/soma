.PHONY: install test lint fmt typecheck contract reach smoke ci

install:
	uv sync --all-groups

test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

fmt:
	uv run ruff format src tests scripts

typecheck:
	uv run basedpyright src

# Confirms the garminconnect methods the sync worker calls still exist. The sync
# worker degrades quietly by design, so upstream drift is invisible without this.
contract:
	uv run python scripts/check_api_contract.py

# Probes every third-party endpoint without credentials. Run before a deploy, and
# from a new network before blaming the code.
reach:
	uv run python scripts/check_reachability.py

# Drives the app over real HTTP against a throwaway database, asserts every
# tool's response, and fails if the process logged anything unexpected.
# `make test` cannot catch lifespan, mount-order or middleware faults; this can.
smoke:
	uv run python scripts/smoke.py

ci: lint test contract smoke
