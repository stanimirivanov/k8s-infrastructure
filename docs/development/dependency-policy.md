# Dependency policy

## TL;DR

- Resolve Python 3.12 dependencies from the reviewed `uv.lock` using the uv
  version required by `pyproject.toml`.
- Add a library or external tool only for a current infrastructure capability
  after maintenance, license, security, and replacement review.
- Keep repository verification offline after bootstrap; run vulnerability
  review separately because it needs current advisory data.
- GitHub Actions may use reviewed release tags or commit pins; cluster images,
  source revisions, archives, and state identities keep exact immutable values.
- Dependabot proposes GitHub Actions updates only. Python updates are manual
  and lockfile-aware until the hosted updater supports the pinned uv toolchain.

## Python dependencies and external tools

Runtime Python libraries belong in project dependencies; Ruff, ty, and other
engineering tools belong in the development group. The repository requires uv
0.12.5 locally and installs that version in CI. Update declarations and
`uv.lock` together with that toolchain. A dependency needs a
current use, maintained releases, a compatible license, bounded input behavior,
and a proportionate replacement boundary. Keep dependency-only updates focused
and run `make verify` and `make supply-chain` after each update.

Docker, kind, kubectl, Helm/Kustomize, Terraform/OpenTofu, cloud CLIs, and
provider scanners are explicit boundary tools. Introduce and pin each with the
capability that uses it. A tool download must have a reviewed version and a
repository-anchored integrity check before executing downloaded bytes. These
live or provider-specific prerequisites are not hidden in the Python inner
loop.

## Updates and immutable identities

The verified resource-metrics capability uses maintained `cryptography` 50.x
(Apache-2.0 OR BSD-3-Clause) for signature/SAN validation and in-memory certificate
issuance. Its locked transitive libraries are CFFI (MIT-0) and pycparser (BSD-3-Clause).
Private keys never enter subprocess arguments, logs or Git. The library boundary
is isolated in `scripts/metrics_certificates.py`; no custom cryptographic primitives
or shell parsing of OpenSSL output is introduced. Helm v4.1.4 is an explicit
offline rendering prerequisite, installed by the reviewed CI setup action.


Workflow `uses:` entries may use reviewed release tags or commit SHAs; the
repository policy checker does not enforce one reference style. The reviewed
kind node-image digest, Argo CD release, provider archives, Terraform locks,
Git source selections, and capability identities remain exact. Changes to
those selections require their own compatibility and operational review.

Dependabot updates GitHub Actions only. Python updates are a manual,
lockfile-aware workflow until hosted uv support is reliable for the pinned
toolchain. Every update receives normal tests and review; an automated pull
request does not establish that a provider target, cost, or state transition is
safe.

## Supply-chain response

The database capability selects CloudNativePG 1.30.1, Barman Cloud Plugin 0.15.1
and cert-manager 1.21.2 (Apache-2.0 projects), with official manifests SHA-256
pinned and OCI images pinned by digest. The supported plugin requires cert-manager;
it is an explicit provider prerequisite, not an unreviewed install hook.
PostgreSQL 18.4 under the [PostgreSQL License](https://www.postgresql.org/about/licence/)
is the neutral persistence fixture, not an implicit consumer database upgrade.
The plugin sidecar includes [Barman (GPL-3.0-or-later)](https://github.com/EnterpriseDB/barman);
its license is distinct from the Apache-2.0 plugin wrapper. Runtime-image redistribution
must preserve the applicable upstream component notices and source obligations.
This introduces no Python library or custom database/backup implementation.
Kind's existing local provisioner is checked against the versioned kind source;
its configuration is not silently adopted or modified. Exact sources and images
live in the [database inventory](../../clusters/local-kind/isolated-network-local/database/releases.json).

The isolated network capability uses Cilium 1.20.3 and standard Gateway API
1.6.3 (Apache-2.0), with the official chart and each API manifest SHA-256 pinned.
The Cilium chart retains upstream image digests and is rendered with existing
Helm v4.1.4; schema-writing operator permissions are removed. No Cilium CLI or
additional Python dependency is introduced. Neutral live probes use go-httpbin
2.26.0 (MIT) and curl 8.23.0 (curl license), selected from maintained upstream
releases and pinned by OCI digest. They receive no product credentials. Their
boundary is [network fixtures](../../scripts/network_fixtures.py), not production
application packaging. Public source and exact selections are in the
[release inventory](../../clusters/local-kind/isolated-network-local/releases.json).

`make supply-chain` checks lock consistency and audits known Python
vulnerabilities. Findings are assessed for affected version, reachability,
available fix, and operational exposure. Exceptions require an owner, scope,
reason, review trigger, and expiry. If advisory data cannot be obtained, report
the command as not run and state the residual risk; do not call it passed.

Python package license metadata is not consistently reliable. Dependency
admission includes explicit human license review until a maintained, pinned
scanner and reviewed baseline are available. The harness does not claim an
automated license pass that it cannot prove.
