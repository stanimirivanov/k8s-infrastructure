# k8s-infrastructure

Project-neutral Kubernetes substrate provisioning, GitOps bootstrap and
capability qualification.

## TL;DR

- This repository will own reusable local and cloud Kubernetes substrates.
- It will install and qualify Argo CD, but it will not own applications that
  Argo CD reconciles for consuming projects.
- The existing Perfeng local-kind lifecycle has been imported with filtered
  source history, but live execution is deliberately disabled.
- The Argo CD bootstrap and controller-readiness logic are also imported and
  dormant; consumer Applications and repository credentials remain elsewhere.
- No existing cluster has transferred ownership; `perfeng-environment` remains
  the active owner until a reviewed cutover.
- Run `make validate` before proposing a change.

## Status

The repository contains its contributor harness, the initial
[substrate capability boundary](docs/architecture/substrate-capability.md) and
the dormant [local-kind lifecycle](docs/local-kind.md) and
[Argo CD bootstrap](docs/local-argocd.md). Until an explicit cutover is
accepted, `perfeng-environment` remains the owner of the existing
`perfeng-local` kind cluster and Argo CD installation. Both imported CLIs
provide offline previews but reject live execution before invoking external
tools.

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

Install the locked development environment and validate the repository:

```sh
uv sync --locked
make validate
```

The individual commands are documented in
[CONTRIBUTING.md](CONTRIBUTING.md). The imported local-kind command remains
dormant, as does the imported Argo CD bootstrap. See their guides for the exact
preview and activation boundaries.

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
