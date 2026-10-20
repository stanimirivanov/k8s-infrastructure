# Architecture decisions

## TL;DR

- ADRs record durable decisions about ownership, state, security, cost,
  compatibility, providers, foundational tools or topology.
- Accepted ADRs are immutable history; supersede them with a new ADR.
- Reserve the next number immediately before opening the pull request and
  resolve collisions by taking the next available number.

## Index

| ADR | Status | Decision |
|:--|:--|:--|
| [0001](0001-repository-boundary-and-tooling.md) | Accepted | Establish the repository boundary and initial tool choices |
| [0002](0002-verified-resource-metrics.md) | Proposed | Provide verified-TLS resource metrics with explicit certificate operations |
| [0003](0003-install-namespaced-argo-workflows.md) | Proposed | Install a pinned namespaced Argo Workflows substrate for finite releases |
| [0004](0004-isolated-local-consumer-profiles.md) | Proposed | Add project-neutral isolated local consumer identities while preserving Perfeng |
| [0005](0005-delegated-consumer-argo.md) | Proposed | Delegate isolated Argo controllers, shared APIs and constrained release authority |

## Numbering under concurrent work

1. Inspect this index and `docs/decisions` immediately before creating a branch.
2. Reserve the lowest unused four-digit number in the issue or pull request.
3. If another change lands with that number first, rename the unmerged ADR to
   the next unused number and update all references in the same change.
4. Never renumber an accepted ADR.

An ADR states context, decision, alternatives, consequences, compatibility,
security/operations and validation. It does not contain task-specific handoff
instructions.
