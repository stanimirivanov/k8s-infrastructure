# k8s-infrastructure

Project-neutral Kubernetes substrate provisioning, GitOps bootstrap and
capability qualification.

## TL;DR

- This repository will own reusable local and cloud Kubernetes substrates.
- It will install and qualify Argo CD, but it will not own applications that
  Argo CD reconciles for consuming projects.
- The imported local-kind and Argo CD lifecycles are cutover-ready, preserve
  the existing Perfeng identities and require explicit live execution.
- A pinned, namespace-scoped Argo Workflows add-on is available for an explicit
  local-kind release pilot; it does not install consumer workflows.
- Consumer Applications and repository credentials remain outside this
  repository.
- Perfeng records its provider cutover as accepted and remains the operator entry
  point through its immutable provider selection. Workspace changes do not update it.
- A project-neutral [isolated consumer profile](docs/isolated-local-consumers.md)
  adds explicit identities for FleetIQ and future projects, with separate
  qualification for networking, controllers and storage.
- Run `make verify` while editing and `make validate` before handoff.

## Status

The opt-in [resource metrics capability](docs/resource-metrics.md) adds a
verified-TLS Metrics Server chart and explicit node/serving-certificate
operations. It does not deploy an Application or change the existing cluster.
Consumer selection must pin its merged provider revision and real public CA.

The repository contains its contributor harness, the initial
[substrate capability boundary](docs/architecture/substrate-capability.md), the
[local-kind lifecycle](docs/local-kind.md) and the
[Argo CD bootstrap](docs/local-argocd.md), plus the
[Argo Workflows add-on](docs/local-argo-workflows.md). Their CLIs provide offline previews
and explicit live execution with target, ownership and timeout checks.
`perfeng-environment` records the cutover as accepted on 2026-09-17 and selects
provider `5970bffa1f3d200e73d5a55e0cd4ef34ed9ef3b7`. It remains the supported
operator entry point for its existing cluster and Argo CD installation; a new
provider merge never changes that selection automatically.

## Responsibility

This repository owns reusable infrastructure capabilities:

- local kind cluster creation, identity, kubeconfig delivery and destruction;
- provider-specific network, managed Kubernetes, node, storage and access
  infrastructure;
- pinned Argo CD installation and controller-readiness validation;
- generic cluster capability and conformance checks;
- provider infrastructure IAM, cost guardrails, plan/apply/status/destroy
  boundaries and recovery documentation.

It does not own consumer namespaces, workload charts, release selections,
application credentials, database migrations, performance policies or product
acceptance. Perfeng-specific composition remains in `perfeng-environment`, and
reusable Perfeng workload packaging remains in `perfeng-k8s`.

## Planned layout

```text
clusters/
  local-kind/perfeng-local/
  oci-oke/
docs/
  architecture/
  decisions/
  development/
scripts/
tests/
```

Provider implementations remain explicit. Shared validation is preferred over
a lowest-common-denominator Terraform module.

## Development

Prerequisites:

- Python 3.12;
- [uv](https://docs.astral.sh/uv/);
- GNU Make for the convenience command surface.
- Helm v4.1.4 for offline resource-metrics linting and rendering.

Install the locked development environment and run the offline inner loop:

```sh
make bootstrap
make verify
```

Run `make validate` before handoff when current vulnerability data is
available. The [documentation map](docs/README.md) routes contributors to the
[harness](docs/development/harness.md), policy and operating guides. See the
local-kind and Argo CD guides for their preview, execution and staged
consumer-cutover boundaries.

## Project governance

- [Contributor workflow](CONTRIBUTING.md)
- [Engineering standards](docs/development/engineering-standards.md)
- [Architecture decisions](docs/decisions/README.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Perfeng cross-repository roadmap](https://github.com/stanimirivanov/perfeng-environment/blob/main/docs/roadmap/milestones.md)

The repository boundary originates in
[Perfeng ADR 0004](https://github.com/stanimirivanov/perfeng-environment/blob/main/docs/decisions/0004-separate-kubernetes-infrastructure.md).
The actual repository name is recorded in local ADR 0001.

## License

See [LICENSE](LICENSE).
