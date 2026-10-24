# FleetIQ full-installation capacity transition

- Status: Resource design approved; execution blocked on prerequisites and tooling
- Design approval: 2026-10-24

## TL;DR

- The owner approved the [resource ledger](../capacity/fleetiq-full-installation.yaml):
  4 CPUs/6Gi control plane, 4 CPUs/14Gi worker; minimum engine eight CPUs/24Gi.
- Preserve the retained foundation. The current profile is not an upgrade switch;
  no engine resize, node change, state rewrite or Stage B activation is authorized.
- Resolve host eligibility, implement an explicit transition and qualify it on a
  disposable target before requesting retained-target execution approval.
- Fresh full-profile lifecycle and explicit retained transition tooling are
  implemented. [Use the transition procedure](../retained-profile-transition.md);
  live disposable/full-installation qualification and retained execution remain pending.
- D06 remains incomplete until provider handoff and the consumer's persistent
  installation acceptance pass. This document is a plan, not a live runbook.

## Target and authority

The retained target is `fleetiq-local`, Docker context `desktop-linux`, Kubernetes
context `kind-fleetiq-local`, HTTPS port `18443`. Its predecessor profile is
`isolated-network-local`; the planned successor is `isolated-network-full-local`.
The operator supplies the protected existing state root. Keep private journals,
kubeconfig, original config bytes and credentials outside Git.

The current-envelope repair and the larger design are separate approvals. Repair
corrects scheduling under the old quotas; it does not qualify the full stack.
Before transition, prove the original ownership claim, cluster incarnation,
Docker endpoint, node container IDs, Kubernetes UIDs, current profile digest,
actual resources and foundation inventory against protected evidence. Do not
substitute a matching cluster name for identity.

[ADR 0009](../decisions/0009-explicit-retained-profile-transition.md) proposes the
transition contract. Its implementation and exact live execution require further
review; design approval does not grant either operation implicitly.

The [minimal disposable profile](../retained-profile-transition.md#minimal-disposable-testing)
can exercise the transition/recovery protocol on a four-CPU engine. Its evidence
must remain separate from full-profile qualification; it does not satisfy the
host/engine or Stage B gates below.

## Gate 1: Eligible host and engine

| Requirement | Required evidence before execution |
|:--|:--|
| Engine CPU | At least eight available logical CPUs; CPU quota totals do not create processors |
| Engine memory | At least 24Gi, including 4Gi above the 20Gi node-cap total |
| Host memory | At least 32Gi installed and 24Gi actually available at the idle window |
| Backing storage | At least 30Gi free on the physical volume backing Docker and sufficient guest space |
| Other projects | Inventory running and stopped owned clusters; agree on shared-engine effects with their owners |
| Stable engine capacity | Record exact CPU/memory observations used to derive kubelet reservations |

The planning inspection on 2026-10-24 found four logical host CPUs and a
four-CPU/about-15.4Gi engine. That target cannot satisfy the approved engine CPU
minimum. Additional free RAM or disk does not remove this blocker. Recheck all
values immediately before proposing execution; a dated observation is not a
standing capacity guarantee.

Select a capable host/engine before live work. If that requires moving to another
machine, stop the in-place path and review a separate relocation and recovery
plan. Do not copy owner state to adopt new nodes, delete the retained foundation
or silently replace it. A smaller or CPU-overcommitted profile requires a new
resource-design decision and full qualification; it is not this approved design.

Engine resizing can change raw capacity for every kind node. Existing reservations
may then produce the wrong scheduler envelope even with unchanged Docker caps.
The engine-change plan must cover affected clusters and explicit revalidation or
repair with each owner's approval. Perfeng and retained qualification targets
are not spare capacity to reclaim automatically. Engine restart, resize, cleanup
and host relocation are outside the retained node-transition authorization.

## Gate 2: Reviewed profile and complete accounting

Preserve the approved ledger's immutable source basis. Re-render a selected,
reviewed provider/chart revision and reconcile any differences in a subsequent
review rather than treating older estimates as current measurements.

- The new independently versioned profile supports fresh creation, ownership
  validation, health, networking and capability reporting for its node budgets
  and HTTPS contract. Existing profile bytes and digests remain unchanged.
- Prove allocatable **3750m/5532Mi** on the control plane and **3750m/13724Mi** on
  the worker. Preserve raw machine capacity as a separate observation.
- Account for actual system Pods, init maxima, restartable sidecars, Pod overhead,
  provider/app/database placement and rollout overlap. Bound or measure unbounded
  CNI init peaks; the ledger's contingency is not a demonstrated upper bound.
- Reconcile normal, maintenance and release manifests and namespace quotas. Limit
  release Jobs to two concurrent 512Mi Jobs; no unreviewed replicas or controllers.
- Measure worker CPU throttling, API/leader stability and product latency. Steady
  CPU limits already exceed four worker CPUs; request fitting is insufficient.

Any unaccounted workload or unsupported resource semantics blocks acceptance.
Do not relax strict health or reservation checks to make a new profile pass.

## Gate 3: Explicit retained transition protocol

The existing repair command is not a migration command. Its fixed
`/var/lib/kubelet/config.before-allocatable.yaml` backup must survive. Its refusal
of unexpected reservations and stale temporary paths must remain intact.
Future tooling must implement these independently recoverable phases:

| Phase | Required behavior and completion evidence |
|:--|:--|
| Read-only preview | Bind exact source, predecessor/successor digests, incarnation, IDs/UIDs, engine observations, caps, expected scheduler envelopes, mutations and bounded restarts; refuse unknown ownership or incomplete prior operations |
| Private preflight | With explicit authority, inspect both bounded configs and all versioned backup/temp paths before writing; verify the known old reservation policy against its recorded capacity and budget; refuse drift |
| Prepare | Atomically persist a private operation journal and predecessor evidence; use unique operation-specific backups without overwriting original repair backups; prove recovery artifacts readable and hash-matched |
| Enlarge and configure | Work on one original node at a time; raise only its approved enforced Docker budget before increasing allocatable; hash-guard config replacement using the new observed capacity; preserve unrelated settings |
| Restart and verify | Bound each kubelet restart, verify same container/node identity, exact caps and allocatable, foundation readiness and pressure; stop before the second node if the first fails |
| Finalize | Verify both nodes and preserved inventories; commit successor ownership through a recoverable commit boundary, retaining predecessor history; validate normal health and capability handoff against the new profile |

The journal must record transition ID, reviewed revisions/digests, old/new budgets,
original IDs/UIDs, config hashes, backup references, engine observations and phase
outcomes without exposing credentials or configuration contents in public output.
Define how crash recovery distinguishes a prepared operation, a completed live
mutation and ownership finalization. Ordinary ensure/health/repair must refuse an
unfinished transition, rather than adopting it or silently rolling it back.

Preserve cluster incarnation, original containers/nodes, namespace identities,
API endpoint and HTTPS mapping, etcd and storage bindings. Inventory PV/PVC,
CNPG and consumer desired state before approval. This first path requires the
foundation-only condition: no Argo/application/database activation. If that
condition changes, stop and review workload maintenance and data recovery.

## Gate 4: Disposable qualification

Prepare an exact preview and obtain separate approval for a disposable target
on the eligible engine, including temporary bootstrap demand, resource envelope,
private reads/writes, restart/failure injection, fixture cleanup and retained
failure state. Do not reuse or delete retained qualification clusters implicitly.

Prove both fresh full-profile creation and transition from an equivalent repaired
small foundation. Exercise positive scheduling controls and CPU/memory rejection
on both nodes, node identity, idempotence, stale/hash-mismatched backup refusal,
interruptions between journal/write/restart/finalization phases and explicit
recovery. Failure injection must not target the persistent foundation.

Then qualify the complete bounded provider/consumer composition under cold start,
release overlap, restart and pressure observation. Record actual placement,
requests/limits, init/sidecar peaks, throttling and bounded readiness/acceptance
results. Generic provider probes belong here; FleetIQ credentials, application
migrations and product release acceptance remain consumer-owned. If complete
consumer qualification is staged separately, retain that gate as pending; do not
label a provider-only drill as full-installation capacity acceptance.

## Gate 5: Retained execution and Stage B handoff

After merged tooling and successful relevant disposable evidence, prepare a new
exact retained-target approval request. Include immutable provider revision,
old/new digests, original node IDs/UIDs, journal/backup locations, measured engine
and host headroom, workload inventory, quiescence, bounded restarts, disruption,
stop conditions and recovery authority. No live execution follows merely from
merging the plan or the profile implementation.

After the approved transition passes, fleetiq-deploy must pin the merged qualified
provider, consume its verified capabilities and re-render immutable chart inputs
for actual placement, quotas, destinations and normal/maintenance release phases.
Refresh release verification and private TLS/OIDC/registry/GitOps prerequisites.
Request a separate Stage B approval for delegated Argo and consumer installation.
The reviewed Git revision, migration authorization, trusted HTTPS/OIDC sign-in,
network denial, database persistence and runtime-privilege checks remain required.
Retain local storage does not prove recovery from node/host loss; off-host backup
and restore remain the separate D07 boundary.

## Implementation sequence and merge gates

| PR-sized slice | Scope and verification before merge/use | Dependency |
|:--|:--|:--|
| Full-profile lifecycle (implemented; unqualified) | Implement the versioned profile across fresh creation, identity, health, rendering and handoff; retain existing profiles; test exact accounting and rejection; run `make validate` | Approved design; live use still blocked on Gate 1 |
| Retained transition capability (implemented; unqualified) | Implement the bounded journaled protocol, private versioned backups, preflight and explicit recovery; test ownership drift, races, stale paths and every interruption boundary, including real Linux shell fixtures; run `make validate` | Full-profile lifecycle (implemented; unqualified) |
| Qualification and operational procedure | Exercise the reviewed disposable transition and complete capacity envelope under separately authorized live scopes; document evidence requirements and tested recovery; run `make validate`; keep unproven gates pending | Both provider slices and eligible host; consumer full-stack accounting may progress in parallel |
| D06 consumer handoff | In fleetiq-deploy, pin qualified merged provider, refresh render/quotas and release evidence, prepare exact retained transition and Stage B requests, then record separately authorized results; pass affected deployment checks | Qualified provider and resolved host/identity prerequisites |

Host selection and consumer manifest/accounting review can proceed alongside
provider implementation. Retained transition is serial after disposable proof;
Stage B follows the qualified handoff, not the plan's merge. None of these slices
is a commitment to recreate or relocate the persistent target.

## Failure, recovery and rollback

- Missing host capacity, changed engine observations, ownership drift, unsupported
  accounting or foreign reservations: refuse before writes and refresh the plan.
- Existing backup/temp or incomplete journal: preserve bytes, stop and inspect;
  never remove a guard file or rewrite owner state to force progress.
- Partial enlargement/configuration/restart or ownership finalization: stop,
  retain private evidence and mark transition incomplete. Recovery must use exact
  recorded identities and hashes under a new bounded approval, not blind retry.
- Rollback is not automatic. Reducing caps requires proving current Pod requests,
  actual memory demand and foundation stability fit the old envelope; restoring
  files alone does not prove that. Any rollback is separately reviewed.
- Retain all original recovery material and the cluster on failure. No force
  deletion, finalizer edit, node replacement or engine restart is an escape hatch.
