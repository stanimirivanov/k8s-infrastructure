# Kubernetes substrate capability boundary

- Status: Initial contract
- Applies from: repository foundation

## TL;DR

- This repository delivers a qualified Kubernetes substrate and an operational
  Argo CD installation; consumers deliver their own desired state.
- The handoff contains identities and capabilities, never credentials or
  Terraform state.
- Provider implementations may differ, but each must report the same core
  capability categories and fail clearly when a requirement is absent.
- The first local implementation must preserve `perfeng-local`,
  `kind-perfeng-local` and its dedicated kubeconfig path.
- This is a human-readable v0 contract. A versioned machine schema waits until
  at least two implementations demonstrate stable common fields.

## Purpose

The boundary prevents reusable infrastructure and consumer-specific deployment
composition from becoming one system. It tells an environment repository what
it may rely on without exposing provider credentials, state layout or internal
provisioning choices.

The provider implementation is responsible for proving the claims it emits.
The consumer is responsible for deciding whether those claims meet its
requirements before reconciling workloads.

## Infrastructure-owned lifecycle

The infrastructure repository owns:

- creation, identity checks, status and explicit destruction of the Kubernetes
  substrate;
- delivery of a dedicated kubeconfig and exact context outside Git;
- node architecture and Kubernetes-version qualification;
- dynamic storage capability qualification;
- DNS and outbound-connectivity qualification;
- installation, version selection and readiness of Argo CD;
- optional namespace-scoped Argo Workflows installation and controller readiness,
  without consumer release templates or product execution authority;
- optional ingress, load-balancer and external-secret capabilities when a
  provider profile declares them;
- an optional resource Metrics API package with explicit serving-TLS provisioning,
  while consumers retain their Argo CD selection and benchmark eligibility;
- provider IAM required to create and operate the substrate; and
- provider-specific cost preflight, inventory evidence and recovery guidance.

It does not reconcile consumer workloads or create their namespaces,
credentials, databases, object stores, policies or acceptance evidence.

## Consumer-owned lifecycle

A consumer such as `perfeng-environment` owns:

- its Argo CD Projects, root Application and child Applications;
- workload namespaces, charts, overlays and immutable release selections;
- application identities, Secrets and initialization;
- schema/data migrations and runtime configuration;
- compatibility checks beyond the generic substrate capabilities; and
- product health, smoke, persistence and performance acceptance.

A consumer must not mutate infrastructure-owned resources as an implicit repair.
If a capability is missing or stale, it must fail with an actionable diagnosis.

## Handoff information

The [isolated local consumer profile](../isolated-local-consumers.md) adds an
explicit project-neutral target alongside the retained Perfeng compatibility
profile. Sharing this provider does not imply sharing Perfeng benchmark workers.
Its local observation report preserves this v0 handoff; it is not a generic
cross-provider schema or proof of capabilities listed as unqualified.
The [delegated Argo capability](../delegated-argo.md) separates provider-owned
shared APIs/controllers from consumer-owned namespaces, desired state and templates.
Controller readiness, native authorization/execution, sign-in and durable evidence
are distinct claims and must be reported separately.

Every supported substrate must make the following non-secret information
available to its consumer and to operators:

| Category | Required information |
|:--|:--|
| Identity | Stable substrate name, provider profile and infrastructure revision |
| Access | Dedicated kubeconfig path and exact context name |
| Kubernetes | Server version and supported node operating systems/architectures |
| Nodes | Schedulable capacity summary and relevant taints or placement limits |
| Storage | Default or named dynamic `ReadWriteOnce` StorageClass and retention behavior |
| Network | Cluster DNS result and outbound-network expectation |
| GitOps | Argo CD version, namespace and controller readiness |
| Optional capabilities | Argo Workflows version, namespace and controller readiness when installed; ingress, load balancer, external secrets and other explicitly qualified features |

The handoff must not contain credentials, kubeconfig bytes, Terraform state,
provider account secrets, Kubernetes Secret data or sensitive billing details.
The implementation may initially expose this information through bounded
status output. A committed machine-readable schema is deferred until two real
implementations prove stable shared semantics.

## Compatibility and first import

The first local-kind implementation must preserve the existing Perfeng
identities:

- substrate name: `perfeng-local`;
- Kubernetes context: `kind-perfeng-local`;
- dedicated kubeconfig: `.local/perfeng-local.kubeconfig` at the consuming
  workspace boundary.

Preserving these values avoids replacing the cluster, persistent volumes,
Secrets or Argo CD resources merely because lifecycle code changes repository.
The source import and the ownership cutover are separate pull requests. During
temporary duplication, only the existing environment implementation is active.

Consumers pin a reviewed immutable infrastructure release or revision. They do
not execute an unreviewed branch or infer compatibility from repository layout.

## Failure contract

Infrastructure commands distinguish at least:

- invalid configuration or unsupported capability;
- missing prerequisite or credential;
- target identity or ownership mismatch;
- provider or cluster unavailability;
- timeout or cancellation;
- partial apply requiring inspection; and
- internal implementation defect.

A failure leaves enough non-secret evidence to identify the target, completed
phase and safe next inspection. It does not rotate credentials, replace state,
delete resources or widen access as an automatic recovery.

## Security and cost boundary

- Credentials enter only through documented private channels and remain out of
  command arguments, logs, plans and Git.
- Network and IAM defaults are deny-by-default and least privilege.
- Apply and destroy verify the exact account/project/compartment, region,
  state/backend and substrate identity.
- A zero-cost profile rejects unapproved resource classes and sizes before
  apply. Budget alerts are delayed detection, not enforcement.
- Provider inventory and billing eligibility are checked after a live change.
- Free-tier capacity failure does not authorize a paid fallback.

## Evolution

Provider-specific fields remain in provider-specific output unless multiple
consumers need common semantics. A machine-readable handoff schema belongs in a
neutral contract boundary only after local kind and one cloud implementation
demonstrate it. Breaking changes require a versioned transition and consumer
compatibility evidence.
