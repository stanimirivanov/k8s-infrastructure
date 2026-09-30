# Repository working agreement

## TL;DR

- Read `CONTRIBUTING.md`; it is the canonical workflow policy.
- Preserve infrastructure state, credentials, cluster identities and ownership
  boundaries.
- Keep validation offline; make plan, apply and destroy explicit and bounded.
- Run the exact repository checks and report their results honestly.

This is the concise, tool-facing entry point for contributors and coding
agents. [CONTRIBUTING.md](CONTRIBUTING.md) is the canonical workflow policy.
Normative terms such as MUST, SHOULD and MAY have the meanings defined there.

## Before changing anything

1. You MUST read [CONTRIBUTING.md](CONTRIBUTING.md), especially its ambiguity,
   issue-timing, verification and external-side-effect rules.
2. You MUST inspect the branch and working tree and preserve pre-existing work.
3. You MUST read the applicable
   [documentation map](docs/README.md),
   [engineering standards](docs/development/engineering-standards.md), accepted
   [ADRs](docs/decisions/README.md), provider guide and capability contract.
4. You MUST use repository-local checks. You MUST NOT claim an unavailable or
   unexecuted check passed.

If code, infrastructure state, documentation and an accepted decision disagree,
you MUST surface the conflict. Correct an obvious local defect or propose a
superseding ADR; do not silently pick the convenient source.

## Repository responsibility

`k8s-infrastructure` owns project-neutral Kubernetes substrates, Argo CD
installation, generic capability qualification, provider infrastructure IAM
and cost guardrails. It does not own a consumer's workloads, application
credentials, release selection, data migration or product acceptance.

- Provider implementations remain explicit and independently understandable.
- Kubeconfig, Terraform state, credentials and generated plans remain outside
  Git.
- A consumer owns the desired state it submits to the installed Argo CD.
- No existing environment changes owner until its reviewed cutover passes.

## Shape of work

- Each task and pull request MUST deliver one coherent, independently
  reviewable infrastructure capability.
- Separate extraction, ownership cutover, provider addition, dependency update
  and broad refactoring unless correctness makes them inseparable.
- Scope, exclusions, target identity, state effects, cost risk, rollout,
  recovery, rollback and verification evidence MUST be explicit.
- Prefer a thin exercised capability over a speculative provider abstraction.

## Infrastructure rules

- Validation and preview MUST be offline and non-mutating.
- Plan, apply and destroy MUST be distinct. Apply and destroy require explicit
  execution, an exact target and appropriate authorization.
- Operations MUST use dedicated state and kubeconfig paths, verify target
  identity, use bounded timeouts and leave safe diagnostic evidence.
- Credentials MUST NOT appear in arguments, logs, plans, fixtures or Git.
- Terraform/OpenTofu state, Kubernetes Secrets and provider credentials MUST
  NOT become cross-repository handoff artifacts.
- Cost eligibility is checked before apply and provider inventory/billing is
  checked after it; budget alerts are not spending caps.
- Argo CD installation ownership is separate from ownership of Applications.

## Quality and documentation

- Test observable behavior at the lowest boundary that proves it, including
  identity mismatch, stale state, timeout, partial failure and recovery.
- Keep parsing and validation independent of live provider or cluster access.
- Comments explain decisions and constraints, not syntax or lint findings.
- Long documents MUST include the TL;DR required by CONTRIBUTING.
- Operational changes and troubleshooting guidance change together.

## Completion

The exact checks and completion-report format are defined in
[CONTRIBUTING.md](CONTRIBUTING.md). Follow them after every work item.
