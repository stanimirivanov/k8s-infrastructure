# Capped kind scheduling capacity

## TL;DR

- Docker quotas constrain execution; they do not make kubelet's machine capacity
  container-aware. The scheduler uses **Allocatable**, not the raw capacity field.
- New isolated targets configure a safe allocatable envelope before readiness.
  Existing targets require a separately approved explicit repair; health never repairs.
- [The approved FleetIQ resource design](capacity/fleetiq-full-installation.yaml) uses a
  4-CPU/6Gi control plane and 4-CPU/14Gi worker, with an 8-CPU/24Gi engine.
  It is not selectable or live-qualified and does not authorize resizing.
- This change does not mutate Perfeng, existing profile identities or retained data.

## Root cause and policy

The Stage A observations showed 4 CPUs/16155204Ki allocatable on **each** node,
while Docker enforced 2 CPUs/4Gi per node. The former health sensor checked only
non-empty strings. That could admit workloads against the shared VM's resources.

Use the supported kubelet [Node Allocatable mechanism](https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/).
For each positively identified node:

1. Read raw `status.capacity` and verify it is at least the enforced Docker budget.
2. Set `systemReserved` CPU/memory to the inaccessible machine-to-container gap.
   This is a kind-specific accounting exclusion, not memory reserved in another cgroup.
3. Reserve 250m CPU/512Mi for real non-Pod kubelet/runtime/OS overhead with
   `kubeReserved`. Reserve another 100Mi through explicit memory eviction.
4. Enforce allocatable on `pods`; do not attempt to enforce the synthetic
   system reservation on a system cgroup. Keep existing disk eviction thresholds.
5. Require the exact expected allocatable values in health/handoff.

The public [kind v0.31.0 template](https://github.com/kubernetes-sigs/kind/blob/v0.31.0/pkg/cluster/internal/kubeadm/config.go)
sets disk eviction thresholds to zero and omits memory from that nonempty map.
The repair preserves those disk settings and explicitly adds `memory.available:
100Mi`; it does not assume the default map was merged. Public source verification
does not assert that a retained node's private config is unchanged; apply still
needs specific authorization for its bounded private-config inspection/write.

For a 2-CPU/4Gi node the Pod envelope is **1750m/3484Mi**. System Pods (API server,
etcd, DNS, CNI and controllers) consume that envelope alongside consumer Pods.
The kubelet still reports host-derived raw capacity. The handoff labels that
field `rawMachineCapacity` and reports `schedulerEnvelope` separately. It never
patches Node status or asserts that raw capacity represents a dedicated machine.

Budgets, requested resources and node-role placement are separate gates. This
fix does not guarantee that limits cannot contend, that disk requests are physical
quotas, or that a shared-host benchmark is isolated.

## Offline preview and approved execution

Use the same original consumer/profile/context/state/HTTPS arguments as the
owned target. The example uses a neutral target; substitute only reviewed inputs.

```sh
uv run --locked python -m scripts.consumer_capacity apply \
  --profile isolated-network-local --consumer example \
  --docker-context default --state-root ../example-private-state --https-port 18443
```

Without `--execute`, both `apply` and `health` are pure previews: no subprocess,
private-state read, download or mutation. Exact reservations need live capacity;
the preview gives the deterministic Pod envelope and effects, not a fake live plan.

After merge, review an exact target, idle window and kubelet restart scope before
adding `--execute` to `apply`. Quiesce workload scheduling/configuration throughout
the repair window. This initial path refuses consumer Pods or controllers (even
scaled-to-zero controllers) and CNPG database desired state even before Pods exist;
it is for a foundation-only target before Argo/apps/databases.
It checks existing and pending Pod requests against the reduced envelope before
writes, including traditional init maxima and Pod overhead. Advanced Pod-level
resources, resize and restartable init sidecars are refused. This is not an atomic
admission lock or a maintenance mechanism for an active application. Both nodes' owner/context/incarnation/IDs, Docker
caps, Kubernetes UIDs, pressure and kubelet settings are checked. Both configs
are preflighted before the first mutation and rechecked immediately before writes.
The node-local config is bounded and must be ordinary; unrelated settings survive.
Unknown reservations, CPU/memory managers or eviction policy are refused.

Each changed node retains `/var/lib/kubelet/config.before-allocatable.yaml` and
receives an atomic, hash-guarded replacement of `/var/lib/kubelet/config.yaml`,
then a bounded kubelet restart. These files remain private inside the original
node; never export their contents. Failure retains config/backup and stops before
advancing. There is no automatic rollback, node deletion, profile migration or
Docker resize. A stale backup/temp file or changed original requires inspection.
Do not remove a backup or edit the ownership record to force a retry.

An already-correct healthy node is skipped. An already-written configuration
whose restart did not complete can be restarted through the same explicit path.
`health --execute` only observes. New creation runs the same configuration step
between Docker limits and CNI/readiness. No step changes legacy Perfeng provisioning.

## Approved complete FleetIQ resource design

Basis: provider revision `41c558a` rendered 19 provider Pods, with main-container
limits of 1664Mi on the control plane and 5632Mi on the worker. The published
FleetIQ charts at `7c516856` add backend 256Mi, static edge 128Mi and PostgreSQL
1024Mi. These are pinned-source planning inputs, not a workload performance test.

| Item | Control plane | Worker |
|:--|--:|--:|
| Approved design Docker cap | 4 CPUs / 6Gi | 4 CPUs / 14Gi |
| Pod allocatable after daemon/eviction reserves | 3750m / 5532Mi | 3750m / 13724Mi |
| Provider main-container memory limits | 1664Mi | 5632Mi |
| Consumer steady main-container limits | 0 | 1408Mi |
| System Pod allowance | 2048Mi | Included in provider/application rows; inspect actual placement |
| Release Jobs | 0 | 1024Mi: two bounded 512Mi Jobs |
| App/provider/database rollout overlap | 512Mi | 1920Mi: 384 + 512 + 1024 |
| Init contingency (not a proven bound) | 768Mi | 1536Mi |
| Total planning memory | 4992Mi | 11520Mi |
| Remaining allocatable above that plan | 540Mi | 2204Mi |

Steady main-container requests are much smaller: provider 300m/512Mi on the
control plane and 800m/1280Mi on the worker; application/database requests add
400m/384Mi on the worker. Planned request peaks are 2800m and 2900m respectively,
including system/init/surge allowances. CPU limits intentionally oversubscribe:
worker steady main limits already total 10.5 CPUs. Throttling and latency must be
measured; fitting memory is not performance qualification.

Four Cilium init definitions lack individual limits. Contingencies are estimates,
not a mathematical upper bound. Bound or measure those peaks, sidecars, Pod
Overhead and actual system Pods before accepting the profile. Default backend/edge
placement is not guaranteed by this document; re-render consumer placement and
quotas against the approved nodes. Backups, new device workers, replicas or added
controllers require a new ledger.

The design's 20Gi node caps need an engine with at least 8 CPUs/24Gi and a host
with at least 32Gi physical RAM, **24Gi actually available** and 30Gi free backing
storage at the reviewed startup window. Existing project demand counts against
that headroom. These are admission prerequisites, not resource reservations or
permission to alter Docker Desktop. The observed 4-CPU/15.4Gi engine does not meet
them. Stop rather than borrowing another project's capacity.

## Retained-target transition and qualification

1. Merge the provider correction and bind the selected immutable source.
2. Review the [disposable capacity qualification](capacity-qualification.md).
   Authorize its exact target to prove corrected scheduler rejection, kubelet
   restart/idempotence and original node/cluster identity. Legacy retained-target
   repair and injected partial-failure recovery remain separate acceptance work.
3. Separately approve correcting the retained target's **current** envelope.
   Keep its original profile, 2-CPU/4Gi node caps, node IDs and retained storage.
4. The larger resource design is approved; follow the
   [full-installation transition plan](migrations/fleetiq-full-installation-transition.md).
   Host eligibility, a selectable versioned profile and explicit retained-target
   migration remain prerequisites; changing `--profile` must continue to refuse.
5. Re-render full Argo/app/database/release placement, resource quotas and memory
   overlap against the selected profile. Qualify pressure, throttling and API/leader
   stability under cold start, release overlap and restart before Stage B approval.

Design approval does not perform an existing-target repair or enlarged-profile run.
The former permissive health result now fails closed until explicit repair.
Old provider source remains usable for inspection; it is not capacity acceptance.
