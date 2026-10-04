# Makefile — repo-level targets.
#
# The controller has its own controller/Makefile; this one hosts cross-cutting
# entry points. `make chaos SCENARIO=<name>` is the documented fault-injection
# entry point for the control plane (see chaos/README.md).
SHELL := /bin/bash
PY := python3
SCENARIO ?= oom

.PHONY: chaos chaos-verify chaos-test

chaos: ## Run one fault scenario end to end (SCENARIO=oom|disk-degrade|latency|healthy)
	$(PY) chaos/run.py --scenario $(SCENARIO)

chaos-verify: ## Run the full chaos acceptance suite (all scenarios)
	$(PY) chaos/verify.py

chaos-test: ## Run the chaos-harness unit tests
	$(PY) -m unittest discover -s chaos/tests -t . -v
