# wallet-service — developer tasks. Requires a Python 3.12 venv at .venv.
PY := .venv/bin/python
# On Windows the venv interpreter lives under Scripts/
ifeq ($(OS),Windows_NT)
	PY := .venv/Scripts/python
endif

.PHONY: help install test security check sbom docker-build

help:
	@echo "install       create/refresh deps (dev+ci)"
	@echo "test          run the test suite"
	@echo "security      run all security gates (local)"
	@echo "check         test + security (what CI runs, strict)"
	@echo "sbom          generate CycloneDX SBOM"
	@echo "docker-build  build the hardened image"

install:
	$(PY) -m pip install -e ".[dev,ci]"

test:
	$(PY) -m pytest -q

security:
	$(PY) ci/security_gate.py

check:
	$(PY) -m pytest -q
	$(PY) ci/security_gate.py --strict

sbom:
	$(PY) -m cyclonedx_py environment .venv --output-format JSON -o sbom.cdx.json

docker-build:
	docker build -t wallet-service:local .
