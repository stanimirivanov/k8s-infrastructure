# ADR-0003: Install a namespaced Argo Workflows substrate

- Status: Proposed
- Date: 2026-10-17
- Milestone: M06 — Production Readiness
- Deciders: Project owner and Kubernetes substrate maintainers
- Supersedes: None
- Superseded by: None

## TL;DR

- Install the official Argo Workflows `v4.1.5` namespace manifest in the dedicated `argo-workflows` namespace for local `kind`.
- Verify the release asset's SHA-256 and namespace-scoped topology before applying it.
- Keep the server internal and the controller without rights to Perfeng workload namespaces.
- Consumer release templates, submission authority and persistent evidence arrive in later changes.

## Context

Perfeng [ADR 0014](https://github.com/stanimirivanov/perfeng-environment/blob/main/docs/decisions/0014-separate-release-and-run-orchestration.md) assigns finite release operations to Argo Workflows, continuous workload reconciliation to Argo CD and performance Runs to the control plane. The cluster and Argo CD installation precede any release workflow. This repository already owns those reusable substrate operations but has no Workflows capability.

## Decision

The local profile selects the upstream `v4.1.5` `namespace-install.yaml` release asset by exact URL and SHA-256. The installer accepts only the reviewed namespace identity and verifies the downloaded bytes and manifest topology before a cluster write. The official asset creates eight cluster-scoped CRDs and one PriorityClass, plus namespace-scoped Roles, RoleBindings, ServiceAccounts, ConfigMap, ClusterIP Service and two Deployments. The controller and server both use `--namespaced`; neither receives a ClusterRoleBinding. The server has no Ingress, NodePort or LoadBalancer and retains the release's default client authentication. Only an explicitly selected, healthy local `kind` context may be mutated.

Bootstrap is explicit and retryable. A foreign namespace is rejected. On first installation, existing Workflow CRDs or the controller PriorityClass cause refusal rather than adoption. A partial owned installation is retained for inspection; retry reuses the same version and server-side apply without forcing conflicts. Health is read-only and checks the owned namespace plus both Deployment rollouts. Changing the namespace's release label or upgrading the controller requires a separate review and procedure.

The installation does not add a WorkflowTemplate, release submitter, workflow execution ServiceAccount, Perfeng workload permissions, artifact repository, archive or exposed UI. A later consumer PR defines those with the release request contract. The official controller Role can act on resources in its own namespace; that is not authority to reconcile Perfeng Applications or execute product Runs.

## Alternatives considered

| Alternative | Reason declined for this slice |
|:--|:--|
| Cluster-wide official install | Grants a controller watch and execution scope beyond the first local release pilot. |
| Quick-start manifest | Includes demo defaults and is explicitly not the production installation path. |
| Repackage the 12 MB release manifest in Git | Duplicates upstream generated CRDs and makes upgrades noisy. The exact SHA-256 pin fails closed if release bytes change. |
| Helm chart | Adds a second chart release/version surface and package source before a real need for chart-specific values. |

## Consequences and compatibility

The local cluster gains eight CRDs and one PriorityClass, even though workflow execution remains namespace-scoped. CRDs are shared cluster API types, so removal is only documented for a disposable cluster and is never an implicit retry. The official install adds a small controller/server footprint; no existing Perfeng Application or data resource is touched by this source change. The existing manual M04 cutover remains authoritative.

The installed server is internal but reachable by other cluster Pods with suitable network access. Client authentication and namespace RBAC remain required; production SSO, restricted template submission, network policy, persistence and disaster recovery belong to M06 hardening after a non-local target is selected.

## Validation

Offline selection tests reject mutable or foreign URLs, wrong namespace ownership and stale version labels. Runtime byte and topology validation rejects a changed asset, cluster-wide RBAC or external server exposure. `make workflows-asset-check` checks the official release without cluster writes. `make verify` and `make validate` exercise repository checks; a disposable-kind install, controller restart and cluster removal remain the live acceptance gate when Docker is available.
