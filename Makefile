PY := .venv/bin/python
ENGINE := packages/engine

.PHONY: help venv demo test verify validate web bundle bench eval eval-live clean

help:
	@echo "make demo      - the walkthrough, in the terminal"
	@echo "make test      - unit + property-based tests"
	@echo "make validate  - regenerate docs/VALIDATION.md from the engine"
	@echo "make verify    - test + validate + determinism (what CI runs)"
	@echo "make web       - re-export the scenario bundle and rebuild the viewer"
	@echo "make eval      - extraction eval, offline from cassettes"
	@echo "make eval-live - re-run the eval against the API and re-record"
	@echo "make bench     - simulation throughput"

venv:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e "$(ENGINE)[dev]" || \
	  $(PY) -m pip install -q pydantic numpy pyyaml pytest hypothesis

demo:
	@$(PY) scripts/demo.py

test:
	@$(PY) -m pytest tests/ -q

validate:
	@$(PY) scripts/validate.py

bundle:
	@$(PY) scripts/export_bundle.py

web: bundle
	@$(PY) scripts/build_web.py
	@echo "open packages/web/index.html"

eval:
	@PYTHONPATH=packages/pipeline:packages/engine \
	  $(PY) packages/pipeline/taxpipeline/evals/run.py

eval-live:
	@PYTHONPATH=packages/pipeline:packages/engine \
	  $(PY) packages/pipeline/taxpipeline/evals/run.py --live --record
	@PYTHONPATH=packages/pipeline:packages/engine \
	  $(PY) scripts/report_evals.py

verify: test validate
	@echo ""
	@echo "verified: tests pass, published tables reproduced, ledger hash stable"

bench:
	@$(PY) scripts/bench.py

clean:
	rm -rf .pytest_cache **/__pycache__ .hypothesis
