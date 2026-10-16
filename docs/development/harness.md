# Kubernetes infrastructure engineering harness

## TL;DR

- Run `make bootstrap` once, `make verify` while editing, and `make validate`
  before handoff when current advisory data is available.
- `make verify` is offline after bootstrap and validates the reviewed local
  kind and Argo CD configuration without contacting a cluster.
- `make supply-chain` checks the lock and known vulnerabilities; advisory data
  may require network access.
- Live `--execute` commands are separate, target-specific operations governed
  by the [operating guides](../README.md).
- Report unavailable checks as not run under
  [CONTRIBUTING.md](../../CONTRIBUTING.md#verification-and-constrained-environments).

Resource-metrics packaging additionally requires Helm v4.1.4. CI installs that
version on both supported operating systems. `make metrics-check` performs
strict offline linting/rendering and security assertions; `make verify` includes
it and the certificate/CSR/CLI regression tests. Rendering uses synthetic,
non-deployable values, never live certificate material. The explicit certificate
operations and real kubelet/aggregation TLS acceptance remain separate from CI.

## Verification tiers

| Tier | Command | Evidence and prerequisites |
|:--|:--|:--|
| Setup | `make bootstrap` | Resolve `uv.lock`; network may be needed. |
| Toolchain | `make doctor` | Print Git, uv, Python, and Make versions. |
| Inner loop | `make verify` | Formatting, repository policy, lint, types, unit tests, and local configuration checks. Uses installed dependencies offline. |
| PR metadata | `make pr-event-check` | Reproduce the separate CI check against `GITHUB_EVENT_PATH`; no API calls. |
| Supply chain | `make supply-chain` | Lock consistency and known vulnerability audit; current advisory data may need network access. |
| Non-live acceptance | `make validate` | Aggregate inner loop and supply-chain checks. |
| Live operations | See [local kind](../local-kind.md) and [Argo CD](../local-argocd.md) | Explicit `--execute`, selected state root, target checks, and separate authorization. |

`make docs` remains an alias for `make docs-check` so existing contributor
commands continue to work. The validation job calls the Make targets rather
than repeating their individual commands. Windows and Linux run `make verify`;
separate jobs run `make supply-chain` and the PR metadata check.

## Check contracts

The repository policy checker reports the affected path and rule. It validates
local Markdown destinations and anchors, required summaries, issue and pull
request templates, and ADR numbering and indexing. It does not prescribe a
GitHub Action reference style, resolve remote links, or infer infrastructure
health. Regression tests cover accepted and rejected states.

On human-authored pull requests, the separate metadata workflow checks the
event title, first body line, and source branch for one matching issue number.
It checks out the exact base revision and runs only its trusted policy code; it
never checks out or executes pull-request source. The job has read-only
permissions, reads the local event payload without a GitHub API call, and
needs no repository secret. Require the `Issue-linked pull request metadata`
job in branch protection to make this a merge gate. Dependabot
dependency-update pull requests are exempt because their generated branches
and titles do not follow the issue-linked contributor workflow.

`make configuration-check` validates the exact checked-in local-kind profile
and pinned Argo CD selection. It reads only repository-owned configuration and
never invokes Docker, kind, kubectl, or a cluster.
Unit tests exercise identity, refusal, timeout, failure, and recovery behavior
with process boundaries replaced. A successful offline run is not evidence of
a successful live cutover.

## Extending the harness

Add a provider-specific offline format, validation, static/security, and
reviewed-plan check with the provider capability that requires it. Place it
under a named Make target, test the important rejection path, and wire it into
CI at the appropriate tier. Keep live plan, apply, and destroy operations
distinct, target-verified, and outside `make verify` and `make validate`.
Recurring review findings should become a focused checker or clearer guidance
when a deterministic local check can catch them early.
