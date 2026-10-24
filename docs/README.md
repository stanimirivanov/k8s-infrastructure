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
| Capped kind scheduling capacity and full-installation sizing | [Node capacity](node-capacity.md), [ADR 0008](decisions/0008-capped-kind-node-allocatable.md) |
| Disposable scheduler/restart proof | [Capacity qualification](capacity-qualification.md) |
| Local kind operations | [Local-kind guide](local-kind.md) |
| FleetIQ or another isolated local consumer | [Isolated consumer guide](isolated-local-consumers.md), [ADR 0004](decisions/0004-isolated-local-consumer-profiles.md) |
| Argo CD installation | [Local Argo CD guide](local-argocd.md) |
| Delegated Argo for isolated consumers | [Delegated Argo guide](delegated-argo.md), [ADR 0005](decisions/0005-delegated-consumer-argo.md) |
| Policy-enforcing CNI and trusted local HTTPS | [Networking/ingress guide](networking-and-ingress.md), [ADR 0006](decisions/0006-isolated-network-and-gateway.md) |
| Database operators, retained local storage and recovery boundaries | [Database/storage guide](database-and-storage.md), [ADR 0007](decisions/0007-local-database-and-retained-storage.md) |
| Argo Workflows installation | [Local Argo Workflows guide](local-argo-workflows.md), [ADR 0003](decisions/0003-install-namespaced-argo-workflows.md) |
| Resource Metrics API or kubelet serving certificates | [Verified resource metrics](resource-metrics.md), [ADR-0002](decisions/0002-verified-resource-metrics.md) |
| Reviewing imported history | [Local-kind](migrations/local-kind-history-import.md) and [Argo CD](migrations/argocd-history-import.md) migration records |

This map routes readers to the smallest relevant guidance. `CONTRIBUTING.md`
remains the canonical contributor workflow and completion-report policy.
