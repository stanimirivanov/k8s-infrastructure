# Minimal-demo bounded cold bootstrap

Status: implemented probe; profile remains unqualified until the exact installed gate passes.

## TL;DR

- Use the dedicated `scripts.minimal_bootstrap` command for `minimal-demo`; generic
  lifecycle commands deliberately refuse this profile.
- Admit the exact target and executable source using a fresh physical host
  observation immediately before creation. Saved admission receipts are not authority.
- The Docker adapter creates stopped nodes with CPU/memory/swap/PID caps, verifies
  them, records immutable IDs, then starts them. It never uses `docker update`.
- The named disposable qualification measures kernel lifetime peaks and actual
  allocatable. Success covers pre-Cilium bootstrap only, not installation fit.
- Failure retains owned state and journals. Cleanup is a separate confirmed
  command; never adopt a cluster, reset evidence, or change daemon configuration.

## Scope and prerequisites

This implements [issue 60](https://github.com/stanimirivanov/k8s-infrastructure/issues/60)
using [physical host admission](host-admission.md). The profile has a 2 CPU/4GiB
control plane and a 2 CPU/8GiB worker. Both enclosing containers have swap disabled
and 2048 PID limits from their first process. Bootstrap/system Pods consume these
same enclosing budgets. The adapter also caps `/tmp` at 256MiB, `/run` at 128MiB,
and local log rotation at three 10MiB files per node. It disables automatic
container restart. [The capacity policy](node-capacity.md) supplies 1750m/3484Mi
and 1750m/7580Mi scheduler envelopes, including system Pods.

Supported execution requires native Linux amd64, unified cgroup v2 with the
systemd Docker cgroup driver, working memory/swap/CPU/PID controllers, and ext4
physical stores. Rootless Docker, user namespace remapping, Docker Desktop,
remote sockets, proxy-dependent kind invocations, alternate mounts, and unknown
Docker protocols are refused. The shim implements the pinned kind v0.31.0 Docker
CLI protocol; it is a repository integration adapter, not an upstream kind API.

Both the pinned node image and the shared `kind` Docker network must already
exist on the selected daemon. Preflight inspects the exact digest-qualified image
and verifies its local RepoDigests before ownership reservation; node creation
also uses `--pull=never` to refuse a cache eviction race. The shim does not pull images or create shared
networks. Provision these prerequisites separately through the operator's
approved daemon preparation procedure; this command grants no preparation
permission. Cold bootstrap means fresh node containers, not an empty daemon.
The pinned image digest is authoritative in the profile YAML; do not replace it
with a tag-only reference.

Docker `overlay2` stores and native containerd-backed `overlayfs` stores are
supported when the collector can prove their physical identity. Socket peer
credentials, daemon executable/root/mount namespace, and ext4 stores are checked.
For systemd socket activation, the Docker service MainPID must own the exact
listener inode. Containerd's socket, process arguments and TOML root identify its
separate image store. Imported containerd configuration is refused. Permission
to inspect `/proc` daemon namespace/file descriptors and connect to containerd's
socket may require a separately authorized privileged collector environment.
Missing access is a prerequisite failure; do not change socket permissions or
switch storage backends to bypass it.

## Admission inputs

Use the request schema from [host admission](host-admission.md). Set `target` to
the exact generated cluster name (`<consumer>-local`) and `artifactDigest` to:

```sh
uv run --locked --offline python -m scripts.minimal_bootstrap source-digest
```

This digest hashes the actual Python scripts, kind profiles, pinned provider
release inventories and dependency lock, including uncommitted changes. Retain the reviewed
Git revision or release alongside this receipt. Source or networking pin changes
require regenerating the request. The receipt binds profile digest, node digest, tooling versions,
request/admission digest, boot identity, target, owner nonce and immutable IDs.

Reserve exactly 12GiB as uncredited `workload` memory for the absent nodes. Keep
explicit browser and tooling accounts. Tooling must run inside its declared,
already enforced cgroup, with aggregate limits at most 1 CPU/2GiB; the collector,
kind, adapter and capacity reconciliation all inherit that scope. The host
admission policy retains a 4GiB memory floor and 10GiB free disk floor per store.

Reserve the full approved 35GiB envelope across the four disk categories, plus
the admission policy’s 10GiB free floor per filesystem. Physical account aliases
are refused and resident credit is discarded for every category. For images,
runtime and retained volumes, use disjoint reservation directories outside the
stores on their actual filesystems. Images map to the image store, retained
volumes to Docker root, and runtime to both Docker and containerd roots. Each
required filesystem needs a positive reservation within the unchanged category
total; split stores do not enlarge that total. Preparation follows its own
observed account path. The helper checks filesystem coverage.
Thus existing shared images are charged by filesystem free space and never
counted as consumer-owned reusable bytes. Do not pass the shared Docker or
containerd root as a `du` account: broad scans and parent/child overlaps are
refused. Other category paths follow the admission collector's existing rules.

## Commands and outcomes

Prepare an existing private state root and the reviewed request JSON outside
Git. Preview performs no provider commands or filesystem mutations:

```sh
uv run --locked --offline python -m scripts.minimal_bootstrap qualify \
  --consumer qual-minimal-01 --docker-context default --state-root /private/state
```

Inside the already admitted tooling cgroup, after explicit authority for this
exact disposable target:

```sh
uv run --locked --offline python -m scripts.minimal_bootstrap qualify \
  --consumer qual-minimal-01 --docker-context default --state-root /private/state \
  --request /private/minimal-request.json --host-id sha256:CURRENT_BOOT_DIGEST --execute
```

Obtain the current host digest through `scripts.host_observation.host_id()`;
the physical host admission guide explains the same boot identity. `create`
uses identical admitted creation and evidence collection but does not mark a
named cold-bootstrap qualification. `qualify` requires a `qual-` consumer.
Neither path installs Cilium, kube-proxy, Argo CD, consumer workloads or ingress.

For a disposable target intended for network qualification, pass the reviewed
`--https-port 18443` at cold bootstrap. This maps `127.0.0.1:18443` to port 8443
on the worker. The port is bound into the owner record and verified against
Docker's stopped container before the first start. Specify the same port for
subsequent network qualification and explicit removal. Omitting the option
creates the pre-Cilium target without an HTTPS mapping.

Selecting the listener also lays down the bounded foundation needed by the
network probe. The stopped-node caps still come first. After Docker starts the
control plane, the adapter writes the pinned kubeadm patches before handing
control back to kind. The four static Pods retain their combined 650m CPU
requests with 1500m CPU and 2048Mi memory limits. Two CoreDNS replicas and a
single local-path provisioner run on the worker with a combined 100m CPU
request, 500m CPU limit and 256Mi memory limit. Their configurations, Service
and StorageClass behavior are retained. The network gate does not admit volume
provisioning helper Pods or PVC activity during its disposable run. These
subdivisions use the existing system reservations and still require installed
qualification.

Creation uses a shared 900-second deadline, including read-only provider commands.
Kind receives at most 600 seconds and allocatable reconciliation at most 280
seconds, each reduced to the remaining operation budget. The 30-second host
collector starts only when its full bound remains. Removal has a separate
180-second deadline. Timed-out subprocess groups are terminated; diagnostic
receipt writes and bounded process draining may finish after the deadline. Host memory is sampled every 0.5 seconds from
before kind starts; the floor is explicitly a sampled minimum. Missing samples
or a sampled floor below 4GiB fail the gate. Node `memory.peak` is a kernel
lifetime peak from the fresh cgroup, covering early bootstrap even before the
first sample. The gate verifies controller caps, zero OOM/oom_kill counters,
zero container restarts, stable cgroup/start identity and CPU throttling counters.
Tooling peaks are labeled as cgroup lifetime peaks, not newly reset operation
peaks; use an appropriate fresh bounded tooling scope if historic failures exist.

Private files under the selected state root's `.local` directory include the
ordinary owner/kind/kubeconfig files, `<target>.bounded-docker/` adapter policy,
per-node claims and journals, and `<target>.bootstrap.json`. Journals capture
creation and prestart verification before the first start request. A partial
failure may have no API incarnation or only one node ID. Never publish private
kubeconfigs, raw Docker output or state. The sanitized receipt includes
`coldBootstrapQualified`, `phase`, actual scheduler envelopes and kernel evidence.
`installedAcceptance` remains false: full product fit requires subsequent gates.
The owner remains `provisioned`, not `ready`; no generic handoff becomes selectable.

## Failure, diagnosis and explicit cleanup

CLI failures report an allowlisted category and phase without raw provider output.
After reservation, the bootstrap receipt also retains `lastCompletedPhase` and
`failure` (including exit code or timeout where applicable). Adapter failures
write private `failure-*.json` journals with safe categories such as
`adapter-protocol`, `docker-create`, `docker-inspect` and `docker-start`; inspect
these alongside node journals when kind exits unsuccessfully. Before reservation
there may be no receipt: inspect the reported preflight phase and prerequisites.

An admission or physical-proof refusal makes no infrastructure changes. A later
failure marks ownership `partial` and retains all available journals. Inspect the
selected receipt and immutable IDs before any recovery. Unsupported protocol,
missing image/network, low host floor, OOM, timeout, source drift, controller cap
mismatch, unexpected cgroup lifetime or allocatable mismatch must be fixed and
requalified on a distinct absent disposable target. Do not loosen budgets.

For an owned partial or completed target, deletion is separately explicit:

```sh
uv run --locked --offline python -m scripts.minimal_bootstrap down \
  --consumer qual-minimal-01 --docker-context default --state-root /private/state \
  --confirm-delete qual-minimal-01-local --execute
```

Removal verifies the exact endpoint, owner nonce, journals, labels, names, image
and immutable IDs before deleting only recorded containers. It does not need a
working Kubernetes API. A claim without an immutable ID represents uncertain
creation and blocks automated cleanup; investigate it explicitly without adopting
by name. Unexpected extra containers also block removal. Failed removal retains
the journal for an exact retry. Successful removal retains the owner/config and
receipts as a tombstone and marks the bootstrap receipt `removed`; use a new
consumer name for subsequent qualification. Shared networks and images are never
removed by this command.
