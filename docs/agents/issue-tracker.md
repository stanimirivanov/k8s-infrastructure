# Issue tracker: GitHub

Issues and specs live in `stanimirivanov/k8s-infrastructure` on GitHub. This repository
owns product-neutral Kubernetes substrate. Use an explicit repository target for GitHub operations.

Before creating work, verify the target with `gh repo view stanimirivanov/k8s-infrastructure`
and check existing issues for duplicates. Follow the selected skill's issue
format. Implementation tickets need bounded scope, dependencies, acceptance
criteria and verification requirements. Preserve existing milestone identities;
apply relevant milestones without inventing or renumbering them.

## Operations

Use `gh issue view`, `list`, `create`, `edit`, `comment` and `close`, with
`--repo stanimirivanov/k8s-infrastructure`. Use `--body-file` for multiline content.
A skill's instruction to publish to the issue tracker means create an issue here;
fetching the relevant ticket means reading that issue and its comments.

Use native issue dependencies and sub-issues when available. On older GitHub CLI
versions, use `gh api --method POST repos/stanimirivanov/k8s-infrastructure/issues/NUMBER/dependencies/blocked_by -F issue_id=DATABASE_ID`
and `gh api --method POST repos/stanimirivanov/k8s-infrastructure/issues/PARENT/sub_issues -F sub_issue_id=DATABASE_ID`.
Obtain the referenced issue's database `id` through `gh api`; it is not its issue
number or node ID. Read dependencies and sub-issues with GET on those endpoints.
An issue is startable only when all blockers are closed. If native relationships
are unavailable, use fully qualified issue URLs with explicit `Blocked by:` and
`Part of:` relationships in the issue bodies.

For cross-repository work, file each implementation ticket with its owner and
link dependencies, compatibility requirements and rollout order. Place a
coordinating issue with the owner of the overall outcome. Never infer a sibling
repository target from the shell's current directory.

For wayfinding, use one map issue and linked child issues. Use `wayfinder:map`
and `wayfinder:<type>` labels when that workflow is selected; create those labels
only as part of authorized wayfinding work. Claim through assignment and record
results in the owning issue. Post a closing explanation before closing an issue,
including when adding information to an already closed issue.

## Pull requests as a triage surface

**PRs as a request surface: no.**
