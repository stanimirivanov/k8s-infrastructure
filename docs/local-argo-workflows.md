# Local Argo Workflows substrate

- Status: Proposed local-kind add-on
- Profile: `local-kind/perfeng-local`

## TL;DR

- This add-on installs Argo Workflows `v4.1.5` into its own namespace using the official asset pinned by SHA-256.
- Offline previews and checks do not contact the cluster. `--execute` is required for installation and health.
- The controller is namespace-scoped; this change creates no Perfeng release WorkflowTemplate or submission authority.
- The existing M04 operator cutover remains in force until a separate consumer release workflow is proven.

## Selection and security boundary

`clusters/local-kind/perfeng-local/addons/argo-workflows/release.json` selects the official `namespace-install.yaml` from the upstream release and its published SHA-256. The installer downloads no more than 16 MiB and verifies the exact hash and resource inventory before applying anything. The manifest contains eight Workflow CRDs, a PriorityClass, two namespace-scoped Roles and bindings, two ServiceAccounts, an internal ClusterIP server and the controller/server Deployments. It has no ClusterRole or ClusterRoleBinding. Both Deployments use `--namespaced` and the reviewed image tag. The default Argo Server authentication mode is `client`; no ingress or port-forward is created by this add-on.

The installation owns only the `argo-workflows` namespace and upstream controller resources. A future `perfeng-environment` change will own release templates, step identities, request validation, receipts and access to any workload namespace. Do not grant the controller or default Workflow ServiceAccount Perfeng permissions as a shortcut. [ADR 0003](decisions/0003-install-namespaced-argo-workflows.md) records the decision.

## Offline validation and preview

Use the locked environment. These commands read only checked-in configuration and print the target commands:

```bash
uv run --locked python -m scripts.workflows validate
uv run --locked python -m scripts.workflows bootstrap --state-root ../perfeng-environment
uv run --locked python -m scripts.workflows health --state-root ../perfeng-environment
```

The preview derives `.local/perfeng-local.kubeconfig` and `kind-perfeng-local` from the selected state root. It does not read that kubeconfig, download the asset or contact Kubernetes. `make configuration-check` checks the same local release selection. `make workflows-asset-check` is a separate network tier: it downloads the upstream asset and verifies its digest and namespaced RBAC topology, but does not contact Kubernetes.

## Explicit install and health

Only after selecting an authorized disposable `perfeng-local` cluster and reviewing the preview, run:

```bash
uv run --locked python -m scripts.workflows bootstrap --execute \
  --state-root ../perfeng-environment
uv run --locked python -m scripts.workflows health --execute \
  --state-root ../perfeng-environment
```

The installer first uses the substrate's dedicated kubeconfig and context checks, verifies node/CoreDNS health, reads namespace identity and checks the release bytes. A missing namespace is acceptable only when the eight Workflow CRDs and controller PriorityClass are also absent. The installer then applies the owned Namespace and verified official manifest through server-side apply without force, checks namespace identity again and waits up to 300 seconds per Deployment. An existing namespace must have this repository's complete labels and exact release version. `health` only reads namespace and rollout status.

The command prints no kubeconfig bytes, tokens or downloaded manifest. If apply or readiness fails, it leaves partial resources for inspection. Repeat only with the same reviewed release and owned namespace; an upgrade or foreign installation needs its own plan. This add-on does not migrate the existing M04 release or activate child Applications.

## Disposable-kind smoke and removal

On a host with local Docker, kind `v0.31.0`, a fresh disposable `perfeng-local` name and a private state root, run the local-kind `up --execute` path, then the bootstrap and health commands above. Restart only the owned `workflow-controller` Deployment, wait for both rollouts and re-run `health`. Inspect the namespace Roles/RoleBindings and confirm `svc/argo-server` is ClusterIP and there are no Workflow ClusterRoleBindings. Finally use the reviewed `scripts.local_kind down --execute --confirm-delete perfeng-local` command for that disposable cluster only. Cluster deletion removes the test CRDs and any Workflow data; never use it as a repair on the retained Perfeng environment.

## Troubleshooting

- **Asset hash or topology differs:** stop. Verify the release record against the official release asset. A new version or changed resource scope requires review; do not bypass the hash.
- **Namespace owner/version differs:** stop before apply. Inspect its labels and the installed controller. Do not relabel or adopt a foreign namespace implicitly.
- **Workflow CRDs already exist with no owned namespace:** identify their owner first. The bootstrap refuses to take over shared cluster API types.
- **Partial apply or failed rollout:** retain the namespace and resources. Inspect the specific Deployment, events and Pods with the dedicated kubeconfig; correct the reviewed input or environmental cause before an exact-version retry. No automatic deletion or credential rotation occurs.
- **Docker unavailable:** run offline checks and asset verification. Report the live disposable-kind gate as not run; do not use the retained Perfeng cluster as a substitute.
