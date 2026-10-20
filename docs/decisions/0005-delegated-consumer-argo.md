# ADR 0005: Delegated Argo instances for isolated consumers

- Status: Proposed
- Date: 2026-10-20

## TL;DR

- Preserve Perfeng's installers/settings; use an explicitly owned I01 target.
- Own shared APIs once per cluster and verify schemas before compatible reuse.
- Consumers own namespaces, Projects, Applications and immutable templates;
  infrastructure binds only reviewed controller/release permissions.
- Combine Secure reference mode, narrow RBAC and fail-closed native admission.
- Qualify real authorization, denial, execution and restart separately from
  offline tests. Sign-in, step approvals and persistent evidence remain separate.

## Context

Widening the existing Perfeng controllers or global revision settings would
change another consumer's authority. I01 supplies independent local identities.
FleetIQ needs isolated controllers without product installation, application
credential access, cluster-admin grants or takeover of existing shared APIs.

## Decision

Select the existing Argo CD v3.5.2 and Workflows v4.1.5 versions; SHA-256 pin all
downloaded manifests. Extract only CRDs from CD's full installer. Render its
namespace installer into `<consumer>-gitops-system`. Workflows runs in
`<consumer>-workflow-system` with `--namespaced`, explicit managed namespace
`<consumer>-releases`, and consumer instance ID. Servers stay ClusterIP.

Shared CRDs and PriorityClass carry provider ownership. Existing APIs require
that identity and the same normalized schema/content; they are never overwritten
by a second instance or deleted by instance removal. Upgrades require separate
compatibility review for every installed API user. Legacy objects without the
shared owner identity are refused, not silently migrated.

Require a bounded public delegation selecting the consumer, existing application
namespaces and approved template names. Consumers bootstrap and own application
and release namespaces. The provider owns system namespaces and its delegated
Roles/Bindings/minimal accounts. A changed delegation fingerprint requires an
explicit authority transition. Ordinary apply creates no consumer Projects,
Applications, templates, workload namespaces or product credentials.

Native ValidatingAdmissionPolicy/Binding resources fail closed. Only a releaser
can submit an approved WorkflowTemplate reference, one `release-digest` argument
and initial global suspension. Extra spec fields and metadata injection are
denied. Named templates cannot change spec in place. Consumer template publishers
remain trusted code authors and select a newly reviewed name for a new version.

Submission field guards are derived from the hash-verified Workflow CRD, using
typed CEL presence checks rather than invalid CRD-object/map comparisons. Every
declared field outside the narrow request is forbidden, including new upstream
fields. Open-ended structural schemas are refused and require compatibility
review. Native admission compilation remains a prerequisite to granting roles.

Kubernetes' authenticated garbage-collector identity may remove only its
`foregroundDeletion` finalizer from an already deleting workflow. The spec,
status, audit annotations, labels and owner references must remain unchanged.
This narrow recovery path avoids blocking native deletion without granting
release principals finalizer or request-editing authority.

Admission binds submitted-by/approved-by annotations to authenticated caller
identities and preserves them with the execution record for consumer audit export.

Viewers read workflows/templates; releasers cannot launch Pods, edit templates
or read Secrets. Approvers can clear initial suspension only while preserving
request, status and security metadata; they get no arbitrary node/status patch
authority. Executors create/patch task results only. Trusted templates must
explicitly select that minimal executor. Controller authority reaches its settings
and managed namespace, not application/database namespaces.

CD gets explicit namespaced resource grants and bounded in-cluster registration,
without namespace/RBAC/cluster-type creation. Consumer AppProjects remain required
source/destination policy. The new instance enables
`application.sync.requireOverridePrivilegeForRevisionSync`; release roles read/sync
and are explicitly denied override and Application create/update/delete. Anonymous
and default admin access are disabled. Trusted sign-in and expiring Argo API
credential delivery require separate qualification; no usable login is issued.

## Alternatives

- Expand Perfeng authority: rejected without a consumer ownership cutover.
- RBAC or Secure referencing alone: insufficient; RBAC cannot filter patch fields
  and upstream reference mode permits fields such as entrypoint.
- A custom approval/submission service: deferred; native admission supports this
  initial narrow interface without a webhook or extra credential store.
- A new chart/operator: unnecessary for the pinned upstream manifests.

## Operations and compatibility

Ownership/namespace checks precede writes. Shared APIs are created only when
absent. Admission is installed and type checked before release roles. Apply never
forces conflicts; partial state remains for an exact-input retry. No automatic
cleanup, credential rotation, rollback or adoption occurs.

Confirmed instance removal refuses nonterminal workflows, revokes delegated
bindings first and uses server-checked UID delete preconditions. It retains
shared APIs, namespaces, Secrets, templates and workflow/evidence records.
Complete disposal remains a separately authorized I01 operation.

Nine controller Pods request 450m CPU/576 MiB, with aggregate limits of
4500m/3456 MiB. I01's worker container cap remains the effective ceiling, not new
host capacity. Networking/TLS, SSO/token issuance, step-level approval, persistent
audit/archive and consumer releases remain separate qualifications. This is not a
production tenancy, evidence recovery or performance-isolation claim.

Argo uses an explicitly selected `isolated-argo-local` profile: two CPUs/4 GiB
for each node (4 CPUs/8 GiB total). Native qualification of the smaller I01
control plane showed memory-limit pressure and liveness restarts during OpenAPI
schema discovery. The original `isolated-local` profile remains unchanged.
Profile identity and budget digests prevent implicit resizing or cross-profile
adoption; replacement needs authorization of the exact target and new envelope.

## Validation

Offline tests cover preview purity, delegation/ownership refusal, shared schema
reuse, controller scopes, limits, permissions, configuration and removal safety.
The existing network-only asset gate verifies new pins as well as the legacy
manifest contract. The workflow's setup-uv hash remains unchanged.

Authorized native qualification checks SubjectAccessReviews and server admission
with working positive controls, runs a neutral minimal-account workflow, restarts
only its controller and tests controlled initial approval. Failed fixtures remain
for inspection. Offline tests alone cannot prove CEL, authentication, real
controller capacity, durable evidence or consumer product acceptance.
