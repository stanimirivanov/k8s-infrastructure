# Physical host admission

## TL;DR

- This product-neutral gate accounts incremental demand on a native Linux host;
  it does not create, stop, resize or clean up resources.
- Consumers supply complete finite workload, browser, tooling and physical disk
  bounds. A passing snapshot is neither installed acceptance nor an apply token.
- Preview is offline. Explicit observation requires native local ext4 storage,
  cgroup v2 and a collector already enclosed by the declared tooling limits.
- Preserve 4GiB MemAvailable and 10GiB free on each charged physical filesystem.
  Account physical copies within the four disk categories; never use PVC sizes
  as observations.
- The new minimal-demo profile remains unselectable. Bounded node creation and
  lifecycle integration belong to [issue 60](https://github.com/stanimirivanov/k8s-infrastructure/issues/60).

## Authority and scope

[Issue 59](https://github.com/stanimirivanov/k8s-infrastructure/issues/59) implements
host accounting from the [approved resource decision](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249).
The [pinned ledger](https://github.com/stanimirivanov/fleetiq-deploy/blob/75b65e1f7dab9b04e3c3e586797ff7cce15970b3/docs/capacity/minimal-demo-candidate-ledger.md)
owns application allocations. This provider owns only the neutral accounting
policy. No application role is embedded in the implementation.

`physical-host-admission.v1` has 11GiB retained, 12GiB images/unpacked layers,
4GiB runtime/log/temp/writable layers and 8GiB preparation/artifact category
ceilings, plus 10GiB preserved free: the approved 45GiB envelope. Separate
physical filesystems each retain 10GiB; available space is never summed across
aliases. Distinct node copies count independently. Category ceilings include
all physical copies, helpers, WAL, system use and preparation copies.

The gate admits a bounded **resource request**, not a Kubernetes target. The
consumer supplies an immutable artifact digest and an explicit target label;
these are bound to the request, not proof of cluster ownership. Future lifecycle
callers must separately verify current Docker endpoint, target incarnation,
physical storage mappings and enforced node bounds immediately before mutation.
No local socket is treated as proof that Docker storage belongs to this host.
Existing profiles and commands are unchanged; their historical gates remain.

## Consumer request

Use a private JSON file outside Git. `version` is 1; `target` and each account
`id` are lowercase slugs (maximum 64 characters). `artifactDigest` is the
consumer's immutable SHA-256 digest. Every size is an integer byte count, never
MiB/GiB text, a float or a boolean. At most 64 accounts per resource kind are
accepted. Consumers own inventory completeness and enforce future bounds.

```json
{
  "version": 1,
  "target": "example-local",
  "artifactDigest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "memory": [
    {"id": "nodes", "kind": "workload", "limitBytes": 12884901888},
    {"id": "browser", "kind": "browser", "limitBytes": 1073741824},
    {"id": "tools", "kind": "tooling", "limitBytes": 2147483648,
     "cpuMillicores": 1000, "cgroup": "/sys/fs/cgroup/REPLACE-WITH-ACTUAL-BOUNDED-SCOPE"}
  ],
  "disk": [
    {"id": "data", "category": "retained", "limitBytes": 11811160064, "path": "/srv/example/data"},
    {"id": "layers", "category": "images", "limitBytes": 12884901888, "path": "/srv/example/layers"},
    {"id": "runtime", "category": "runtime", "limitBytes": 4294967296, "path": "/srv/example/runtime"},
    {"id": "artifacts", "category": "preparation", "limitBytes": 8589934592, "path": "/srv/example/artifacts"}
  ]
}
```

These paths and the digest are placeholders, not a deployment recommendation.
Use actual pre-existing disjoint directories on the host. Missing directories
are refused; admission does not create them. The combined scan deduplicates hardlinked files across roots. Reflink-capable
filesystems (including XFS) are unsupported because inode identity alone cannot
prove that shared extents have not received duplicate resident credit.
Directory aliases with identical
physical device/inode share one account only when category/bound/observation
agree. Parent/child directory overlap and nested mounts are refused. Native
ext4 is supported; overlay, remote stores, tmpfs and VM/Desktop storage are
unsupported and fail closed. This intentionally does not infer physical backing
through virtual storage layers.

Memory includes at least one account each for `workload`, `browser` and `tooling`.
Aggregate tooling reservations are at most one CPU and 2GiB. All tooling accounts
must identify enforced cgroups, and one must include the observer itself.
Optional workload/browser `cgroup` paths enable resident credit and require an
actual finite memory.max no greater than the declared bound. Without a cgroup,
resident credit is zero: reserve the entire bound conservatively. Cgroups must
be disjoint and under `/sys/fs/cgroup`; parents and children cannot both count.
Live credit uses only anonymous resident memory, excluding file cache that may
already contribute to MemAvailable. No swap or reclaimable-cache credit is used.

## Commands and prerequisites

Run from a reviewed checkout after `make bootstrap`; Python 3.12, GNU `du`,
Linux `/proc`, readable cgroup v2 metrics and local ext4 paths are required.
Read-only permission failures refuse admission; do not broaden permissions or
run privileged automatically. The collector must already be inside a scope with
finite `memory.max` and `cpu.max` no greater than its declared budget.

```sh
uv run --locked --offline python -m scripts.host_admission_cli preview --request /private/request.json
uv run --locked --offline python -m scripts.host_admission_cli host-id --execute
uv run --locked --offline python -m scripts.host_admission_cli observe --request /private/request.json --host-id sha256:REPLACE --execute
```

`host-id` reports the boot-identity digest. Pass that exact value to `observe`.
An operator may separately start an authorized collector scope with
`systemd-run --user --scope -p CPUQuota=100% -p MemoryMax=2G -p MemorySwapMax=0`
and invoke the observation command there. Set the request's tooling cgroup to
that actual scope before observation; it is not an arbitrary example path.
Creating a systemd scope is explicit operator setup, never an admission side effect.
All tooling/preparation/collection concurrency must fit the aggregate bound;
the collector's bounded scope alone does not bound unrelated builds.

Observation has a 30-second total deadline and a five-second deadline for the combined `du`
scan, without retries. Large/inaccessible directories may fail safely.
No stress load, Docker command, Kubernetes access, reclamation or cleanup occurs.
Use shell redirection to save stdout to an operator-selected receipt outside Git.
Exit 0 means a completed preview or a passing observation; exit 1 means refusal.
Preview without observations validates syntax and reports `liveAdmission:false`;
it never reports a capacity pass. For offline evaluation of a saved observation:

```sh
uv run --locked --offline python -m scripts.host_admission_cli preview --request /private/request.json --observation /private/observation.json --host-id sha256:REPLACE
```

Use only the nested `observation` object from the live receipt as that file.
Offline evaluation remains `liveAdmission:false`, even if `admitted:true`.
The pure Python consumer boundary is `scripts.host_admission.evaluate(request,
observation, host_id=current_host_id, now=current_epoch_seconds)`. Observations
must match the full request digest, target, current boot identity and all account
identities, with a timestamp no more than 60 seconds old and not in the future.
Its inputs are trusted producer measurements; arbitrary caller-supplied JSON is
not independent proof of host state. Saved receipts cannot authorize apply.

## Results, qualification and recovery

Receipts include policy version, exact source-file digest, consumer artifact and
request digests, target label, hashed boot/filesystem/account identities, timestamp,
measured resident bytes, incremental requirements, remaining memory/disk and
refusal reasons. They omit absolute paths, host names, cgroup paths, credentials
and private Docker state. Keep the original input privately to reproduce its digest.
The source digest covers the three admission Python modules; record the Git
revision and dirty state alongside qualification evidence.

Incremental demand is the bound minus measured resident usage. Existing bytes
are already reflected in available space and are not subtracted twice. An
account exceeding its bound is refused, never clamped. A shared filesystem's
free capacity is sampled once and charged with every distinct account's growth.
Live `du` measures allocated bytes, not logical PVC requests. Unsupported nested
mounts are refused rather than silently skipped. Filesystem overhead, future
images, additional node copies and logs must still be included in the request.

`insufficient-memory` or `insufficient-disk` includes remaining-byte totals;
stop admitting new heavy work. Other refusals expose a stable sanitized `reason`
code, never raw exception text:

| Reason | Safe next action |
|:--|:--|
| `invalid-request` | Correct request JSON, account bounds/paths or command flags. |
| `invalid-observation` | Correct saved evidence or inspect malformed/incomplete host metrics, then observe again. |
| `identity-mismatch` | Verify the intended host and request; obtain fresh matching evidence. |
| `stale-observation` | Explicitly observe again; saved evidence has expired or is future-dated. |
| `unsupported-host` / `unsupported-storage` | Use the documented native Linux/ext4 boundary; XFS is refused. |
| `missing-prerequisite` | Check GNU du, requested directories and Linux/cgroup files locally. |
| `permission-denied` | Inspect access locally; do not automatically broaden permissions. |
| `observation-timeout` | Inspect scan scope and host pressure before an explicit retry. |
| `measurement-failed` | Inspect the local measurement dependency; no command output is published. |
| `host-evidence-unavailable` | Inspect the host I/O failure locally; this is the safe OS-error fallback. |
| `resource-bound-exceeded` | Correct an over-bound account; do not clamp its measured usage. |
| `internal-error` | Report a collector defect; this is not evidence of unavailable capacity. |

Inspect the private request and prerequisites, correct the cause, then explicitly
observe again. Never delete retained data, widen the budget, omit a category or
retry until green automatically. If a bound cannot fit, report the measured
shortfall.

A current-host qualification runs the same observer with an explicit target and
bounded tooling, saves its sanitized receipt and reports either success or safe
refusal. Fixtures and empty qualification directories prove only the accounting
path. They do not qualify Docker storage, installed workloads, peak behavior,
bootstrap, scheduling, browser functionality or the full product. Concurrent
host demand may change after sampling; future callers must observe again directly
before each expensive operation and stop new admission under pressure. Complete
installed acceptance remains separately owned and explicitly unqualified.
