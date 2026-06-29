# music-sync — common tasks. Run `make` or `make help` to list targets.
# Python via uv; the web SPA lives in web/ (Vite + TypeScript).

DB ?= library.db
EXPORT ?= biblioteca.csv

.DEFAULT_GOAL := help

.PHONY: help install sync dry-run apply export offline \
        dev backend frontend build-ui \
        test lint typecheck check gate seed smoke clean

help: ## Show this help
	@echo "music-sync — make targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Vars: DB=$(DB)  EXPORT=$(EXPORT)  (override e.g. 'make sync DB=/tmp/x.db')"

# --- setup ---------------------------------------------------------------
install: ## Install Python + web dependencies
	uv sync
	cd web && npm install

# --- CLI sync ------------------------------------------------------------
sync: ## Safe reconcile: read all platforms, update DB, write review files (no remote writes)
	uv run python sync_music.py --db $(DB)

dry-run: ## Show the diff only; write nothing anywhere
	uv run python sync_music.py --dry-run --db $(DB)

apply: ## Actually push likes to every platform (Spotify + Apple + Tidal)
	uv run python sync_music.py --apply-spotify --apply-apple --apply-tidal --db $(DB)

export: ## Export the unified library to CSV (no network); override with EXPORT=path.csv
	uv run python sync_music.py --offline --export $(EXPORT) --db $(DB)

offline: ## Offline dry-run reconcile from the DB only (no network)
	uv run python sync_music.py --offline --dry-run --db $(DB)

# --- web app -------------------------------------------------------------
dev: ## Run backend + frontend together; Ctrl+C stops both; opens the browser
	@echo "Starting backend (:8000) + frontend (:5173) — Ctrl+C stops both..."
	@uv run python -m musicsync.web & \
	back_pid=$$! ; \
	trap 'kill $$back_pid 2>/dev/null' EXIT INT TERM ; \
	( sleep 2 && (open http://localhost:5173 2>/dev/null || true) ) & \
	cd web && npm run dev

backend: ## Run only the FastAPI backend on 127.0.0.1:8000
	uv run python -m musicsync.web

frontend: ## Run the Vite dev server on http://localhost:5173 (proxies /api -> :8000)
	cd web && npm run dev

build-ui: ## Production build of the SPA (tsc + vite build)
	cd web && npm run build

# --- quality gates -------------------------------------------------------
test: ## Run the test suite
	uv run pytest -q

lint: ## Lint with ruff
	uv run ruff check .

typecheck: ## Type-check with mypy
	uv run mypy .

check: gate ## Alias for `gate`
gate: ## Full pre-PR gate: ruff + mypy + pytest (mirrors CI)
	uv run ruff check . && uv run mypy . && uv run pytest -q

# --- smoke / fixtures ----------------------------------------------------
seed: ## Seed a throwaway SQLite library for smoke testing (idempotent)
	uv run python -m musicsync.seed --db .metate/smoke.db

smoke: seed ## Offline behavior proof: seed, offline dry-run, prove CSV export
	uv run python sync_music.py --offline --dry-run --db .metate/smoke.db
	uv run python sync_music.py --offline --export /tmp/smoke_export.csv --db .metate/smoke.db
	@test -s /tmp/smoke_export.csv && echo "smoke OK: CSV export produced"

# --- housekeeping --------------------------------------------------------
clean: ## Remove caches and the SPA build (keeps library.db, .env, token caches)
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache web/dist
	@echo "cleaned (library.db, .env, .cache, .tidal-cache left untouched)"
