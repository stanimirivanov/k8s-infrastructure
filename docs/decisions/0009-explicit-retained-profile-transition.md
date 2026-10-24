# ADR 0009: Explicit retained consumer profile transitions

- Status: Proposed
- Date: 2026-10-24

## TL;DR

- Resource design approval does not authorize live enlargement or change ownership.
- Keep existing profiles immutable; require a separately implemented transition
  between exact reviewed profile digests on positively identified original nodes.
- Preserve prior backups and record bounded migration phases for explicit recovery.
- Qualify the complete profile on a disposable target before retained activation.

## Context

The owner approved the FleetIQ 4-CPU/6Gi control plane and 4-CPU/14Gi worker
resource design. The retained foundation uses 2-CPU/4Gi nodes and already has
original kubelet backups from its completed scheduler-envelope repair. The
design was non-selectable and unqualified at approval. Fresh-target lifecycle
implementation is the first step; live qualification remains separate. The current
repair command checks the current profile, refuses foreign reservations and refuses
stale backup paths. It cannot be reused as an enlargement command.

The selected engine must expose at least eight CPUs and 24Gi. Changing engine
capacity also changes kubelet's raw machine observations for other capped
clusters, even when their Docker quotas stay unchanged.

## Decision

Introduce a versioned full-size profile and a distinct transition capability in
future reviewed implementation. Preserve normal ensure, health and repair
refusal behavior. A transition requires an exact predecessor profile digest,
successor digest, source revision, target incarnation and original node/container
identities. The operator approves its exact private-config, resource and restart
scope after a read-only preflight.

Keep the predecessor ownership claim and original backups. Use operation-specific
private backups and a durable phase journal, with hash-guarded writes and bounded
restarts. Do not allow ordinary lifecycle operations to treat an interrupted
transition as a healthy old or new profile. Finalize the new ownership claim
only after both nodes' enforced resources, scheduler envelopes and foundation
readiness pass; retain an auditable predecessor and recover the commit boundary
explicitly after interruption.

This ADR proposes the migration protocol; it does not supply an executable
command or approve a particular live transition. The
[transition plan](../migrations/fleetiq-full-installation-transition.md) defines
implementation and qualification gates.

## Alternatives

- Changing a profile flag or editing the owner record bypasses identity checks.
- Reusing the repair backup path loses the original recovery boundary.
- Silently changing the existing profile digest breaks retained ownership.
- Recreating the cluster loses identity and may lose local retained bytes.
- Lowering the approved minimum to fit the present host requires a new design
  decision and qualification; it is not an implementation workaround.

## Consequences and compatibility

Existing profiles, repair paths and consumers remain unchanged. The new profile
must remain unavailable until its creation and lifecycle checks are implemented.
In-place migration and relocating to another host are different capabilities;
a relocation must not copy ownership journals to adopt unrelated node identities.
No full-stack performance or recovery claim follows from arithmetic headroom.

## Security and operations

Use a capable host with an approved idle window. Assess engine-wide effects on
all owned clusters with their owners before any shared-engine change. Transition
one node at a time, retain private configs locally and publish only bounded,
non-secret evidence. Stop on identity drift, pressure, timeout or partial failure.
No automatic rollback, backup deletion, cluster replacement or application
activation is included. Recovery is a separately approved exact-target operation.

## Validation

Retain sizing and non-selectability checks now. Future implementation must prove
ownership rejection, versioned no-clobber backups, interrupted writes/restarts,
phase persistence, finalization recovery and identity preservation. Disposable
qualification must exercise actual allocation, scheduling, full-stack startup,
release overlap, throttling and restart behavior before retained use. Consumer
release acceptance remains in fleetiq-deploy.
