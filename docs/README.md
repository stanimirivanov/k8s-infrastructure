# Documentation map

## TL;DR

- Start with [the harness](development/harness.md) for setup and verification.
- Use [engineering standards](development/engineering-standards.md) for Python,
  infrastructure, state, security, and cost rules.
- Read the relevant operating guide before selecting an infrastructure target.
- Read [the capability contract](architecture/substrate-capability.md) when
  changing the boundary with a consumer.

| Task | Read first |
|:--|:--|
| Contributing or reporting completion | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Running or extending checks | [Harness](development/harness.md) |
| Adding or upgrading dependencies | [Dependency policy](development/dependency-policy.md) |
| Changing infrastructure code or target safety | [Engineering standards](development/engineering-standards.md) |
| Changing the substrate/consumer handoff | [Capability contract](architecture/substrate-capability.md) and [ADR index](decisions/README.md) |
| Local kind operations | [Local-kind guide](local-kind.md) |
| Argo CD installation | [Local Argo CD guide](local-argocd.md) |
| Reviewing imported history | [Local-kind](migrations/local-kind-history-import.md) and [Argo CD](migrations/argocd-history-import.md) migration records |

This map routes readers to the smallest relevant guidance. `CONTRIBUTING.md`
remains the canonical contributor workflow and completion-report policy.
