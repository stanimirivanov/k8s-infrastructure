# Minimal-demo Cilium qualification

Status: implemented probe. The installed candidate remains unqualified until an explicitly authorized run passes.

## TL;DR

- Start with the admitted, capped [minimal-demo cold bootstrap](minimal-bootstrap.md)
  and select the reviewed loopback HTTPS port at creation.
- Stage the exact digest-qualified Cilium and neutral probe images on their
  assigned nodes before qualification. The qualification refuses missing images.
- Qualify a fresh disposable `qual-` target with the original request and host ID.
  This installs Cilium in stages and checks effective resources, actual init
  completion, allowed/denied traffic, DNS, Gateway routing and verified TLS.
- Inspect the private receipt after success or failure. Cleanup is separately
  confirmed through `scripts.minimal_bootstrap down`.

## Scope and prerequisite

This gate implements [issue 61](https://github.com/stanimirivanov/k8s-infrastructure/issues/61)
using the [approved ledger](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249),
[pinned networking assets](../clusters/local-kind/isolated-network-local/releases.json)
and the existing [network and TLS contracts](networking-and-ingress.md).
Run this on a fresh dedicated target in the approved tooling cgroup. Keep the
same release source, private state root, Docker context, host, request and
listener selection throughout. Only the authorized qualification process should
write to the cluster during the run. The continued admission checks physical
stores, host boot, node identities, kubeconfig, tooling and source before
Cilium writes. Existing profile and retained-target commands use their original
network configuration and capacity selections.

The optional `--https-port` on the bootstrap command selects the network
candidate foundation and a worker `127.0.0.1:<port>` mapping to port 8443. Its
foundation budgets retain four static control-plane Pods, two CoreDNS replicas
and one local-path provisioner. Standard Kubernetes 1.35 kubeadm defaults request
650m for its four static Pods and 100m per CoreDNS replica, which exceeds the
combined 800m system request ledger before adding storage. The candidate retains
the static requests and places bounded DNS and storage Pods on the worker. See
the [foundation resource selections](../scripts/minimal_foundation.py) and pinned
upstream source for the [three control-plane requests](https://github.com/kubernetes/kubernetes/blob/v1.35.0/cmd/kubeadm/app/phases/controlplane/manifests.go),
[etcd](https://github.com/kubernetes/kubernetes/blob/v1.35.0/cmd/kubeadm/app/phases/etcd/local.go),
[CoreDNS replicas](https://github.com/kubernetes/kubernetes/blob/v1.35.0/cmd/kubeadm/app/phases/addons/dns/dns.go)
and [CoreDNS resources](https://github.com/kubernetes/kubernetes/blob/v1.35.0/cmd/kubeadm/app/phases/addons/dns/manifests.go).

`make network-asset-check` verifies the upstream assets and performs actual Helm
rendering for each profile. The minimal-demo report includes exact image
references by node and effective CPU/memory accounting. For each approved
network image, stage the digest reference into the Kubernetes CRI image store on
its assigned capped node. Use a separately authorized preparation window within
the approved host tooling and disk budgets, and serialize this work with other
heavy preparation. The accepted inputs are digest-qualified references printed
under `minimalDemoRequiredImages`. For approved registry preparation, the exact
node means the immutable `<node-id>` taken from the matching private cold
bootstrap receipt. Use the exact selected Docker context and one image per
invocation.

```sh
docker --context default exec "<node-id>" \
  timeout --signal=TERM --kill-after=5s 120s crictl --image-endpoint unix:///run/containerd/containerd.sock pull "<image-ref>"
```

The runtime verifies each CRI repository digest and records the observed image
config digest before applying Cilium objects. Cilium and fixture Pod manifests
use `imagePullPolicy: Never`, so unstaged content cannot trigger an implicit
registry pull during qualification. The continued admission charges the
physical backing stores after preparation. Network probe traffic includes the
reviewed provider and registry **HTTP** destinations and requires functioning
DNS and upstream access. Detailed peak/disk attribution for other installations
and phases remains separate from this Cilium gate.

## Commands

Use the [host admission request](host-admission.md) and the same tool cgroup and
host ID required for [cold bootstrap](minimal-bootstrap.md). Both commands offer
an offline preview when invoked without `--execute`. The following commands
show execution after separate approval for the exact disposable target. Set a
fresh consumer and available listener rather than reusing the example identity
if it is already reserved.

```sh
uv run --locked --offline python -m scripts.minimal_bootstrap qualify \
  --consumer qual-minimal-net-01 --docker-context default --state-root /private/state \
  --https-port 18443 --request /private/minimal-request.json \
  --host-id sha256:CURRENT_BOOT_DIGEST --execute
```

Stage the reviewed images as above. Leave the target otherwise quiescent and
invoke the existing network command with the minimal profile and the same
arguments.

```sh
uv run --locked --offline python -m scripts.network qualify \
  --profile minimal-demo --consumer qual-minimal-net-01 \
  --docker-context default --state-root /private/state --https-port 18443 \
  --request /private/minimal-request.json --host-id sha256:CURRENT_BOOT_DIGEST --execute
```

The 900-second qualification deadline includes continuation admission,
rendering, bootstrap and probes. Controller waits are at most 300 seconds, API
establishment and foundation rollout waits at most 60 seconds per resource,
GatewayClass acceptance 90 seconds and individual traffic/Gateway checks at
most 120 seconds, subject to the operation deadline. Image preparation is a
separate input to this gate. The default neutral origin is
`https://qual-minimal-net-01.localhost:18443`.

## Startup and evidence

The agent runs on both nodes. Its target is 100m/256Mi requested and 1000m/512Mi
limited. Its five serial initializers fully request their individual limits;
four use 512Mi and the upstream CNI binary installer keeps its 1024Mi ceiling.
Envoy runs on both nodes at 50m/64Mi requested and 500m/128Mi limited. The one
worker operator uses 50m/128Mi requested and 500m/256Mi limited. The software
checks every effective request and limit, including concurrent sidecars and Pod
overhead, against the [approved ledger](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249)
and the [kind scheduler calculation](node-capacity.md). CPU limits share the
physical CPU cap.

The gate applies common APIs and supporting resources, then waits for both
agents and witnessed initializer completion before starting Envoy. The Kubernetes
host-scope IPAM agent consumes `Node.spec.podCIDRs` from Kubernetes. The pinned
agent serves xDS and checks the external Envoy version asynchronously. Next the
gate starts the worker operator, then verifies GatewayClass and foundation
readiness before creating four bounded neutral Pods. No automatic DaemonSet
rollout is enabled. The operator uses `Recreate`. The resource report checks
one replacement at a time and includes the maximum permitted init overlap with
steady peers. Multi-controller replacement is outside the selected stage model.
See the [Cilium IPAM guide](https://docs.cilium.io/en/stable/network/concepts/ipam/kubernetes/)
and the [pinned Envoy integration](https://github.com/cilium/cilium/blob/v1.20.3/pkg/envoy/cell.go).

Retention of `<target>.network-qualification.json` in the private `.local`
state subdirectory prevents accidental retry. The receipt records source and
artifact identity, exact target and incarnation, admitted Pod budgets and
placement, observed init completion timestamps and container image IDs, each
node's original cgroup lifetime peak/OOM and CPU statistics, host memory
samples, observed physical free space and network/TLS outcomes. Kernel memory
peaks cover the node lifetime and are compared against the largest accounted
stage so far, including the non-Pod capacity deduction. CPU throttling is
reported. OOM, eviction, unexpected restart, missing readiness or a sampled
floor breach fail the candidate. Host sampling is phase evidence, and installed
Cilium success is narrower than installed product acceptance.

## Failure and exact cleanup

Review the receipt's `lastCompletedPhase` and allowlisted `failure`. At image
admission, verify that each exact digest is staged on the assigned node. At
foundation or startup admission, inspect actual Pod placement, effective
resources, cgroup peaks and container status. At the traffic stage, inspect both
positive controls as well as denied-path results. Use the exact context and
private kubeconfig when separately authorized to inspect the cluster. Keep raw
provider output and the kubeconfig in private storage. Make resource or network
changes through reviewed source and use a new cold target for requalification.

The gate retains bounded fixtures and its receipt for inspection. To remove the
exact disposable target and its local data, use the separately authorized
bootstrap removal command. The reviewed HTTPS port remains part of its identity.

```sh
uv run --locked --offline python -m scripts.minimal_bootstrap down \
  --consumer qual-minimal-net-01 --docker-context default --state-root /private/state \
  --https-port 18443 --confirm-delete qual-minimal-net-01-local --execute
```
