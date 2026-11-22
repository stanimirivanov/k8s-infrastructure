# Minimal-demo resource peaks

Status: installed comparison requires separate authorization for the selected disposable target.

## TL;DR

- Collect a bounded evidence window on the exact capped minimal-demo target.
- Run the collector in the tooling cgroup already accounted for by physical
  host admission. It reads native counters and adds no resident service.
- Keep the receipt private. Missing counters, incomplete lifecycle coverage
  and source or identity drift block a capacity claim from that window.

## Scope and prerequisites

This collector supports [M10-PR19](https://github.com/stanimirivanov/k8s-infrastructure/issues/64).
Use the [approved resource ledger](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249)
for budget limits and the [installation acceptance contract](https://github.com/stanimirivanov/fleetiq-deploy/issues/89#issuecomment-7071307181)
for the evidence required by a complete installation. Run the workload or
disposable probe under its own reviewed workflow. Start the collector before
the phase whose startup or overlap you need to observe.

Use the native Linux cgroup v2 and ext4 prerequisites in the
[cold-bootstrap guide](minimal-bootstrap.md) and the exact Docker context,
consumer name, private state root and optional HTTPS port of the existing
capped cluster. Keep its cold-bootstrap receipt and original admission
request. The selected source and artifact digest must agree with the receipt.
The process must have access to the named kubeconfig, the owned Docker target
and the host kernel counter files. Reuse the allocated tooling cgroup and
record its cost. The [host admission guide](host-admission.md) owns the
1 CPU/2 GiB tooling allowance and the physical host floors.

## Collect a window

Preview the selection offline. Specify `--https-port` only when the bootstrap
used a listener. The example selects a 30-second window with three seconds
between planned observations. Choose a duration of 10–1,800 seconds and an
interval of 2–60 seconds that divides the duration evenly, with at most 61
samples including both endpoints. For the ledger's 30-minute normal-operation
window, use `--duration-seconds 1800 --interval-seconds 30`. Capture startup
and init overlap in separate short windows with a shorter interval.

Preflight and final identity checks each have a 90-second limit. Sampling,
including sample writes, is bounded by the selected duration plus 20 seconds.
A scan that takes longer than its interval makes the collection incomplete.
State-root validation and receipt setup each have a 15-second limit. Setup
includes the admission request read, preparation check and initial sync.
The final result sync has a five-second limit. If that sync is interrupted,
the collector allows up to five more seconds to restore the preceding prefix.

```sh
uv run --locked --offline python -m scripts.resource_peaks collect \
  --consumer qual-resource-01 --docker-context default --state-root /private/state \
  --https-port 18443 --duration-seconds 30 --interval-seconds 3
```

Use the current boot digest from the [host admission guide](host-admission.md)
and the input used by cold bootstrap. After separate approval for this exact
target, run from inside its admitted tooling cgroup:

```sh
uv run --locked --offline python -m scripts.resource_peaks collect \
  --consumer qual-resource-01 --docker-context default --state-root /private/state \
  --https-port 18443 --duration-seconds 30 --interval-seconds 3 \
  --request /private/minimal-request.json --host-id sha256:CURRENT_BOOT_DIGEST \
  --receipt /private/state/.local/qual-resource-01.peaks.jsonl \
  --confirm-target qual-resource-01-local --execute
```

Choose a fresh `.jsonl` name in the target's private `.local` directory for
every attempt. Put `--state-root` inside the positive `preparation` account
in the original admission request, or use that account as the state root.
The account must contain `.local` on the same admitted native ext4 filesystem.
Before opening the receipt, the collector checks the account's allocated disk
usage and the store's available bytes. Allow 64 MiB for the capped receipt
and 1 MiB for allocation overhead within the account and above the 10 GiB
physical free-space floor. The start record identifies the charged account
and filesystem. Include retained receipts in preparation usage for later runs.

The collector appends private `start`, `window`, `sample` and `result` records.
The window binds source, host, cluster incarnation and physical stores.
Each sample records monotonic offsets. Require a successful invocation and a
complete result before using the measurements for a capacity claim. Keep the
available prefix for diagnosis after timeouts, interruptions, or an exhausted
receipt budget. Use a new path for another attempt. Stop a running collector
with `Ctrl+C`. Collection reads the target without changing workload limits.

## Read the evidence

Linux `memory.peak` records the cgroup lifetime high-water mark. The collector
reads it without resetting it. A lifetime peak can include activity before
collection began. A maximum of observed `memory.current` measures sampled
usage in the window and can miss spikes between observations. These are
different measurements. [Linux cgroup v2 memory interface](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files)

CPU usage and throttling come from cumulative microsecond and event counters.
Compare endpoints for the same cgroup identity to get interval deltas. Own
bandwidth throttling can be visible at a node cgroup even when a child does
not report it in its local counter. Keep node and container measurements
separate. Node counts already include their descendants.
[Linux cgroup v2 CPU interface](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu-interface-files)

Keep init instances and restartable sidecars in lifecycle accounting. The
receipt lists same-frame, same-node co-observations and the sampled node
memory for those frames. Container lifetime high-water marks can occur at
different times. A short-lived container can appear only after termination
or disappear between scans. An attempt whose cgroup is gone at its first
known termination observation has an incomplete termination tail. The receipt
can retain its sampled maximum, last observed lifetime peak and CPU delta
for the explicitly recorded observed endpoints. Missing or unknown lifecycle
timestamps are a coverage gap for that attempt. Check the receipt against the
expected workload inventory and rerun an authorized window when coverage is
incomplete.

The receipt records unmatched native scopes in `unattributedRuntimeScopes`
and reports their attribution and counter coverage in `runtimeAttribution`.
These scopes can include Pod sandboxes and workload attempts absent from Pod
status. The collector reports their counters when available and leaves their
workload identity unresolved. Node counters include all descendants. The
result's `state` describes the positively identified measurements and the
tooling charge. Check `runtimeAttributionState` in stdout before relying on
per-container coverage. Resolve attribution gaps before making an exhaustive
capacity claim. The collector reports `installedAcceptance: false`.
[Kubernetes init and sidecar lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/#sidecar-containers-and-pod-lifecycle)

Track physical store identity and free space independently from requested
storage. A local filesystem reading includes other users of that store and
does not establish per-workload disk usage or a physical reservation. Keep
the exact artifact, target, interval and store identities with any published
summary. Preserve detailed receipts and inputs in private state outside Git.

## Compare a disposable run to native counters

Approved installed qualification compares the receipt to independently read
native counters for the same window. Use the exact Docker node container IDs
in the bootstrap receipt and the explicit context. For each node, get its
host PID and verify the full ID and lifetime match the receipt:

```sh
timeout 10s docker --context default inspect \
  --format '{{.Id}} {{.State.Pid}} {{.State.StartedAt}}' "$NODE_CONTAINER_ID"
cat "/proc/$NODE_PID/cgroup"
```

In the unified `0::` entry, use the path through the exact
`docker-<node-container-id>.scope` component. The PID may be in `init.scope`
below it. Set `SCOPE` to the enclosing node directory under `/sys/fs/cgroup`.
For a workload container, use the full `containerd://` ID from the exact
Pod's status. Find the corresponding `cri-containerd-<container-id>.scope`
within the reviewed node's `kubepods.slice` and use that directory for `SCOPE`.
Check the Pod UID, container kind and attempt as well as the node. The kubelet
can remove a terminated container's cgroup before you compare it.

```sh
kubectl --kubeconfig /private/state/.local/qual-resource-01-local.kubeconfig \
  --context kind-qual-resource-01-local --request-timeout=10s \
  --namespace "$POD_NAMESPACE" get pod "$POD_NAME" \
  -o 'custom-columns=UID:.metadata.uid,NODE:.spec.nodeName,INIT-IDS:.status.initContainerStatuses[*].containerID,APP-IDS:.status.containerStatuses[*].containerID'
```

```sh
stat --format='%d %i' -- "$SCOPE"
cat -- "$SCOPE/memory.current" "$SCOPE/memory.peak" "$SCOPE/cpu.stat"
awk '/^MemAvailable:/ { printf "%.0f\n", $2 * 1024 }' /proc/meminfo
stat --file-system --format='%a %S' -- "$REVIEWED_STORE_PATH"
```

Read the native counters immediately before and after the window. Match the
device and inode to the cgroup identity digest and compare cumulative peaks
and CPU counters between the bracketing reads. Point values such as
`memory.current` and `MemAvailable` can change between reads. Filesystem
availability is the available block count multiplied by the fundamental
block size. Use the same physical store, even if more than one account is
assigned to it. Compare the tooling cgroup by the same method. Divide CPU
usage microseconds by the observed interval in microseconds to obtain average
cores over those endpoints. The tooling cgroup peak accounts for the collector
and any other processes in that admitted group. The tooling charge reports
CPU deltas from the first tooling observation before receipt setup through the
final identity checks. The enforced cgroup also covers the final receipt write.
An incomplete tooling charge makes the result incomplete. Installed comparison
requires complete relevant observations, counter and identity agreement, and
the reviewed resource envelope.
