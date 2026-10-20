# Delegated Argo capabilities for isolated consumers

- Status: I02; report live qualification separately for each selected revision
- Profile: `local-kind/isolated-argo-local` (explicit 4-CPU/8-GiB selection)

## TL;DR

- Select an owned I01 target and public reviewed delegation explicitly.
- Consumers bootstrap/own their namespaces; infrastructure only installs
  isolated controllers/shared APIs and binds reviewed permissions.
- Preview is offline. Apply, health, qualification and removal require explicit
  execution and authorization for the exact target.
- Native admission constrains submission; approvers clear initial suspension,
  not arbitrary workflow nodes or request fields.
- Sign-in, step approvals, durable evidence, networking/TLS and consumer product
  acceptance are separate qualifications.

## Namespace and ownership contract

For `fleetiq`, system namespaces are `fleetiq-gitops-system` and
`fleetiq-workflow-system`; the consumer-owned managed namespace is
`fleetiq-releases`. I01 supplies exact Docker context, private kubeconfig, node IDs
and cluster incarnation. Existing Perfeng entry points/settings are unchanged.

Consumer bootstrap creates application/release namespaces labeled both
`app.kubernetes.io/managed-by: fleetiq` and
`k8s-infrastructure.io/consumer: fleetiq`. The provider checks these labels and
never creates/adopts those namespaces. Consumers reserve the provider's delegated
Role/Binding/account names and must not reconcile their fields. System resources
have provider ownership and delegation fingerprint; shared APIs have one provider
owner rather than an individual consumer owner.

Example public, credential-free, operator-reviewed delegation:

```json
{
  "consumer": "fleetiq",
  "workloadNamespaces": ["fleetiq-apps"],
  "approvedTemplates": ["reviewed-release-v1"]
}
```

Only listed application namespaces get CD reconciliation grants. Additional
database/Gateway CRDs need reviewed delegation when I03/I04 supply them. CD may
reconcile application Secrets there; Workflows controllers/executors cannot read
those namespaces. Later artifact/evidence credentials belong to the consumer's
release boundary, not an application database credential reused for recovery.

## Commands and offline preview

Use an operator-private state root and reviewed delegation file outside Git.
After I01 qualification and approved consumer namespace bootstrap, preview:

```sh
uv run --locked python -m scripts.delegated_argo apply \
  --profile isolated-argo-local --consumer fleetiq --docker-context default \
  --state-root ../fleetiq-state --delegation ../fleetiq-delegation.json
```

Select the exact local Docker context on your host. Without `--execute`, no
process, private ownership read, download, cluster query or state write occurs.
Preview names target, namespaces, authority fingerprint, pinned assets and effects.
After separate live authorization, add `--execute`. `health`/`handoff` use the same
arguments and require `--execute` to contact the target read-only. Commands have
explicit timeouts and private child-output handling; no remote shell is executed.

## Apply and health

Preflight checks base health, consumer namespace delegation and all existing
desired resource ownership. Foreign objects, incompatible schemas or changed
authority fingerprints stop before writes. Missing shared APIs are established,
then native admission is installed/type checked before granting release roles.
Remaining resources use server-side apply without force-conflicts. Secret
observation requests metadata only; generated credentials are never exported.

Health checks owned inventories, shared compatibility, arguments/images, placement,
limits, config, roles/bindings, admission status and nine controller rollouts.
Handoff reports source revision/dirty status, namespaces, instance ID, resource
envelope and unqualified capabilities. Rollout is not authorization/execution
qualification; dirty source is not a reviewed release.

## Submission, approval and identity

| Provider account in managed namespace | Authority |
|:--|:--|
| `release-viewer` | Read workflows/templates |
| `release-releaser` | Create constrained suspended requests; read approved template names |
| `release-approver` | Read requests; patch only initial suspension from true to false |
| `release-executor` | Create/patch workflow task results; no Secret/Pod administration |

Approved templates are trusted consumer code. They must select the minimal
executor and cannot change spec in place. A new version needs a new reviewed name
and explicit delegation transition. Artifact/evidence interfaces may be defined by
approved templates, never by submitter overrides.

A submitted Workflow has the consumer instance label and an annotation
`k8s-infrastructure.io/submitted-by` equal to the authenticated releaser's username,
and exactly this spec:

```yaml
spec:
  workflowTemplateRef:
    name: reviewed-release-v1
  arguments:
    parameters:
      - name: release-digest
        value: sha256:1111111111111111111111111111111111111111111111111111111111111111
  suspend: true
```

Only the named releaser may create it. Native admission rejects extra fields,
artifacts/valueFrom, arbitrary metadata, unapproved/cluster templates and
account/entrypoint/volume/pod overrides. Secure referencing is an additional
upstream check, not the entire submission boundary.

The complete forbidden-field list comes from the pinned Workflow CRD's closed
structural schema, including nested reference/argument/parameter and initial
status fields. New upstream override fields are denied automatically; an open
schema is incompatible and stops rendering. The installer also requires native
CEL type checking to finish without warnings before granting release roles.

An authenticated approver merge-patches `{"spec":{"suspend":false}}` together
with the single new annotation `k8s-infrastructure.io/approved-by` equal to its
authenticated username. Admission verifies both audit identities; callers cannot
forge them. Request, digest, status and other metadata remain unchanged. There is no
arbitrary workflow/status or step-level resume authority. Consumer orchestration
must separately implement/qualify approval at irreversible steps before production.

Use authenticated users/automation and short-lived Kubernetes TokenRequest
credentials; issuance belongs to the operator/identity platform. No permanent
service-account token Secrets are created. Only the executor automatically mounts
its minimal token in Pods. Audit identities and approval/run outcomes must reach
consumer durable evidence; local records are not a backup/audit service.

CD requires revision-override privilege and explicitly denies it to release roles,
along with Application create/update/delete. Consumers own AppProject restrictions
and approved desired revisions. Do not grant direct Application patch or broader
credentials. Default admin/anonymous access is disabled; trusted sign-in/expiring
Argo API identity delivery must be configured/qualified before API/UI use. No
usable login or SSO trust is created here. Perfeng settings/access are unchanged.

## Native disposable qualification

Authorize exact cluster/context/state, neutral namespaces, controllers/APIs,
admission, workflow fixture/restart, removal and disposal. Coordinate an idle host
window: initial bootstrap is uncapped, and clusters share host/engine resources.

Include `provider-qualification-v1` in the disposable approved-template list,
install, then run `qualify --execute` with the same arguments. It explicitly
creates a named neutral template/workflow; it never applies/overwrites consumer
templates. Repeating it requires separately inspected fixture cleanup or another
disposable target. Failures retain fixtures and partial resources for diagnosis.

Qualification checks real SubjectAccessReviews, valid server submissions and
denied overrides, creates a suspended request, restarts only its controller,
rejects request-changing approval, and executes under `release-executor`. Report
actual outcomes separately from health. It does not qualify Argo authentication,
per-step approvals, product workflows, durable archive or production capacity.

## Removal and recovery

Preview `remove`; authorized execution additionally requires
`--confirm-remove fleetiq-local/argo`. Nonterminal workflows cause refusal.
Bindings are revoked first, then other non-secret provider resources are deleted
with server-checked UID preconditions. A replacement cannot be deleted by stale
name inspection. Removal never sweeps a namespace or deletes shared APIs. It
retains namespaces, Secrets, consumer templates and execution/evidence records.
Inspect asynchronous deletion. Cluster-local retained data is destroyed only by
separately authorized I01 disposal.

Admission permits the authenticated Kubernetes garbage collector to remove only
the system `foregroundDeletion` finalizer from an already deleting workflow,
preserving its request, status and audit metadata. It cannot edit a release
request. Do not force-remove workflow finalizers to work around a policy defect.

- **Namespace/owner mismatch:** correct approved consumer bootstrap or plan an
  authority transition; never relabel/adopt implicitly.
- **Shared schema mismatch:** identify all users and review an API upgrade;
  never apply competing versions or delete CRDs as repair.
- **Admission warnings/pending:** stop before release roles; inspect compilation.
  Do not disable failurePolicy or change to audit-only.
- **Apply/rollout/capacity failure:** retain state, diagnose and retry identical
  reviewed inputs. No forced conflict, limit increase, rotation or auto-delete.
  First-time shared APIs are installed sequentially, with a bounded 120-second
  CRD request window for schema compilation on a bounded local control plane.
  A timed-out request may already have created an API; retry verifies its owner
  and complete schema before reuse rather than assuming the write rolled back.
  Use the explicitly selected `isolated-argo-local` base profile (4 CPUs/8 GiB
  total) for Argo qualification. The smaller I01 base profile is retained, but
  native schema discovery exceeded its control-plane envelope. Never raise live
  limits implicitly or rewrite an existing owner record to change profiles.
- **Lost controller:** preserve consumer records, restore exact resources and
  qualify recovery; do not borrow another consumer's credentials.
- **Command deadline/Docker engine unavailable:** owned subprocess trees are
  terminated and private partial state is retained. Killing a Docker client does
  not prove rollback of an accepted daemon request. Inspect the exact target
  before retry/cleanup. Restore a shared engine through its operator; never
  restart it automatically or stop other consumers to make a test pass.

## Verification and limits

`make validate` runs offline policy/behavior and advisory checks.
`make delegated-argo-asset-check` verifies official hashes without cluster access;
the existing CI asset job includes it alongside legacy checks. Network asset
verification stays separate from offline acceptance. The setup-uv SHA stays the
owner-approved exception to major-version action references.

See [ADR 0005](decisions/0005-delegated-consumer-argo.md) and
[I01](isolated-local-consumers.md). I03 networking/trusted ingress and I04 storage
remain separate prerequisites. Provider checks do not complete FleetIQ D06
installation or D08 release execution acceptance.
