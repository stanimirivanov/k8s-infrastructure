UV_RUN := uv run --locked --offline

.PHONY: help bootstrap doctor fmt fmt-check docs docs-check check test configuration-check verify supply-chain validate

help:
	@echo k8s-infrastructure engineering command surface
	@echo "  make bootstrap  Resolve the locked Python environment"
	@echo "  make doctor     Report the effective engineering toolchain"
	@echo "  make fmt        Format Python sources"
	@echo "  make fmt-check  Verify formatting without changing files"
	@echo "  make docs-check Validate documentation and repository policy"
	@echo "  make check      Run formatting, policy, linting and type checks"
	@echo "  make test       Run the unit-test suite"
	@echo "  make configuration-check  Validate reviewed local infrastructure selections"
	@echo "  make verify     Run all offline repository-local checks"
	@echo "  make supply-chain  Check the lock and known vulnerabilities"
	@echo "  make validate   Run complete non-live acceptance"

bootstrap:
	uv sync --locked

doctor:
	@git --version
	@uv --version
	@$(UV_RUN) python --version
	@$(MAKE) --version

fmt:
	$(UV_RUN) ruff format .

fmt-check:
	$(UV_RUN) ruff format --check .

docs-check:
	$(UV_RUN) python -m scripts.repository_policy

docs: docs-check

check: fmt-check docs-check
	$(UV_RUN) ruff check .
	$(UV_RUN) ty check

test:
	$(UV_RUN) python -m unittest discover -s tests -v

configuration-check:
	$(UV_RUN) python -m scripts.configuration_check

verify: check test configuration-check

supply-chain:
	uv lock --check
	uv audit --locked

validate: verify supply-chain
