# ADR-0001: Establish the Kubernetes infrastructure repository boundary

- Status: Accepted
- Date: 2026-09-16
- Milestone: M06 — Production Readiness
- Deciders: Project owner
- Supersedes:
- Superseded by:

## TL;DR

- Use the created repository name `k8s-infrastructure` for the project-neutral
  substrate boundary proposed as `kubernetes-platform-infrastructure` in
  Perfeng ADR 0004.
- Own Kubernetes substrate lifecycle, Argo CD installation, generic capability
  qualification and provider cost/security guardrails here.
- Keep consumer desired state and product acceptance in each consumer's
  environment repository.
- Start with Python 3.12 managed by `uv`; add Task, Terraform and Kustomize only
  with the operational capability that needs them.
- This foundation transfers no live resource ownership.

## Context

Perfeng's completed local GitOps migration exposed a reusable boundary between
the Kubernetes/Argo CD substrate and Perfeng-specific composition. Perfeng ADR
0004 selected a separate project-neutral repository and proposed the longer
name `kubernetes-platform-infrastructure`. The repository was created as
`k8s-infrastructure`, which is concise while preserving the intended scope.

The foundation must make responsibility, safety and contributor expectations
clear before source history or live ownership is transferred.

## Decision

Use `k8s-infrastructure` as the repository name and durable ownership boundary.
It owns local and provider-specific Kubernetes substrate lifecycle, dedicated
access delivery, pinned Argo CD installation/readiness, generic capability
qualification, provider infrastructure IAM, cost policy and explicit
plan/apply/status/destroy workflows.

It does not own consumer Applications, workload configuration, product
credentials, databases, runtime releases, migrations or acceptance evidence.
The handoff follows the
[substrate capability boundary](../architecture/substrate-capability.md).

The initial engineering harness uses Python 3.12, `uv`, Ruff, ty, unittest and
portable CI on Windows and Linux. Python is appropriate for cross-platform
identity, validation and process orchestration. It does not generate provider
or consumer desired state.

Task will become the small operator-facing command surface when the first
operational capability is imported. Terraform and Kustomize will be introduced
and pinned with the provider or Argo CD implementation that uses them. Avoiding
unused tool scaffolding keeps the foundation honest and independently
verifiable.

No existing cluster, Argo CD installation or state changes owner in this
decision. The kind history import and its activation/cutover are separate
reviewed changes.

## Alternatives considered

### Rename the repository to `kubernetes-platform-infrastructure`

The longer name matches the original proposal but adds little clarity. The
created name is sufficiently neutral and unambiguous in its GitHub context.

### Keep the harness in `perfeng-environment`

This would leave reusable infrastructure governed by a consumer repository and
would make independent validation and reuse difficult.

### Add every planned tool immediately

Adding Task, Terraform, Kustomize and provider security scanners before any
operational code would create unexercised configuration and dependency churn.
Each tool instead arrives with a behavior and a fail-closed check.

## Consequences

- The actual repository name differs from the name recorded in Perfeng ADR
  0004; Perfeng references need a follow-up clarification without rewriting the
  accepted ADR.
- Contributors have a complete policy, CI and documentation boundary before
  infrastructure is imported.
- The initial repository cannot provision or inspect a cluster; this is a
  deliberate truthful limitation.
- Future consumers can depend on a neutral capability contract rather than
  Perfeng-specific scripts.

## Compatibility and migration

The first local import preserves `perfeng-local`, `kind-perfeng-local` and the
dedicated `.local/perfeng-local.kubeconfig` boundary. Source is imported with
history. Existing commands remain active in `perfeng-environment` until a
reviewed cutover passes local health, smoke and persistence acceptance.

## Security and operations

This change creates no resource, credential, state backend or live operational
path. Future live operations must be explicit, target-verified, bounded and
separate plan/apply/destroy. State, credentials, kubeconfigs and generated
plans remain outside Git.

## Validation

- Repository documentation links resolve locally.
- Python formatting, linting, type checking and unit tests pass on Windows and
  Linux.
- The repository contains no infrastructure state or live mutation command.
- README, policy and capability documentation agree on ownership.

## References

- [Perfeng ADR 0004](https://github.com/stanimirivanov/perfeng-environment/blob/main/docs/decisions/0004-separate-kubernetes-infrastructure.md)
- [Perfeng cloud Kubernetes evaluation](https://github.com/stanimirivanov/perfeng-environment/blob/main/docs/cloud-kubernetes-options.md)
