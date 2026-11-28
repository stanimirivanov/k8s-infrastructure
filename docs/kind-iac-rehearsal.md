# Disposable kind rehearsal

Status: The hosted rehearsal qualifies the disposable lifecycle in GitHub
Actions. The #74 laptop rehearsal informs the retained provisioning decision
for Environment #434.

## TL;DR

- Use the **Rehearse disposable kind** Actions workflow to qualify the
  disposable lifecycle on a fresh GitHub-hosted VM.
- Use the existing Linux AMD64 Docker daemon with cgroup v2. Check capacity and
  inotify before planning. The earlier pilot stopped at two Ready nodes when
  the third kubelet exhausted inotify instances.
- Use the pinned OpenTofu CLI, provider archive, dependency lock and node image.
  Review the exact saved plan, then explicitly apply its digest.
- Verify three Ready nodes, CoreDNS, local-path storage, a zero-change repeat
  plan, and repeatable Argo CD bootstrap on the recorded cluster UID.

## GitHub-hosted rehearsal

Open [Rehearse disposable kind](https://github.com/stanimirivanov/k8s-infrastructure/actions/workflows/kind-rehearsal.yml)
and select **Run workflow**. The workflow also runs for deployment input
changes on main and on same-repository pull requests so reviewers can verify
the proposed revision before merge. Its only target is `perfeng-iac-pilot-74`
on the job's local Docker daemon. The workflow checks for a vacant target before
applying and uses a fresh scratch backend and dedicated kubeconfig.

The run summary identifies the revision and reports node placement, DNS, PVC
read/write, repeatable Argo CD installation, an empty OpenTofu repeat plan and
teardown. The job log includes the native OpenTofu results, target Docker
inventory after a failed create, and workload status after a failure on a
verified cluster. Cleanup uses a saved OpenTofu destroy when the scratch state
contains the target. If create failed before recording state, cleanup can
delete the exact named cluster after the recorded vacancy check. The workflow
then checks that the target's containers and state entry are gone and removes
the scratch saved plans. State and kubeconfig stay on the disposable VM. To
retry, start a new hosted run.

## Setup

Infrastructure owns the disposable kind substrate and the pinned Argo CD
installation. Argo CD reconciles consumer workloads. The rehearsal uses
OpenTofu 1.13.10 and the pinned Linux AMD64 build of
`registry.terraform.io/elioseverojunior/kind` 0.0.4. The trusted archive
checksum, also in the dependency lock, is
`7c9d7a8817d0a3293e4a358d9ab88f16c69ef13c6479130e5a790c0b2d1a4e46`.

Use kind v0.31.0, kubectl v1.35.0 and the locked repository Python
environment. Keep the verified archive as a read-only file in the packed
filesystem mirror under the private state directory. The command verifies
the archive and initializes with the checked-in read-only dependency lock
and filesystem-only provider installation. Operations already holds a
recoverable copy of the archive in its private artifact store.

With the pinned `tofu` on `PATH` and `TF_CLI_CONFIG_FILE` set to the staged
mirror configuration, initialize an isolated work area and run the same native
checks as PR CI:

```sh
(
  set -eu
  scratch=$(mktemp -d)
  trap 'rm -rf "$scratch"' EXIT
  export TF_DATA_DIR="$scratch/module-data" CHECKPOINT_DISABLE=1
  export DOCKER_HOST="unix://$scratch/no-docker.sock" KUBECONFIG="$scratch/no-kubeconfig"
  tofu -chdir=clusters/local-kind/iac-pilot init -backend=false -lockfile=readonly -input=false
  make iac-pilot-check
)
```

The tests plan both registered targets, check their topology and reject an
unregistered name. They run without refreshing live state or using Docker or
Kubernetes. If initialization fails, check that the staged mirror points to
the verified provider archive and that the CLI version matches the module.

The laptop's default local Docker endpoint is
`unix:///var/run/docker.sock`. The pilot uses cluster name
`perfeng-iac-pilot-74`, context `kind-perfeng-iac-pilot-74`, and the
loopback Kubernetes API. Its owner-only state, kubeconfig, saved plans and
evidence live under `~/.local/state/perfeng/kind-74`, outside Git. Use
the same OS account and directory for all operations.

## Local operator procedure

Use the [native OpenTofu and kubectl procedure](local-kind-opentofu.md)
for #81 disposable qualification. The original operator commands below
record the earlier #74 rehearsal and are retained until reviewed cutover.

## Run the rehearsal

From the infrastructure checkout, put the pinned tools on PATH. On the
prepared laptop they are available in the pilot's private `bin` directory.

~~~sh
export PATH="$HOME/.local/state/perfeng/kind-74/bin:$PATH"
uv run --locked --offline python -m scripts.iac_pilot preflight --execute
uv run --locked --offline python -m scripts.iac_pilot plan-create --execute
~~~

Preflight reports CPU, available memory, Docker disk and inode headroom,
and inotify limits and visible usage. Processes the caller cannot inspect
are counted separately. Visible counts are a lower bound. Use the actual
three-node readiness result to qualify capacity. If a kubelet hits the
earlier inotify failure, preserve the evidence and review the limit or
shared usage before retrying.

Review the private `create-plan.txt` from planning. Expect one create at
`kind_cluster.pilot` with the pinned image, one control plane, two labelled
workers and a loopback API. Run the following with the SHA-256 printed by
planning.

~~~sh
uv run --locked --offline python -m scripts.iac_pilot apply-create --execute --plan-sha256 "$PLAN_SHA256"
uv run --locked --offline python -m scripts.iac_pilot plan-repeat --execute
uv run --locked --offline python -m scripts.iac_pilot bootstrap --execute
uv run --locked --offline python -m scripts.iac_pilot bootstrap --execute
~~~

Create verifies the actual nodes, DNS and storage readiness and records
the live `kube-system` UID. Repeat planning must report zero changes.
Bootstrap uses that UID, the dedicated kubeconfig, and the pinned manifest
with the same field manager. Verify the installation and controller
generations stay stable across both runs. Commands without `--execute`
preview offline.

## Recovery and teardown

The operation lock and OpenTofu state lock serialize operations. The
command refuses a saved plan if its digest, module, Docker daemon, state
or target inventory differs from planning. It checks the live UID before
bootstrap or destroy and refuses replacement or extra plan actions.

If an apply is interrupted, run `observe --execute` to compare live Docker
and Kubernetes identity with canonical state. Use `recover --execute`
when a known outcome can be confirmed. When identity or ownership is
uncertain, keep the pending record and inspect the resources before any
write. Import and deliberate failure drills are separate exercises.

To retire the pilot, run `plan-destroy --execute`, review the single
target deletion and explicitly run `apply-destroy --execute --plan-sha256
"$PLAN_SHA256"` with its digest. Preserve any state or evidence that must
survive in encrypted off-host storage before teardown. Retained provisioning
and its release, evidence and UID binding are reviewed through Environment
#434, #407 and #408.

## Prepare the retained kind plan for Environment #434

Use the same qualified three-node configuration with the explicit `retained-434`
profile. It selects `perfeng-retained-434`, context `kind-perfeng-retained-434`,
and owner-only state at `~/.local/state/perfeng/kind-434/cluster.tfstate`.
Stage the verified package in that profile's local mirror and use the pinned
tools from the rehearsal. Review the saved plan from this command and use its
exact digest for a separately authorized apply.

~~~sh
uv run --locked --offline python -m scripts.iac_pilot plan-create --profile retained-434 --execute
~~~

After provisioning, independently verify the fresh UID against the private
handoff. Use the recorded UID for the two pinned Argo CD bootstrap runs.
Review the Environment release binding for the new context before activating
workloads.

~~~sh
uv run --locked --offline python -m scripts.iac_pilot bootstrap --profile retained-434 --execute --expected-cluster-uid "$RECORDED_UID"
~~~

[Infrastructure #74](https://github.com/stanimirivanov/k8s-infrastructure/issues/74) ·
[Environment #434](https://github.com/stanimirivanov/perfeng-environment/issues/434)
