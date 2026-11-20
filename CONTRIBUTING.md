# Contributing to k8s-infrastructure

## TL;DR

- Deliver one coherent, verified infrastructure outcome per issue and pull
  request.
- Use MUST, SHOULD and MAY as defined below.
- Proceed only on local, reversible assumptions; stop for choices affecting
  state, cost, security, compatibility, ownership or external systems.
- Follow the installed Matt Pocock workflow within the user-authorized scope.
- Run the exact checks and report unavailable checks as not run, never passed.
- Preserve published milestone identities and extraction dependencies.

## Workflow authority

The root [AGENTS.md](AGENTS.md) routes work through installed `mattpocock/skills`.
This guide owns technical verification and safety constraints, not a parallel
planning, ticketing, implementation or review process. Read
[issue tracker](docs/agents/issue-tracker.md), [triage labels](docs/agents/triage-labels.md)
and [domain docs](docs/agents/domain.md) for the selected skill's configuration.

## Policy language and sources of truth

- **MUST** and **MUST NOT** identify requirements. A deviation needs an
  explicit reviewer-approved exception recorded in the issue or pull request.
- **SHOULD** and **SHOULD NOT** identify strong defaults. A deviation is allowed
  only when its reason and trade-off are recorded.
- **MAY** identifies an optional choice.

Lowercase guidance does not silently create another normative level.

| Concern | Canonical source |
|:--|:--|
| Workflow and issue operations | Installed `mattpocock/skills`, configured in [AGENTS.md](AGENTS.md) |
| Concise contributor and agent entry point | [AGENTS.md](AGENTS.md) |
| Infrastructure engineering standards | [docs/development/engineering-standards.md](docs/development/engineering-standards.md) |
| Task-routed documentation and verification tiers | [Documentation map](docs/README.md) and [harness](docs/development/harness.md) |
| Dependency admission and updates | [Dependency policy](docs/development/dependency-policy.md) |
| Architecture decisions | [docs/decisions](docs/decisions/README.md) |
| Substrate/consumer handoff | [docs/architecture/substrate-capability.md](docs/architecture/substrate-capability.md) |
| Cross-repository milestones | [Perfeng roadmap](https://github.com/stanimirivanov/perfeng-environment/blob/main/docs/roadmap/milestones.md) |
| Current responsibility and status | [README.md](README.md) |

Technical sources govern their stated constraints; the root AGENTS.md routes workflows. An
accepted ADR governs its decision until superseded. A contributor MUST surface
an unresolved conflict rather than quietly choosing the convenient rule.

## External operations and destructive actions

Read-only inspection is allowed when relevant. Filesystem, cloud, Kubernetes,
registry, GitHub and other mutations MUST remain within scope and authority.

- Validation and preview are offline and side-effect-free.
- Plan is read-only with respect to managed infrastructure and writes only to a
  reviewed local output when explicitly requested.
- Apply and destroy are separate, explicit, target-verified and bounded.
- Credentials MUST NOT appear in arguments, logs, plans, fixtures or Git.
- State replacement, import, move, deletion, credential rotation and repository
  history changes require explicit authorization and verified targets.
- Partial operations retain non-secret evidence for diagnosis and safe retry.
- Live acceptance runs only when the issue requires it or the owner requests it.

## Verification and constrained environments

Bootstrap the locked environment, then run the offline repository checks:

```sh
make bootstrap
make verify
```

Before handoff, run `make validate`, which adds the lock and vulnerability
checks. `make verify` uses installed dependencies offline after bootstrap.
`make supply-chain` may need current advisory data. A provider or cluster
implementation adds its own format, validation, security, and plan checks to
the appropriate Make target and CI job in the same pull request. The tiers and
prerequisites are defined in the [harness](docs/development/harness.md).

When a check cannot execute because of sandbox, network, credentials, missing
tools/services, platform limits or another constraint, report it as **not run**,
state the blocker and safe attempts, list evidence that did run, state residual
risk and where it should run. Never weaken a check or claim an unexecuted check
passed. Report failures even when apparently unrelated.

## Documentation

Every long narrative document MUST contain `## TL;DR` immediately after its
title and status metadata. A document is long when it has at least 800 words,
more than five second-level sections, or is an architecture, security,
migration or operational guide readers consult selectively. Templates are
excluded.

The TL;DR SHOULD state purpose, reader, outcome and next action in three to
seven bullets. Operational changes update troubleshooting guidance in the same
pull request. Accepted ADRs are historical records; supersede rather than
rewrite them.

## Pull request description

Use the `mattpocock/skills` PR workflow and the repository template: Summary,
Evidence and Merge Danger. Include issue links, relevant milestone, compatibility,
security, operational and rollback effects where applicable. Keep unavailable
verification and residual risk explicit.

## Completion report

Use the selected skill's delivery format. Report changed behavior, verification
outcomes and material limitations; distinguish passed, failed and not-run checks.
