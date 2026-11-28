# Minimal-demo namespace and operation delegation

Status: installed qualification requires a separately authorized disposable run.

## TL;DR

- The provider installs narrow Roles, identity bindings, quotas and admission
  rules for a named minimal-demo target.
- Use explicit `<consumer>-apps`, `<consumer>-data` and `<consumer>-demo-support`
  workload namespaces. The provider owns the separate launcher namespace.
- Approve one exact application or data Job attempt at a time. The attempt
  binds one launcher identity, one Job name and one reviewed Pod specification.
- Opt in an existing deployment observer to read the exact Job and watch its
  creation from an empty list. Withdraw both grants when the attempt ends.
- Qualify the permissions and filesystem on a disposable target. Keep the
  receipt, Pods and retained claim for inspection.

## Ownership and scope

This gate implements [M10-PR18](https://github.com/stanimirivanov/k8s-infrastructure/issues/63).
Use the [runtime contract](https://github.com/stanimirivanov/fleetiq-deploy/issues/91#issuecomment-7070957050)
and [resource ledger](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249)
for consumer-specific Jobs, places and production claims. This provider gate
installs infrastructure authority. The consumer owns application, data and
support desired state and remains responsible for workload credentials and
operation receipts.

Prepare the three workload namespaces with the exact consumer label
`k8s-infrastructure.io/consumer` and Pod Security Admission enforcement
`baseline` at `v1.35`. The provider binds `demo-reconciler` from the separate
`<consumer>-demo-operations` namespace into those exact workload namespaces.
The reconciler manages routine-owned ConfigMaps and private ClusterIP
Services in the three workload namespaces. It can create retained claims
named `demo-ordinary-*` and observe Pods and events. It manages routine-owned
Deployments and StatefulSets in apps and demo-support and can observe
CloudNativePG Clusters in data. Routine ConfigMaps, Services and workload
controllers must carry `k8s-infrastructure.io/demo-routine: "true"`. Keep the
label off operator-owned objects and their templates.

Routine controllers select Pods carrying that label and use the dedicated
`demo-routine-runtime` ServiceAccount with
`automountServiceAccountToken: false`. Each has one bounded container and up
to one replica on the owned worker. Changes to Deployments use `Recreate`.
StatefulSets mount separately requested claims instead of creating them from
claim templates. Allowed volumes are emptyDir, ConfigMap, downwardAPI and
routine claims. Admission refuses Secret env references, Secret/image-pull
credentials and projected tokens. The reconciler cannot update or delete an
operator-owned controller by adding the routine label. Local quotas bound
the controller and Pod inventory, CPU and memory in apps and demo-support.
The quotas account for operator Pods in those namespaces too. The reviewed
consumer workflow still serializes shared worker phases and accounts for
initializer and replacement overlap.

The consumer's separately authorized credential operator owns namespaced
Secret writes and credential-bearing controller writes in apps and
demo-support, along with CloudNativePG Cluster writes and database credential
references in data. Use the scoped deployment workflows for
[database and bootstrap](https://github.com/stanimirivanov/fleetiq-deploy/issues/96),
[administrative Jobs](https://github.com/stanimirivanov/fleetiq-deploy/issues/100)
and [retained simulator Jobs](https://github.com/stanimirivanov/fleetiq-deploy/issues/104).
The [issuer](https://github.com/stanimirivanov/fleetiq-deploy/issues/102),
[receiver](https://github.com/stanimirivanov/fleetiq-deploy/issues/107) and
[RustFS](https://github.com/stanimirivanov/fleetiq-deploy/issues/108) workflows
own their support-service credentials.
Those workflows supply the reviewed manifests and own their scoped operator
permissions. This provider profile grants the routine reconciler neither
Secret access nor CloudNativePG mutation. Keep credential material and
credential-bearing inputs in operator-owned Secrets and claims outside the
`demo-ordinary-` prefix. Use Secret references in visible specs. Keep
credential-bearing configuration outside routine-owned objects. Namespace
creation and cluster API or RBAC changes remain provider operations.

## Approve an operation

An operation document comes from a reviewed workflow. It names one concrete
Job attempt in the application or data namespace and includes one regular
container and, where needed, a bounded initializer. For example, a neutral
application attempt with the filesystem probe image uses this input:

```json
{
  "name": "neutral-attempt-01",
  "namespace": "example-apps",
  "deadlineSeconds": 60,
  "podSpec": {
    "securityContext": {"runAsUser": 65532, "runAsGroup": 65532},
    "containers": [{
      "name": "operation",
      "image": "docker.io/library/python:3.12.16-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4",
      "command": ["python", "-I", "-B", "-c", "print('neutral operation')"],
      "resources": {
        "requests": {"cpu": "50m", "memory": "32Mi"},
        "limits": {"cpu": "200m", "memory": "64Mi"}
      }
    }]
  }
}
```

Review every image, command, credential reference, mount and resource bound.
Store the document outside Git and supply references instead of secret values.
To delegate observation, add `"observer": {"kind": "ServiceAccount", "name":
"job-observer", "namespace": "deployment"}` to the operation document. An
individual Kubernetes `User` uses `kind` and `name`, with the exact authenticated
username. Select an existing identity whose other roles and group memberships
provide no additional Job permissions. Include this subject in the existing
attempt review. The subject participates in the operation digest. A document
without `observer` keeps its existing digest and authorization.

Use the preview's digest to identify the approved attempt. Render its exact
Job manifest offline into a private file for the launcher:

```sh
(
  umask 077
  uv run --locked --offline python -m scripts.minimal_delegation job \
    --consumer example --docker-context default --state-root /private/state \
    --https-port 18443 --operation /private/attempt.json > /private/attempt-job.json
)
```

The provider creates a digest-named launcher and a separate runtime ServiceAccount.
The launcher can submit the exact new Job and read that Job and its reviewed
template. Native admission binds the launcher to the reviewed PodSpec. The
runtime account carries no API grants or mounted API token. The Job has one
completion, no controller retry budget, an explicit deadline and worker
placement. The application namespace keeps simulator Jobs with ingestion. The
data namespace can host a separately reviewed administrative operation.
The deployment operator reviews and stages any Secret and ConfigMap references
before approval. Stage immutable operator-owned inputs for attempts that
consume credentials or execute commands from configuration.

The observer grant provides Batch Job `get`, `list` and `watch` for the
reviewed Job name in its workload namespace. Collection list and watch
require `fieldSelector=metadata.name=<reviewed-job-name>`. Kubernetes also
serves a deprecated watch route scoped by the Job name in its path. Use the
collection route with the exact selector for this workflow. Before the initial
POST, list that name as the observer, verify `items` is empty and retain the
list's `metadata.resourceVersion`. Start a namespaced Job watch with the
same selector and that resource version. Use the first `ADDED` event to obtain the
server-assigned UID and compare the named GET with it. Treat an expired watch
version, a missing creation event, or different Job identity as incomplete
observation. The [Kubernetes RBAC resource-name rules](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#referring-to-resources),
the [Batch Job API](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.35/#job-v1-batch)
and [watch version semantics](https://kubernetes.io/docs/reference/using-api/api-concepts/#semantics-for-watch)
define this behavior.

## Configure and inspect

Use the same exact context, private state root and HTTPS port selected at cold
bootstrap. Complete the owned [network](minimal-network.md) and [database](minimal-database.md)
provider setup before installing delegation. The workload namespace owner
creates the named namespaces first. Preview is offline:

```sh
uv run --locked --offline python -m scripts.minimal_delegation configure \
  --consumer example --docker-context default --state-root /private/state \
  --https-port 18443 --operation /private/attempt.json
```

After separate approval for the exact target, repeat with
`--confirm-target example-local --execute`. Omit `--operation` to select the
base namespace delegation. The provider installs quota and admission controls
and exercises their admission behavior before enabling the corresponding
RoleBindings. The named provider lock serializes configure calls. Configure
checks all three workload namespaces for another attempt grant or an active
or incomplete owned Job. Repeating an attempt with an existing Job checks
its identity and reviewed envelope. For a watched attempt, configure
requires the existing observer binding before publishing the handoff. A
recorded withdrawal blocks further configure calls for that same attempt.
The provider records completed phases and the last observed grant in a
private `<target>.delegation-configure-*.json` receipt. If configure is interrupted,
inspect the exact grants and the receipt before retrying. Run `health` with
the same inputs and explicit target confirmation for live inventory.
The preview and configure receipt identify the observer subject and the
attempt RoleBinding. Use that identity, target, namespace, Job name and digest
for handoff. Review which groups and roles affect the observer on the target.
A `Forbidden` on collection reads warrants checking the selected namespace,
exact `metadata.name` selector and binding. Treat a withdrawn attempt as
retired and prepare a fresh reviewed name for the next attempt.

## Disposable qualification and evidence

Use a fresh `qual-` consumer and the same reviewed source for the capped
bootstrap, network and database receipts. Preview `qualify` to see the two
digest-qualified worker probe images. Stage them on the exact capped worker
in the separately approved preparation window. Use the [database guide's
staging procedure](minimal-database.md#scope-and-prerequisites).

After authorization for this disposable target, run the native gate in the
admitted tooling cgroup:

```sh
uv run --locked --offline python -m scripts.minimal_delegation qualify \
  --consumer qual-demo-01 --docker-context default --state-root /private/state \
  --https-port 18443 --request /private/bootstrap-request.json \
  --host-id sha256:CURRENT_BOOT_DIGEST --confirm-target qual-demo-01-local --execute
```

The gate checks positive and forbidden API operations as the exact scoped
identities. It tests ordinary controllers, refused controller credential
paths, denied reconciler access to Secrets and operator-side CNPG admission.
It also dry-runs unrelated Jobs in the reviewed and a foreign namespace.
Its neutral observer lists the unused name, watches from the pre-submit
resource version and reads the first server-assigned UID. The launcher
submits one neutral Job. The gate checks terminal success and the exact Pod
owner, verifies that unfiltered, wrong-name and wrong-namespace reads and
Job mutations are denied, and checks that fresh launcher CREATE and exact
observer reads are denied after withdrawal. Negative observer reads pair the
raw requests with matching SubjectAccessReview decisions. Each workload
namespace opts into `local-retained` Filesystem RWO claims of whole 1–4Gi,
with at most four and 8Gi requested per namespace. The quotas measure requested storage. The installed filesystem
probe binds one neutral 1Gi application claim, verifies its Retain PV identity
and worker affinity, and checks a local ext4 mount. Two bounded Pods exercise
SQLite and POSIX lock exclusion with UID/GID 65532 and private 0700 directories
and 0600 files. Once the original writer exits, a replacement reads both
SQLite state and the checkpoint persisted by file fsync, rename and directory
fsync. The local-path helper gets its own bounded CPU and memory allocation.
The test also checks that an unrelated object survives unchanged.

The shared qualification deadline is 900 seconds. Individual filesystem Pod
waits are at most 90 seconds within that deadline. The private
`<target>.delegation-qualification.json` records source, target, admission,
binding, checks and outcomes. A failed gate leaves its neutral objects and
the last completed phase for diagnosis. Keep kubeconfig, raw diagnostics and
credentials outside Git. Record the reviewed, published provider commit in
the downstream qualification and compare it with the provider receipt's
`sourceProvenance`. Whole-target cleanup uses the separately confirmed
[cold-bootstrap removal procedure](minimal-bootstrap.md#failure-diagnosis-and-explicit-cleanup).

## Withdrawal and retained state

The consumer's controlled deployment operator inspects the exact Job status
and owned Pods under its separately scoped read access. It captures operation
identity, selected input and artifact fingerprints, exit status and
application results in its private receipt. Give downstream consumers the
sanitized receipt. The launcher has named Job and PodTemplate reads and does
not have Pod logs, exec or cleanup access.

Stop launcher submissions and close any observer watch connections. An
already-open server watch can continue after RoleBinding revocation. Verify
the exact attempt is unused or wait for its Job to reach a terminal condition
with all Pods accounted for and its receipt collected. If a launch was
interrupted or its CREATE result is ambiguous, inspect the exact attempt
under provider authority and resolve the original submission before declaring
the name unused, retiring controls or approving another attempt. An absent
Job at one instant or a client timeout leaves the submission unresolved.
Preview withdrawal with the same reviewed operation, then execute it on the
same exact target.

```sh
uv run --locked --offline python -m scripts.minimal_delegation withdraw \
  --consumer example --docker-context default --state-root /private/state \
  --https-port 18443 --operation /private/attempt.json \
  --confirm-target example-local --execute
```

Withdrawal verifies the target and the RoleBindings' subjects, role
references and ownership labels. It withdraws the launcher binding, confirms
denial of fresh CREATE requests, rechecks Job and Pod status, and withdraws
the observer binding when present. The private
`<target>.delegation-withdraw-*.json` receipt records the exact binding UIDs,
the last completed phase and the observed Job and Pod identities. If
interrupted, inspect the Job and receipt and repeat withdrawal with the same
operation. A retry refuses replacement bindings. If a Job appears after an
unused snapshot, withdrawal stops and preserves the observer binding if it
is still present. The provider and deployment operator must reconcile the
late Job, collect its terminal evidence and complete UID-scoped cleanup
before retrying the original withdrawal. Keep the original receipt.

An already-authorized CREATE can still complete after the launcher binding
is withdrawn. The attempt admission policies, PodTemplate and tokenless
runtime stay in place. Under the separate provider retirement procedure,
remove the named parameterized ValidatingAdmissionPolicyBinding before
deleting its PodTemplate. Remove the remaining attempt controls only once
all outstanding execution is accounted for. Keep shared admission, namespace
state and the existing observer identity.

The deployment operator can then delete the exact owned Job and its Pods with
UID-scoped cleanup. Retain historical receipts and use a fresh reviewed Job
name and digest for another attempt after teardown. Preserve retained
PVCs/PVs and any Jobs whose evidence is pending. A replacement Pod test
establishes local persistence on the same worker. Loss of that worker or the
kind cluster remains a separate recovery condition.
