# Local database and retained storage capability

## TL;DR

- The provider owns CloudNativePG, Barman Cloud Plugin, cert-manager and a named
  Retain StorageClass. Consumers own databases, credentials and recovery drills.
- Install explicitly on an owned `isolated-network-local` target. An absent or
  incompatible capability fails; installation does not adopt a foreign operator.
- Opt database namespaces into native storage admission and request quotas before
  granting Argo database authority. Existing delegations keep their permissions.
- Retain preserves a local PV after PVC deletion. It does not preserve data after
  kind node/cluster destruction, reserve disk space, or provide off-cluster backup.
- Controller health, native persistence qualification and consumer backup/restore
  acceptance are different results. Never infer one from another.

## Ownership and supported inputs

The [release inventory](../clusters/local-kind/isolated-network-local/database/releases.json)
selects CloudNativePG 1.30.1, Barman Cloud Plugin 0.15.1 and cert-manager 1.21.2.
These releases support the profile's Kubernetes 1.35; the supported plugin
installation requires cert-manager. Official manifest bytes are SHA-256 checked
before parsing. Controller, runtime helper and neutral PostgreSQL images carry
immutable digests. The operator's own generated init-container image and Barman's
sidecar-image configuration use those same selected digests.

Infrastructure owns shared CRDs, webhooks, controllers, provider namespaces and
`local-retained`. It uses kind's already installed local provisioner without
adopting or changing its setup/teardown configuration. Default `standard` remains
a kind-owned Delete class; database opt-in requires an explicit retained class.

Consumers own `Cluster`, `Backup`, `ScheduledBackup`, Barman `ObjectStore`, database
and application identities, PVC selection, Secrets, schedules, retention settings
and recovery acceptance. Installing the capability creates none of those consumer
objects. The upstream Barman Secret containing only the public `SIDECAR_IMAGE`
setting becomes a ConfigMap; runtime certificates remain controller-generated
Secrets and are never inventoried or exported.

cert-manager's three controllers run on the control-plane node; CloudNativePG and
the plugin run on the worker. Five single-replica controllers request 300m CPU and
448Mi memory, in addition to networking and kind's existing components. Individual
CPU/memory limits are explicit in the renderer. This fits the reviewed 4-CPU/8-GiB
node-container envelope; it is not a production sizing or isolation claim.

## Preview, installation and health

Read the [networking guide](networking-and-ingress.md) first. Create the explicit
network profile and prove its base readiness before adding database capabilities.
The private state root must already exist as an ordinary, operator-controlled
directory. Preview is offline and never reads kubeconfig or queries a cluster:

```sh
uv run --locked python -m scripts.database apply \
  --profile isolated-network-local --consumer example \
  --docker-context desktop-linux --state-root ../example-private --https-port 18444
```

Review target, versions, namespace/cluster API effects, resource envelope and data
disposal. Obtain explicit authorization under CONTRIBUTING, then repeat with
`--execute`. Use the same flags with `health` or `handoff --execute` for read-only
observation. Only `apply` can resume an inspected partial install. It verifies
identity, shared schemas and resource ownership first; it never forces conflicts
or implicitly upgrades an existing differently selected installation.

Installation orders cert-manager APIs/controllers before CloudNativePG and the
plugin. It waits for CRD establishment, native rollout and plugin certificates.
The report separates readiness from unexecuted persistence, backup and identity
checks. `make database-asset-check` verifies actual pinned source inventories
without Kubernetes; the existing CI asset gate includes this command.

## Explicit database namespace delegation

The consumer first creates its namespaces with the exact
`k8s-infrastructure.io/consumer: example` label. Select database authority when
preparing the initial public Argo delegation file:

```json
{
  "consumer": "example",
  "workloadNamespaces": ["example-app", "example-db"],
  "approvedTemplates": ["release-approved"],
  "databaseNamespaces": ["example-db"]
}
```

Run database `apply --delegation <file> --execute` with the same explicit target.
It creates provider-owned quotas and fail-closed native admission for the selected
existing database namespaces. Then use that file for
[delegated Argo installation](delegated-argo.md). Before granting database roles,
Argo installation verifies operator health and native namespace enforcement.
`databaseNamespaces` must be a distinct subset of `workloadNamespaces`; omission
preserves the old delegation digest and permissions. Opt-in changes the digest:
changing an already installed delegation requires a reviewed authority transition,
not relabeling or forced adoption.

Argo gets namespace-scoped CRUD for CNPG Cluster/Backup/ScheduledBackup and Barman
ObjectStore only in selected namespaces. This does not delegate shared CRDs,
StorageClasses/PVs, admission, certificate issuers, or provider controller roles.
The provider does not read consumer Secret payloads to qualify the capability.

| Storage property | Local capability |
|:--|:--|
| Explicit class | `local-retained`, not a new default |
| Access | ReadWriteOnce; one node-local volume, not replication |
| Binding | WaitForFirstConsumer; preserve actual PV node affinity |
| Reclaim | Retain: deleted claims leave Released PVs/data for inspected recovery |
| Expansion | Unsupported |
| Opt-in requests | Whole 1–4Gi per claim; 8Gi requests/four claims per namespace |
| Physical enforcement | Requests/quotas are admission accounting, not directory hard quotas |
| Available capacity | Observed backing-filesystem free bytes; shared, unreserved and changeable |
| Failure domain | One kind node on one Docker host; node or host loss can lose data |

Separate typed admission policies validate PVC and Cluster storage.
The PVC resources object needs a narrowly scoped CEL `dyn` lookup: the selected
Kubernetes API publishes that object through an `allOf` reference which its
admission type checker does not resolve. The surrounding PVC stays typed, CEL
warnings still fail health checks, and native dry-runs must accept all four whole
sizes and reject fractional, undersized, oversized, wrong-class and wrong-mode
claims with working positive controls.

Cluster instances are bounded to one or two, and deprecated in-tree Barman configuration
is rejected. Consumers must size other workloads against the actual Docker node
limits; Kubernetes allocatable figures may reflect the larger underlying VM.

## Retention and recovery semantics

A Pod restart reuses its bound PVC. Removing the operator Deployment does not own
or garbage-collect the consumer's Cluster/PVCs. Removing a consumer Application can
still prune a Cluster and its generated PVCs; a Retain PV preserves the bytes but
does not promise that the original PVC object survives. Protect persistent desired
state explicitly, for example with reviewed Argo `Prune=false,Delete=false` sync
options and appropriately separate database Applications.

Released PV recovery is an operator decision. Inspect exact PV UID, old claim UID,
node affinity, class and consumer identity before changing a claimRef. Rebinding
is not ordinary apply or an automatic recovery path. Never reinitialize a database
or clear arbitrary claim references to make a deployment succeed.

The neutral qualification proves owner/PVC garbage collection and explicit
UID-checked rebind of retained bytes. It is not a test that deletes an actual
consumer Argo Application. The consumer must test its own prune/retention policy.

Provider recovery information lives outside the database: reviewed Git revision,
asset inventory and infrastructure configuration, plus a protected private
ownership journal/kubeconfig. Back these up independently when retaining a target.
Lost ownership evidence does not authorize adopting matching container names.
Rebuilding after kind/host loss requires fresh infrastructure and external
consumer recovery records/backups. A local PVC is not that external recovery store.

## Backup, evidence and identity handoff

Use the supported `barman-cloud.cloudnative-pg.io` plugin and consumer-owned
ObjectStore/Backup/ScheduledBackup resources from the selected APIs. The consumer
supplies the destination, verified HTTPS endpoint/CA, namespace credential
references or separately qualified workload identity, schedule and retention.
Infrastructure does not create a bucket, read credentials or schedule backups.

Local operator readiness does not prove remote connectivity, sidecar backup
execution, successful WAL archival, production workload identity or restore.
The consuming deployment must perform its seeded backup/restore drill and retain
release/recovery evidence outside the database being restored. Off-host object
storage, encryption/key custody, production CSI and cloud identity need their
own provider profile and authorization; no paid or local-endpoint fallback is
implicit.

## Native qualification and disposal

Preview `database qualify` with the exact target flags. Obtain authorization for
the complete disposable creation/install/qualification/destruction sequence before
adding `--execute`. It creates one neutral namespace, one single-instance database
and one bounded mount probe, with two 1Gi claim requests. It checks:

1. Accepted valid and rejected unsafe storage requests, with live positive controls,
   and a server dry-run of the ObjectStore API without creating an object store.
2. PostgreSQL write/read after deleting and replacing its Pod with a new UID.
3. Data access while the exact CNPG operator Deployment is removed, then restores
   that same pinned Deployment and proves its readiness.
4. Retained bytes after deleting a neutral consumer owner/PVC, followed by explicit
   recovery using API-server tests of the PV UID and old claim UID.

Readiness deadlines do not qualify cold image download performance. On a
constrained host, inspect slow pulls and cache only the reviewed image digests in
the exact owned nodes before a fresh drill. Record that preparation and any
retries with the evidence; do not relax readiness checks or claim a cold-start
performance result from a run using cached images.

Failures retain fixtures and partial state for inspection. Retry requires an
inspected, authorized fixture reset or a fresh authorized target. A success leaves
fixtures available for inspection and reports unqualified backup/production work.
Whole-target removal uses the [existing exact-ID lifecycle](networking-and-ingress.md#disposal-troubleshooting-and-limitations)
and permanently destroys local data. Retain does not protect against this action.
There is no automatic operator/CRD/PV uninstall or finalizer removal.

## Upstream references

- [CloudNativePG supported releases](https://cloudnative-pg.io/docs/1.30/supported_releases/)
- [Supported Barman plugin installation](https://cloudnative-pg.io/plugin-barman-cloud/docs/installation/)
- [cert-manager supported releases](https://cert-manager.io/docs/releases/)
