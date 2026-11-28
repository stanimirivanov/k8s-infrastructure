UV_RUN := uv run --locked --offline
TOFU ?= tofu

.PHONY: help bootstrap doctor fmt fmt-check docs docs-check check test configuration-check workflows-asset-check delegated-argo-asset-check network-asset-check database-asset-check metrics-check iac-pilot-check kind-baseline-check kind-baseline-argo-check kind-plan-check pr-event-check verify supply-chain validate

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
	@echo "  make workflows-asset-check  Verify official Argo Workflows bytes and namespace RBAC"
	@echo "  make delegated-argo-asset-check  Verify isolated consumer Argo asset bytes"
	@echo "  make network-asset-check  Verify pinned Cilium/Gateway assets and actual Helm render"
	@echo "  make database-asset-check  Verify pinned CNPG, Barman, cert-manager and kind storage assets"
	@echo "  make metrics-check  Strictly lint/render the verified-TLS resource metrics chart"
	@echo "  make iac-pilot-check  Format-check, validate and test the initialized kind module offline"
	@echo "  make kind-baseline-check  Validate and test the kind-baseline root with the initialized offline provider"
	@echo "  make kind-baseline-argo-check  Render and check the baseline Argo CD overlay from staged inputs offline"
	@echo "  make kind-plan-check  Exercise registered and legacy kind fixtures with the supplied KIND_TEST_JUST"
	@echo "  make pr-event-check  Validate issue linkage in the GitHub PR event"
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

workflows-asset-check: delegated-argo-asset-check network-asset-check database-asset-check
	$(UV_RUN) python -m scripts.workflows asset-check

delegated-argo-asset-check:
	$(UV_RUN) python -m scripts.delegated_argo asset-check

network-asset-check:
	$(UV_RUN) python -m scripts.network asset-check

database-asset-check:
	$(UV_RUN) python -m scripts.database asset-check

metrics-check:
	$(UV_RUN) python -m scripts.metrics_chart

iac-pilot-check:
	$(TOFU) -chdir=clusters/local-kind/iac-pilot fmt -check -recursive
	$(TOFU) -chdir=clusters/local-kind/iac-pilot validate
	$(TOFU) -chdir=clusters/local-kind/iac-pilot test

kind-baseline-check:
	$(TOFU) -chdir=clusters/local-kind/kind-baseline fmt -check -recursive
	$(TOFU) -chdir=clusters/local-kind/kind-baseline validate
	$(TOFU) -chdir=clusters/local-kind/kind-baseline test

kind-baseline-argo-check:
	@test -n "$$KIND_BASELINE_KUBECTL" && test -n "$$KIND_BASELINE_ARGO_INSTALL" || { echo 'Set KIND_BASELINE_KUBECTL and KIND_BASELINE_ARGO_INSTALL to the staged input paths.' >&2; exit 1; }
	$(UV_RUN) python -m unittest discover -s tests -p test_kind_baseline_argocd.py -v

kind-plan-check:
	@test -n "$$KIND_TEST_JUST" || { echo 'Set KIND_TEST_JUST to the absolute path to the pinned just binary.' >&2; exit 1; }
	python3 -m unittest discover -s tests -p 'test_kind_just.py' -v

pr-event-check:
	$(UV_RUN) python -m scripts.repository_policy --pr-event

verify: check test configuration-check metrics-check

supply-chain:
	uv lock --check
	uv audit --locked

validate: verify supply-chain
