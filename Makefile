# BizAgent Workbench — developer entry points. Everything here runs on CPU with the mock LLM.
SHELL := /bin/bash
export PYTHONPYCACHEPREFIX := $(CURDIR)/.cache/pycache
UV ?= uv
RUN := $(UV) run

.PHONY: setup doctor lint check-links test test-unit test-integration check-numbers results demo-mock data train-lock clean

setup:  ## Create the app env (root) and lock the train env (no GPU install)
	git submodule update --init third_party/agent-world-model third_party/AgentFly
	$(UV) sync --frozen
	$(RUN) python tests/fixtures/awm_mini/build_fixture.py
	cd train && $(UV) lock --check

doctor:
	$(RUN) workbench doctor

lint: check-links
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests
	$(RUN) mypy --strict src

check-links:  ## Relative links and images in README.md and docs/**/*.md must resolve
	$(RUN) python scripts/check_links.py

test:  ## Unit + integration tests, mock LLM only
	$(RUN) pytest -q

test-unit:
	$(RUN) pytest -q tests/unit

test-integration:
	$(RUN) pytest -q tests/integration

check-numbers:
	$(RUN) workbench results check

results:  ## Regenerate docs/RESULTS.md from results/registry.yaml
	$(RUN) workbench results render

demo-mock:  ## Start API + gateway + mini env with the mock LLM; open http://127.0.0.1:8080/ui/
	$(RUN) workbench api serve --demo

data:
	./scripts/download_data.sh data/awm1k

train-lock:
	cd train && $(UV) lock

clean:
	rm -rf .cache .pytest_cache .mypy_cache .ruff_cache
