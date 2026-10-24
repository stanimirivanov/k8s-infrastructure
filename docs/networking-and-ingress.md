# Isolated networking and trusted Gateway ingress

- Status: I03 implementation; report live qualification separately
- Profiles: `local-kind/isolated-network-local`, `local-kind/isolated-network-full-local`
- Full-size profile: fresh-target implementation available; live full-stack qualification pending

## TL;DR

- Operators create a fresh explicit target; existing CNIs and profiles are not
  migrated or adopted. Read [ADR 0006](decisions/0006-isolated-network-and-gateway.md).
- Cilium supplies policy enforcement and Gateway API. Consumers own actual
  namespaces, policies, Gateways, routes, TLS Secrets and application trust.
- Choose a free HTTPS host port explicitly; it binds 127.0.0.1 only. The API
  stays loopback and administrative/application ports are not published directly.
- Controller readiness is distinct from actual network/TLS qualification.
- Authorize live targets and idle benchmark windows; preserve partial state and
  inspect failures. Never repair by changing CNI, port, trust or ownership.
- CA bundles are public; CA keys, provider credentials and kubeconfigs are not.
  No automatic host trust installation or insecure verification is supported.

## Profile and ownership

The profile pins kind v0.31.0/Kubernetes v1.35.0 by the existing node-image
digest. It creates a control plane and worker, each capped at two CPUs/4 GiB after
bootstrap (4 CPUs/8 GiB total, 2048 PIDs each, no swap). Initial bootstrap is
temporarily uncapped. The engine minimum remains four CPUs/12 GiB, which does
not prove unused host capacity. Coordinate shared-host benchmarks before live use.

Cilium 1.20.3, its 22 CRDs, and the seven required Gateway API 1.6.3 CRDs are
SHA-256 pinned. Infrastructure owns them and the Cilium GatewayClass. Cilium's
operator cannot create/update those schemas. Consumers keep namespace, policy,
Gateway, route, certificate and release ownership. Do not install a second CNI,
competing API installer or application-owned controller package as a repair.

Two Cilium agents, two Envoy agents and one operator request 350m CPU/768 MiB.
The worker can also host Argo and application workloads only after their combined
capacity is assessed. This slice alone does not qualify a full persistent
FleetIQ installation, production capacity or performance isolation.

The chosen host port is part of the target identity and dedicated kind file.
Changing it is not a health operation or permission to overwrite an owner record.
Existing `isolated-local`, `isolated-argo-local` and Perfeng profiles are preserved.
Networking cannot be enabled by rerunning their application bootstrap.

## Preview and creation

Use an operator-private state directory outside Git, an explicit local Docker
context and a selected free port. Examples are inputs to review, not live
authorization:

```sh
uv run --locked python -m scripts.consumer_kind up \
  --profile isolated-network-local --consumer example \
  --docker-context desktop-linux --state-root ../example-state --https-port 18443
```

Preview performs no subprocess, downloads, kubeconfig reads, state writes or
listener reservation. Review the profile, context, state root, port and budgets,
then obtain execution authorization and add `--execute`. A collision fails without
port substitution. The actual node bindings must match the reviewed mapping.

The profile disables kindnet and kube-proxy during creation. Nodes remain
NotReady until Cilium is installed; that is expected bootstrap sequencing, not
permission to install a different CNI. Exact node IDs/incarnation are journaled
before CNI writes. Shared APIs and all built-in resources undergo ownership
preflight before mutation. Existing schema/selection mismatches stop the run.

## Health and partial recovery

```sh
uv run --locked python -m scripts.network health \
  --profile isolated-network-local --consumer example \
  --docker-context desktop-linux --state-root ../example-state --https-port 18443
```

This is a preview until `--execute` is supplied. Executed health/handoff reports
exact target/incarnation/source, versions, GatewayClass, intended loopback mapping,
resource envelope, controller configuration and rollouts. Traffic and TLS remain
unqualified until their separate native tests pass. No sign-in or database
capability is implied. Helm is needed for exact desired-state comparison.

After an inspected partial creation with complete journaled node IDs/incarnation,
`network apply --execute` with identical target/port inputs explicitly resumes
CNI bootstrap and the original base readiness checks. It does not adopt foreign
controllers, force field conflicts, change selection or repair another profile.
Ordinary `consumer_kind ensure` retains its no-partial-repair contract.

## Consumer routing and trust contract

Use a same-namespace certificate Secret and explicit route attachment:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: application
  namespace: example-apps
spec:
  gatewayClassName: cilium
  listeners:
    - name: https
      hostname: example.localhost
      port: 8443
      protocol: HTTPS
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: application-tls
      allowedRoutes:
        namespaces:
          from: Same
```

The consumer supplies `application-tls` (`kubernetes.io/tls`), its HTTPRoute(s)
and backend Services. The external origin is `https://example.localhost:18443`;
the Gateway port remains 8443. Configure DNS/local resolution explicitly where
needed; the provider changes neither hosts files nor DNS. Same-origin Host,
Origin and secure cookie behavior must survive the proxy. OIDC redirect and
trusted-proxy configuration remain application responsibilities.

Obtain a certificate from the consumer's reviewed issuer and distribute only its
public CA chain through the operator's approved trust process. An explicit CA
bundle can be used by clients; browser/OS trust installation is manual and needs
separate authorization. Never export a root signing key, commit a private TLS
key or disable verification. Production DNS/ACME/certificate renewal is separate.

Cilium's proxy uses its reserved ingress identity. Consumer CiliumNetworkPolicy
must explicitly permit that identity to selected backends in addition to normal
same-consumer flows. Use Kubernetes NetworkPolicy/CiliumNetworkPolicy to deny
unneeded ingress and egress and allow required DNS and named provider/registry
destinations. Policies are additive; do not install a blanket allow policy as
troubleshooting. CNI installation alone does not isolate all application namespaces.

On this profile, I02 delegated application roles also reconcile Gateways,
HTTPRoutes and CiliumNetworkPolicies in listed application namespaces. Cluster
APIs, GatewayClasses, cluster-wide Cilium policies and cross-namespace trust grants
are not delegated. Existing non-network profile permissions do not expand.

## Native qualification

Prepare and authorize a disposable exact target with the full creation,
qualification and disposal sequence. `network qualify --execute` with the same
target flags and optional `--hostname example.localhost` creates two neutral
consumer namespaces (`<consumer>-net-a/b`), four bounded non-root test Pods,
Services, policies, a temporary TLS leaf Secret, Gateway and routes. It requires
those names to be absent and never adopts existing fixtures.

- Prove both cross-node traffic paths before policies.
- Apply policies and prove same-consumer success, cross-consumer ingress and
  egress timeouts, working cluster DNS and required provider/registry HTTPS.
- Distinguish successful Kubernetes exec plus curl's exit/HTTP result from
  Kubernetes/RBAC failures. Use working controls before/after denials.
- Prove accepted routing and rejected foreign namespace attachment, trusted
  TLS, unknown-CA and wrong-host rejection, unselected Host rejection and preserved
  Host/Origin/request Cookie/response Set-Cookie.

The report distinguishes a certificate hostname mismatch from Envoy's TLS
handshake rejection when no SNI listener matches. The latter requires the exact
unexpected-EOF result between verified successful requests to the selected host;
timeouts, connection resets, other TLS errors and unavailable controls fail.

The root key is generated/discarded in memory. Only public CA fingerprint and
outcomes enter the report; no Secret data or CA key is exported. This proves an
explicit-client-trust path, not automatic browser/OS trust or production identity.
Fixtures remain for inspection. A repeated run requires inspected fixture cleanup
or an explicitly authorized fresh disposable target; failure never auto-deletes.

## Disposal, troubleshooting and limitations

Preview removal and authorize exact permanent disposal:

```sh
uv run --locked python -m scripts.network remove \
  --profile isolated-network-local --consumer example \
  --docker-context desktop-linux --state-root ../example-state --https-port 18443 \
  --confirm-delete example-local --execute
```

This uses I01's immutable node-ID/incarnation checks and deletes only the whole
dedicated target and its three ownership/configuration files. All cluster-local
consumer data/certificates are lost. No in-place CNI uninstall, shared API deletion,
retained-target migration, host trust cleanup or Docker engine restart is implied.

- **Existing profile/CNI, foreign object or schema mismatch:** stop and design an
  explicit migration/compatibility transition. Do not relabel or force conflicts.
- **Occupied port:** select/review another explicit target port before creation;
  never stop another project or silently change the origin.
- **API/controller timeout or resource pressure:** retain state and inspect the
  exact target. Do not increase limits, disable checks or switch versions implicitly.
- **Network denial lacks positive control:** no pass; inspect actual Pod/service,
  DNS, Cilium policy propagation and remote curl exit result.
- **TLS/route failure:** inspect Gateway/HTTPRoute conditions and public hostname/
  certificate references. Do not print Secret contents or use insecure TLS flags.
- **Kernel/CNI failure:** verify Cilium's documented Linux eBPF/cgroup/TPROXY
  prerequisites. Do not change shared-host networking to force qualification.

Run `make validate` offline and `make network-asset-check` for official bytes and
real Helm output. CI retains the original Argo gates and adds this network gate.
I04 storage, complete Argo+application capacity, product isolation/acceptance,
production administration/SSO and durable recovery remain separate qualifications.
