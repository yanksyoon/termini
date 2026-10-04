# Termini Controller — make targets (stdlib-only, no venv needed)
SHELL := /bin/bash
PY := python3

.PHONY: run verify verify-operator migrate clean

run: ## Run the controller (requires TERMINI_BOOTSTRAP_TOKEN)
	$(PY) -m app.main

verify: ## Run the end-to-end acceptance suite
	$(PY) scripts/acceptance.py

verify-operator: ## Run the operator loop vs the chaos harness (oom/disk/latency/healthy)
	$(PY) scripts/acceptance_operator.py

migrate: ## Apply pending DB migrations (also applied automatically at startup)
	$(PY) -m app.migrate

clean: ## Remove runtime artifacts (DB, bytecode)
	rm -rf data __pycache__ app/__pycache__ app/*/__pycache__ .pytest_cache
