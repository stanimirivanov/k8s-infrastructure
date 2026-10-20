# ADR 0007: Explicit local database operators and retained storage

- Status: Proposed
- Date: 2026-10-20

## TL;DR

Provide pinned CloudNativePG, Barman Cloud Plugin and cert-manager on the isolated
network substrate. Qualify a non-default Retain class backed by kind local storage.
Keep consumer databases/credentials and backup/restore acceptance consumer-owned.
Report node-local limitations rather than treating retention as disaster recovery.

## Context

Independent consumers need persistent PostgreSQL and supported plugin APIs before
performing their own release and recovery drills. Controller health alone does not
prove persistence, retention, remote backup or identity. Existing kind storage is
dynamic but its default Delete policy does not preserve bytes after claim deletion.

## Decision

Install SHA-verified supported upstream CNPG/Barman/cert-manager resources, with
immutable runtime images and bounded single-replica controllers. Use cert-manager
for the supported plugin certificate lifecycle. Provider namespaces, shared APIs,
controller permissions and `local-retained` belong to infrastructure. Convert the
upstream public sidecar-image configuration to a ConfigMap; never inventory
consumer credential bytes. Preserve original profile and delegation behavior.

Use kind's existing local provisioner without modifying or adopting it. Add a
named non-default RWO/WaitForFirstConsumer/Retain class, no expansion. Report live
free filesystem capacity separately from admission request accounting and its
lack of physical directory quotas. A retained PV remains dependent on its node.

Database namespace authority is explicit opt-in, with native storage admission and
bounded request quotas. Consumers retain Cluster/Backup/ScheduledBackup/ObjectStore
desired state, credentials, PVC selection and recovery acceptance. Opt-in changes
the reviewed delegation digest; it never silently widens an existing installation.

## Alternatives

- A standalone PostgreSQL StatefulSet avoids operators but duplicates supported
  lifecycle/backup machinery and does not establish the required plugin APIs.
- Deprecated in-tree object-store backup is not a substitute for plugin support.
- A distributed CSI/object-store installation would add topology and operational
  choices without qualifying off-host durability on one Docker host.
- Treating kind's Delete class as persistent would hide claim-deletion loss.

## Consequences and compatibility

Five provider controller Pods are added only by explicit installation on the
network profile. Original profile identities and unopted-in Argo roles remain
unchanged. Differently owned or selected resources and incompatible schemas fail
before writes; upgrades require a reviewed transition, never forced adoption.

Retain preserves bytes through claim garbage collection, not through node/cluster
destruction. Consumer Argo prune protection and explicit Released-PV recovery remain
necessary. A recovery workflow must not keep its only metadata in the database
being restored. Provider configuration belongs in Git; protected ownership/access
state and consumer external backup/evidence require independent recovery custody.

## Security, cost and operations

Use exact owned targets, bounded commands, native controller/API readiness and
namespace-scoped permissions. Provider controllers are trusted infrastructure;
consumer roles do not gain shared API, PV, issuer or operator authority. Request
quotas do not constitute physical disk reservations or hard filesystem limits.

The local capability creates no consumer bucket, credential, backup schedule,
production identity, external trust enrollment or billable infrastructure. No
existing target is replaced or converted implicitly. A separately authorized
whole-target destroy permanently removes local data. Never force finalizers or
delete shared APIs as ordinary cleanup.

## Verification

Run repository acceptance and the pinned database asset gate. On an authorized
disposable target, prove safe storage admission, actual provisioning, SQL data
after Pod replacement, data while the operator is absent, and retained marker
recovery after owner/PVC deletion with exact UID checks. Report readiness and
native persistence separately from the consumer's seeded backup/restore drill.

See the [operating guide](../database-and-storage.md) for supported versions,
commands, handoff limitations and recovery boundaries.
