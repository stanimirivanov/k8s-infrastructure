# Verified-TLS resource metrics capability

- Status: Opt-in provider package; consumer selection and live acceptance are separate
- Upstream: Metrics Server 0.9.0; Kubernetes 1.34 or newer

## TL;DR

- This package provides the resource Metrics API, not performance telemetry or benchmark approval.
- The chart verifies TLS on both hops: API server to Metrics Server, and Metrics Server to kubelets.
- Operators explicitly enable kubelet serving bootstrap and approve one reviewed CSR at a time.
- Serving keys are generated in memory and created once through private stdin in an immutable Secret.
- Consumers pin the merged infrastructure revision and public CA values in their Argo CD selection.
- No command installs a workload, weakens TLS, recreates a cluster or approves all CSRs.

## Ownership and scope

`charts/resource-metrics` is reusable packaging owned here. The consuming environment owns its
AppProject/Application, namespace, immutable source revision, scheduling and public CA selection.
Argo CD is the only workload reconciler. Operator certificate provisioning is a separate,
explicit capability; it does not create an Application or run Helm install/upgrade.

The first exercised target is the existing `perfeng-local` kind cluster. Its name, dedicated
context, node names and immutable node image are checked before node mutation. Existing cluster
configuration, Argo CD, volumes, credentials and consumer release locks remain unchanged.
The provider's original kind definition is not rewritten: newly recreated clusters also need the
explicit serving-TLS procedure. Cloud kubelet certificates remain provider-owned; do not run the
kind-specific node rollout on a cloud cluster.

Metrics Server supplies coarse, short-lived node CPU/memory observations for admission. Its
sampling is not accurate benchmark telemetry, historical monitoring or proof of isolation on a
shared Docker host. Perfeng still performs its own freshness, pressure, interference and identity
qualification. Metrics readiness alone never marks a benchmark ACCEPTED.

## Reviewed deployment boundary

The release record under `addons/resource-metrics` binds official 0.9.0 to the registry manifest
digest. The chart supports one installation per cluster with fixed service/account/API/RBAC names.
Do not deploy it beside another owner of `v1beta1.metrics.k8s.io`.

The caller supplies:

- `tls.secretName`: externally created `kubernetes.io/tls` Secret in the workload namespace;
- `tls.caBundle`: base64 PEM CA that signed that Secret's serving leaf; this value is public;
- environment scheduling via `nodeSelector` and `tolerations`.

For the local environment select namespace `monitoring`, Linux control-plane placement and the
control-plane NoSchedule toleration. This keeps the metrics workload off the generator/SUT nodes.
Do not give the Perfeng workload AppProject general cluster-write permission: create a separate
metrics AppProject allowing only the metrics chart source, namespaces `monitoring` and
`kube-system`, and the exact rendered ServiceAccount, Deployment, Service, RoleBinding,
ClusterRole, ClusterRoleBinding and APIService kinds. Namespace creation is not required.

The chart creates no Namespace, Secret, CSR or approval controller. Its service account can read
nodes/Pods and node resource metrics; standard delegated authentication permits TokenReview and
SubjectAccessReview, and the kube-system binding reads request-header authentication configuration.
It cannot approve certificates, mutate nodes, read workload Secrets or launch benchmark workloads.
The Pod runs non-root, drops capabilities, uses a read-only root filesystem and bounded temporary
storage/resources. No host networking or host mounts are used.

`APIService.insecureSkipTLSVerify` is false, with an explicit serving CA. The scraper uses the
cluster CA from its projected service-account volume to validate kubelet serving certificates.
Neither `--kubelet-insecure-tls` nor an aggregation TLS bypass is supported. Readiness/liveness
HTTPS probes do not constitute certificate verification; the APIService and actual scrape path do.

## Offline verification and preview

Prerequisites are Python 3.12, uv 0.12.5, GNU Make and Helm v4.1.4. `make bootstrap` installs the
locked cryptography package for strict CSR parsing and certificate issuance. It is Apache-2.0 OR
BSD-3-Clause; the lock also includes CFFI (MIT-0) and pycparser (BSD-3-Clause). Certificate handling does not use a shell
OpenSSL parser or a new long-running approval service.

```powershell
make bootstrap
make validate
uv run --locked python -m scripts.resource_metrics enable-serving
```

The last command is an offline preview: no cluster reads, keys or writes. Helm strict linting,
schema rejection and rendered-object security tests use explicitly non-deployable synthetic
values under `addons/resource-metrics/render-values.yaml`. Never use those values for selection.

## Explicit serving-certificate setup

Run from this repository after reviewing and merging the provider PR. The commands below operate
the existing dedicated kind cluster through the consumer state root, never the ambient context.
Pause benchmark Runs and coordinate one operator; kubelet configuration/restarts must not overlap
other node administration. These are **mutating** operations and require separate confirmations.

```powershell
uv run --locked python -m scripts.resource_metrics enable-serving --state-root ../perfeng-environment --execute --confirm perfeng-local
```

The rollout prevalidates all three node files and command-line overrides, enables
`serverTLSBootstrap`, removes explicit serving certificate/key file selections, and restarts one
kubelet at a time. Other configuration is preserved. Original files remain inside each node at
`/var/lib/kubelet/config.yaml.before-resource-metrics`. File-content checks, exclusive temporary
creation, node/cluster UIDs, address snapshots and Docker container IDs prevent known stale
operations from overwriting a changed target. It is not a distributed transaction: partial rollout
is retained for inspection, never hidden by automatic rollback or cluster recreation.

Kubernetes deliberately does not auto-approve kubelet serving CSRs. Inspect pending requests:

```powershell
uv run --locked python -m scripts.resource_metrics inspect --state-root ../perfeng-environment --execute
```

Review the printed request name, node name/UID and addresses. Approve each **specific** reviewed
request, using the values from inspection, not a copied example name:

```powershell
uv run --locked python -m scripts.resource_metrics approve --state-root ../perfeng-environment --request ACTUAL_CSR_NAME --node ACTUAL_NODE_NAME --execute --confirm perfeng-local
```

Only authenticated `system:node:<name>` / `system:nodes` server-only requests are accepted. The CSR
must request exactly `digital signature` and `server auth`, optionally with `key encipherment`.
Both usage sets are supported for EC and RSA keys; client authentication and additional or
duplicate usages are refused. These are the two Kubernetes `kubernetes.io/kubelet-serving`
usage sets, not a TLS verification exception. The CSR
signature/key, exact subject, DNS/IP SANs, denial state and one-hour freshness are checked against
the current node; requests older than that node are refused. The node address/UID snapshot is
rechecked before a resource-version/UID-fenced update to that one approval subresource. This does
not delegate approval authority to the Metrics Server Pod or auto-approve future rotations.
Cluster-admin credentials and exclusive node administration remain trust assumptions; checks cannot
make a compromised cluster administrator safe. Approval is not proof the signer has issued a leaf.

Provision the Metrics Server leaf and its independent serving CA, once:

```powershell
uv run --locked python -m scripts.resource_metrics serving-tls --state-root ../perfeng-environment --namespace monitoring --execute --confirm perfeng-local
```

The namespace must already exist and remain consumer-owned. Private leaf key bytes go only through
kubectl's private stdin, never arguments, logs, files or Git. The CA signing key is discarded from
memory after issuance. An existing owned immutable Secret is verified/reused without rotation;
foreign ownership, mismatched key/SANs, invalid chain or near expiry is refused. A create/commit
error can be ambiguous: retry inspection/reuse, never overwrite a conflicting Secret.

The command prints only public Helm `tls` values. Hand these and the merged infrastructure commit
SHA to the consuming environment's selection PR. A new CA/certificate must not be invented in Git.
Do not deploy by Helm from this repository. Keep both CA bundles and all TLS verification enabled.

## Consumer promotion and acceptance

The consumer adds a narrowly scoped AppProject and Application targeting the immutable merged
`k8s-infrastructure` revision and `charts/resource-metrics`, supplying the verified public values.
Automatic pruning and self-healing start disabled until consumer ownership/rollback is accepted.
Certificate setup can precede Application creation; it cannot prove a deployment happened.
Environment selection cannot pin this uncommitted directory or a future placeholder SHA.

After Argo CD sync/rollout, run the read-only capability check:

```powershell
uv run --locked python -m scripts.resource_metrics health --state-root ../perfeng-environment --namespace monitoring --execute
```

Healthy means an Available APIService with verified serving TLS and complete node metrics with
nonfuture timestamps at most 120 seconds old and positive windows no longer than 60 seconds.
Then run `task kubernetes:qualify` in `perfeng-environment` and require all six PASS checks. Real
benchmark runtime promotion and M04 completion remain separate gates.

## Failure, renewal and rollback

- Docker/context/UID/image conflict: stop; verify the exact selected substrate. No adoption occurs.
- TLS command-line overrides: inspect the node's systemd/flag configuration. This tool does not
  silently change a second configuration owner.
- Interrupted rollout: inspect original/current files and pending CSRs before retrying. A retained
  backup or temporary file prevents an unsafe second write. Do not delete it blindly.
- Kubelet configz unavailable after restart: inspect/approve the reviewed serving CSR first; an
  unissued serving certificate may prevent the kubelet HTTPS server from answering.
- CSR refused: approval errors and scoped inspection identify the failed policy check (signer,
  username/groups, usages, timestamp/freshness, deletion/denial, public key/signature, subject or
  SANs). Inspection ignores other signers and users outside the three reviewed nodes. Malformed payloads
  receive a safe diagnostic without echoing their bytes or library error details. Correct the
  reported cause and inspect again; do not rerun the node rollout merely to change CSR usages.
  A request outside the one-hour review window still requires fresh evidence: do not change its
  timestamp, extend the gate or bulk-approve it. Do not use `kubectl certificate approve` as a bypass.
- APIService unavailable: inspect the selected namespace, Service endpoints, certificate SANs,
  CA bundle and Metrics Server logs. A missing registration is not zero resource usage.
- Scrape x509 errors: verify issuance of cluster-CA-signed kubelet leaves; never disable TLS.
- Missing/stale node metrics: inspect availability, clocks and scrape health; rerun qualification
  only after the prerequisite is healthy. Never edit retained evidence to say PASS.
- Serving leaf validity is 365 days; provisioning refuses reuse within seven days of expiry.
  Rotation is explicit: provision a separately reviewed replacement Secret/CA and update the
  consumer Application, retaining the old identity until acceptance. Automatic key rotation or
  deletion is not provided in this first slice.
- Kubelet serving rotation also requires fresh, individually reviewed CSR approval. Monitor pending
  CSRs and certificate expiry; this is an operator-maintained local capability, not unattended PKI.

Rollback disables the consumer Application's automatic reconciliation and reverts selection through
Git. Retain Secrets, CA evidence, node backups, PVCs and other workloads. Do not delete the shared
`kube-system` or `monitoring` namespaces. If node rollback is needed, review the exact node-local
backup/current diff, restore only that file during a maintenance window and restart that kubelet.
No rollback command is executed automatically and previously published certificates remain evidence.

References: [Metrics Server requirements](https://github.com/kubernetes-sigs/metrics-server#requirements),
[official release](https://github.com/kubernetes-sigs/metrics-server/releases/tag/v0.9.0), and
[kubeadm serving-certificate procedure](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/#kubelet-serving-certs).
The [Kubernetes signer contract](https://kubernetes.io/docs/reference/access-authn-authz/certificate-signing-requests/#kubernetes-signers)
defines the two permitted serving usage sets.
