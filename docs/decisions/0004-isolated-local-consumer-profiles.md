# ADR 0004: Isolated local consumer profiles

- Status: Proposed
- Date: 2026-10-19

## TL;DR

- The provider is shared by projects; local clusters need not be shared.
- Preserve the selected Perfeng compatibility profile and add a project-neutral
  isolated profile selected by consumer ID.
- Bind lifecycle operations to explicit Docker/state/context identities and
  observed node IDs and cluster incarnation; never adopt by name alone.
- Base readiness does not qualify tenancy, networking, storage or production use.

## Context

Perfeng was the first consumer. Its reserved generator/SUT workers and immutable
provider selection cannot safely become the implicit placement contract for
FleetIQ or future projects. No inspected evidence establishes spare reserved
capacity, cross-consumer policy enforcement or benchmark performance isolation.

The consumer guide records the Perfeng cutover as accepted on 2026-09-17 and its
lock selects provider `5970bffa1f3d200e73d5a55e0cd4ef34ed9ef3b7`. Updating this
provider's working tree does not update that selection or change live ownership.

## Decision

Add `local-kind/isolated-local`, with one control plane and one worker, selected
through explicit consumer ID, local Docker context and existing private state
root. Names derive from the consumer ID; the Perfeng identity is reserved.
The original profile and entry point retain their compatibility contract.

Use a private exclusive ownership record containing canonical profile identity,
Docker endpoint, immutable node container IDs and the observed `kube-system` UID.
Verify these before reuse or removal. Preview performs no process or private-file
read. Create refuses existing identities. Partial creation retains state; only
positively verified targets can be removed with exact confirmation.

Bound owned node CPU/memory/PIDs after kind bootstrap and before reporting base
readiness. Check engine minimums first. Initial kind bootstrap is transiently
uncapped by this mechanism; it has a bounded command deadline and requires a
reviewed host resource window. Report node allocatable and container limits
separately: container limits are not scheduler reservations or host isolation.

Keep the human-readable v0 capability handoff. The observation JSON is a local
CLI report, not a new cross-provider schema or an admission token.

## Alternatives

- Reuse Perfeng workers: deferred until capacity, placement, isolation and
  compatibility are qualified independently.
- Duplicate one lifecycle per project: rejected because target safety and
  recovery semantics would drift.
- Introduce a universal provider framework: deferred until real implementations
  demonstrate common semantics beyond this local-kind capability.

## Consequences and operations

Isolated clusters preserve consumer Kubernetes identities and avoid reserving
Perfeng nodes, but share physical host/engine resources. Coordinate benchmark
windows or use independently qualified separate engines/hosts; this profile
does not claim performance or hostile-tenant isolation. Local kind nodes are
privileged infrastructure containers, unsuitable as a production security claim.

Consumer namespaces, workloads, credentials, controllers, ingress, database and
backup capabilities remain separate. I02-I04 must qualify their own boundaries.
Removing a cluster permanently loses cluster-local data; no automatic rollback
or partial-state adoption is provided. No cloud resources or paid fallback exist.

## Validation

Existing Perfeng compatibility tests remain required. New fixtures exercise
offline previews, context redirection, foreign/stale identity, node pressure,
resource limits, partial failures, interrupted removal and cross-consumer state
preservation. Configuration validation and CI include the new profile.
Live acceptance uses an explicitly authorized disposable target; offline tests
do not establish that live gate.
