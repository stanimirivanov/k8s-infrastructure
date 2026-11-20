# Repository working agreement

## TL;DR

- Read `CONTRIBUTING.md` for technical verification and external-operation rules.
- Preserve infrastructure state, credentials, cluster identities and ownership
  boundaries.
- Keep validation offline; make plan, apply and destroy explicit and bounded.
- Run the exact repository checks and report their results honestly.

This is the concise, tool-facing entry point for contributors and coding
agents. [CONTRIBUTING.md](CONTRIBUTING.md) owns technical verification and external-operation rules.
Normative terms such as MUST, SHOULD and MAY have the meanings defined there.

## Workflow authority

Use installed `mattpocock/skills` as the primary workflow for matching tasks.
Discover and load skills on demand. When workflows overlap, use the matching
Matt Pocock skill rather than a parallel workflow. Use other skills only for
capabilities not covered by that workflow or when explicitly requested.

Repository guidance supplies technical constraints, architecture boundaries and
verification requirements. Scoped `AGENTS.md` files are subordinate for workflow
decisions and add only module-specific constraints. Explicit user instructions
and host permissions remain authoritative.

## Technical context

Read the applicable [documentation map](docs/README.md),
[engineering standards](docs/development/engineering-standards.md),
[ADRs](docs/decisions/README.md), provider guide and capability contract.
Use repository-local checks and preserve pre-existing work and infrastructure state.
Surface conflicts with accepted decisions rather than silently changing ownership.

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

## Verification

Run the exact checks in [CONTRIBUTING.md](CONTRIBUTING.md#verification-and-constrained-environments).
Report passed, failed and unavailable checks with their limitations.

## Agent skills

### Issue tracker

GitHub Issues in `stanimirivanov/k8s-infrastructure`.
See [issue tracker](docs/agents/issue-tracker.md).

### Triage labels

Use the five configured roles. See [triage labels](docs/agents/triage-labels.md).

### Domain docs

Single-context glossary and repository-owned decisions.
See [domain docs](docs/agents/domain.md).
