# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Alexander D. Kanevskiy

# GNU make. Run "make" or "make help" for the list of targets.
# Variables can be overridden on the command line, e.g. "make install DESTDIR=/tmp/root".

UV     ?= uv
PYTHON ?= python3
PREFIX ?= /usr
DESTDIR ?=
TAG    ?=
# release-prepare switches: set to 1 to enable
DRY_RUN ?=
COMMIT  ?=
TAG_IT  ?=
SIGN    ?=
# Values typed by the user reach recipes only through the environment ("$$TAG"), never
# expanded into the shell command line. The switches only select fixed flags.
export TAG DESTDIR PREFIX
PREPARE_FLAGS :=
ifeq ($(DRY_RUN),1)
PREPARE_FLAGS += --dry-run
endif
ifeq ($(COMMIT),1)
PREPARE_FLAGS += --commit
endif
ifeq ($(TAG_IT),1)
PREPARE_FLAGS += --tag
endif
ifeq ($(SIGN),1)
PREPARE_FLAGS += --sign
endif

.DEFAULT_GOAL := help

.PHONY: help sync lint typecheck format test test-network examples check build pyz install \
	deb rpm changelog release-check release-prepare clean distclean

help: ## Show this help
	@echo "Usage: make [target] [VAR=value ...]"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "} {printf "  %-16s %s\n", $$1, $$2}'
	@echo
	@echo "Variables: UV=$(UV) PYTHON=$(PYTHON) PREFIX=$$PREFIX DESTDIR=$$DESTDIR"

sync: ## Create or update the virtualenv with all dev dependencies and extras
	$(UV) sync --all-extras

lint: ## Run ruff check and ruff format --check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck: ## Run the ty type checker
	$(UV) run ty check

format: ## Format the code and apply safe lint fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

test: ## Run the test suite (offline)
	$(UV) run pytest

test-network: ## Run the live network tests (MINIDEB_NETWORK=1)
	MINIDEB_NETWORK=1 $(UV) run pytest -m network

examples: ## Run the offline example tests (tests/test_examples.py)
	$(UV) run pytest tests/test_examples.py

check: lint typecheck test ## Run lint, typecheck and test

build: ## Build the wheel and sdist into dist/
	$(UV) build

pyz: ## Build the self-contained dist/minideb.pyz
	$(PYTHON) scripts/build_pyz.py -o dist/minideb.pyz

install: build ## Install the built wheel offline under DESTDIR/PREFIX
	$(UV) pip install --offline --no-deps --python $(PYTHON) \
		--prefix "$$DESTDIR$$PREFIX" "$$(ls -t dist/*.whl | head -n 1)"

deb: ## Build the Debian package with dpkg-buildpackage
	@if [ ! -d debian ] || [ ! -f debian/control ]; then \
		echo "error: debian/ packaging files are missing (not added yet)" >&2; exit 1; fi
	@if ! which dpkg-buildpackage >/dev/null 2>&1; then \
		echo "error: dpkg-buildpackage not found; install dpkg-dev" >&2; exit 1; fi
	dpkg-buildpackage -us -uc -b

rpm: ## Build the RPM with rpmbuild from a git archive tarball
	@if [ ! -f python-minideblib.spec ]; then \
		echo "error: python-minideblib.spec is missing (not added yet)" >&2; exit 1; fi
	@if ! which rpmbuild >/dev/null 2>&1; then \
		echo "error: rpmbuild not found; install rpm-build" >&2; exit 1; fi
	@set -e; top=$$(mktemp -d); trap 'rm -rf "$$top"' EXIT; \
		ver=$$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n1); \
		mkdir -p "$$top/SOURCES"; \
		git archive --format=tar.gz --prefix=python-minideblib-$$ver/ \
			-o "$$top/SOURCES/minideblib-$$ver.tar.gz" HEAD; \
		rpmbuild -ba --define "_topdir $$top" python-minideblib.spec; \
		mkdir -p dist; cp -r "$$top/RPMS" "$$top/SRPMS" dist/

changelog: ## Regenerate CHANGELOG.md with git-cliff
	@if ! which git-cliff >/dev/null 2>&1; then \
		echo "error: git-cliff not found; see https://git-cliff.org/docs/installation" >&2; \
		exit 1; fi
	git cliff --config cliff.toml -o CHANGELOG.md

release-check: ## Check versions match TAG=vX.Y.Z[-rcN] (rc1 = PEP 440 rc1, Debian ~rc1)
	@if [ -z "$$TAG" ]; then echo "error: pass TAG=vX.Y.Z" >&2; exit 2; fi
	$(PYTHON) scripts/check_release.py "$$TAG"

release-prepare: ## Set TAG=vX.Y.Z[-rcN] in all version files (DRY_RUN=1 COMMIT=1 TAG_IT=1 SIGN=1)
	@if [ -z "$$TAG" ]; then echo "error: pass TAG=vX.Y.Z" >&2; exit 2; fi
	$(PYTHON) scripts/prepare_release.py $(strip $(PREPARE_FLAGS) "$$TAG")

clean: ## Remove build outputs and caches (keeps .venv)
	rm -rf dist build *.egg-info src/*.egg-info .pytest_cache .ruff_cache
	find . -path ./.venv -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +

distclean: clean ## Like clean, and also remove .venv
	rm -rf .venv
