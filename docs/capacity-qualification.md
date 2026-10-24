# Disposable capped-node capacity qualification

## TL;DR

- Prove scheduler rejection and restart stability on a separately approved disposable target.
- Preview is offline. Live execution requires exact target/idle-window approval and `--execute`.
- Create the target explicitly first; qualification never creates, adopts or deletes a cluster.
- Successful per-node proofs delete only recorded fixture Pod UIDs; remaining fixtures are retained on failure.
- This proof does not approve persistent repair, resource enlargement or the complete FleetIQ installation.

## Prerequisites and approval boundary

Use the locked toolchain and the [isolated consumer prerequisites](isolated-local-consumers.md).
The selected profile, Docker endpoint, creation journal, original container IDs
and cluster incarnation must agree. Qualification requires healthy nodes with
corrected allocatable and no consumer Pods/controllers or CNPG database desired state.
Only consumer names beginning with `qual-` are accepted. This prevents accidentally
pointing the command at `fleetiq-local`; the prefix alone never establishes ownership.

Review actual free host/engine resources, existing cluster demand and an idle
benchmark window before creation. The example adds two nodes capped at 2 CPUs/4Gi
each after temporarily uncapped bootstrap. It needs **additional** resources while
the retained foundation remains running. The profile's 4-CPU/12Gi engine minimum
does not account for other clusters and is not proof of concurrent capacity.
Do not delete the persistent foundation, resize Docker or borrow another project's
resources to make the example fit. Keep live qualification pending if it does not fit.

Approval covers the exact disposable name, Docker/Kubernetes contexts, state root,
profile, resource envelope, creation-time bounded private kubelet config inspection,
backup/replacement and restart, and the qualification's two kubelet restarts,
reconciliation reads, creation of the exact neutral cluster-scoped PriorityClass,
and neutral Pod creation/UID deletion. The PriorityClass is non-default, has
priority zero and disables preemption; it grants no access and changes no existing
workload or global default. It is retained with the namespace and cluster.
This fixture requires explicit inclusion in any approval issued before it existed.
No retained cluster,
consumer credential, host trust, engine configuration or storage drill is included.
Cluster deletion is separate and requires its exact name and explicit authorization.

## Preview the exact target

Prepare a new operator-private ordinary state directory before preview; `Target`
refuses a missing directory or a redirected path. Verify its inherited Windows
ACL is private. This creates only an empty directory, not a journal or cluster.
Do not reuse another target's state directory.

```powershell
New-Item -ItemType Directory -Path 'C:\Users\sonof\FleetIQ\state\qual-capacity-32'
```

The following PowerShell inputs are a proposed example, not permission to execute:

```powershell
$capacityTarget = @(
  '--profile', 'isolated-argo-local',
  '--consumer', 'qual-capacity-32',
  '--docker-context', 'desktop-linux',
  '--state-root', 'C:\Users\sonof\FleetIQ\state\qual-capacity-32'
)
uv run --locked --offline python -m scripts.consumer_kind up @capacityTarget
uv run --locked --offline python -m scripts.consumer_capacity qualify @capacityTarget
```

Both commands are offline without `--execute`. The second reports the fixture-image
digest, namespace, maximum three concurrent Pods, restart scope and failure policy.
No private ownership file or kubeconfig is read by the qualification preview.

## Execute after named-target approval

Create only the absent disposable target, then qualify it:

```powershell
uv run --locked --offline python -m scripts.consumer_kind up @capacityTarget --execute
if ($LASTEXITCODE -ne 0) { throw 'Creation failed; inspect retained disposable state.' }
uv run --locked --offline python -m scripts.consumer_capacity qualify @capacityTarget --execute
if ($LASTEXITCODE -ne 0) { throw 'Qualification incomplete; retain all evidence.' }
```

The bounded run:

1. Verifies foundation readiness, original creation claim, container identities,
   exact Docker budgets, node UIDs, pressure and safe allocatable.
2. Refuses an existing qualification namespace or same-name PriorityClass before any restart.
3. Restarts each original kubelet, waits for allocatable and base health, and
   confirms unchanged ownership and API node identities. Reconciliation must
   then be idempotent, without another restart or configuration write.
4. Creates `<consumer>-capacity-probe`, a priority-zero, `globalDefault: false`
   PriorityClass with `preemptionPolicy: Never`, verifies its UID/policy, then
   creates one neutral namespace. On each node, starts a 20m/32Mi positive-control
   Pod (100m/64Mi limit), then two sleeping probes requesting respectively one
   millicore or 1Mi above that node's **whole** allocatable envelope. Over-budget
   probes use the scheduler through node selectors; they never set `nodeName`
   or preempt other Pods. Every Pod references the exact class and asserts the
   matching `Never` policy. Admission rejects a raced conflicting class; observed
   Pods must also retain the class name, priority zero and `Never` policy.
   Only three probe Pods coexist at a time.
5. Requires `Pending`, no bound node, and `PodScheduled=False` with reason
   `Unschedulable` and an explicit `Insufficient cpu` or `Insufficient memory`
   message. API failure, missing scheduling evidence, image-pull failure or
   timeout cannot pass. The positive control must be Ready on the same node
   both before and after the denials.
6. Checks original ownership/UIDs before every mutation and deletes only the
   exact created Pod UIDs with server-side preconditions. It retains the empty
   namespace, PriorityClass and cluster, then proves reconciliation remains idempotent.

Each probe has a 120-second observation deadline, each API request is bounded,
and successful Pod deletion has a 60-second wait. Pods have a 600-second active
deadline and no service-account token, host access or privileged capabilities.
Large negative-probe requests are declarations for rejection, not a load test;
a probe binding to any node fails qualification immediately.

## Evidence and recovery

Retain the public final summary from standard output alongside the reviewed
provider revision and approved target. Command progress precedes the final JSON;
do not treat the entire output stream as a standalone JSON document. Evidence
contains cluster incarnation, node UIDs/container IDs, PriorityClass identity/policy,
fixture UIDs, per-resource
scheduler rejection and restart/idempotence outcomes. It contains no kubeconfig,
private kubelet settings or application credentials.

A failed run stops without cleanup or rollback. Inspect the exact recorded
PriorityClass, namespace, remaining Pods and original node-local backup on that disposable target under
its authorized scope. Do not delete a stale backup, alter an owner record, reuse
an existing namespace/PriorityClass or rerun creation to force success. A successful
run also leaves both fixtures to prevent silent adoption on retry.

Whole-cluster disposal uses the original lifecycle command only after exact-target
removal authorization; it permanently destroys disposable data:

```powershell
uv run --locked --offline python -m scripts.consumer_kind down @capacityTarget --execute --confirm-delete qual-capacity-32-local
```

No disposal command belongs to the persistent `fleetiq-local` target.

## Remaining Stage B gates

After this disposable proof passes, separately review and authorize the retained
foundation's current-envelope repair. Preserve its original profile, node IDs,
storage and 2-CPU/4Gi per-node Docker budgets. A healthy disposable run does not
prove legacy repair or permit migrating persistent resources.

The [approved complete-stack resource design](node-capacity.md#approved-complete-fleetiq-resource-design)
is implemented as a fresh-target selectable profile but remains unqualified.
Review real host capacity, bound or measure init peaks, implement the explicit
retained-target migration,
and re-render application/controller placement and quotas. Prove cold-start,
release-overlap and restart pressure before requesting Stage B approval.

## Priority admission troubleshooting

Kubernetes [non-preempting PriorityClasses](https://v1-35.docs.kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/#non-preempting-priorityclass)
are the source of a Pod's admitted priority/preemption policy. An explicit
`Never` policy without a matching named class conflicts with the default
`PreemptLowerPriority` policy and is rejected before scheduling. Pod-side
`Never` alone therefore cannot establish the qualification contract.

The fixture sets both the supported class reference and matching explicit policy;
the latter makes a conflicting replacement fail at admission. Public progress
markers identify restart, class, namespace and per-probe creation stages without
exposing command output or private configuration. An admission rejection is an
incomplete qualification, never scheduler-denial evidence. Do not retry an
existing failed namespace or broaden a previous approval silently; review the
corrected provider revision, fixture scope and exact recovery target first.
