# Local-kind history import

- Status: Imported, not activated
- Import date: 2026-09-17
- Donor repository: `stanimirivanov/perfeng-environment`
- Donor revision: `18679d6ea8a456402e3d98ce7246d5cedb087fd4`

## TL;DR

- The import retains only cluster lifecycle code, focused tests and the kind
  profile.
- `git-filter-repo` rewrote commit identifiers while retaining authors, dates,
  messages and relevant ancestry.
- Files were renamed into infrastructure-owned locations during filtering.
- Perfeng namespace bootstrap and every workload implementation were excluded.
- The merge is dormant and changes no live ownership.

## Selected paths

| Donor path | Imported path |
|:--|:--|
| `scripts/cluster.py` | `scripts/local_kind.py` |
| `tests/test_cluster.py` | `tests/test_local_kind.py` |
| `local/kind/cluster-config.yaml` | `clusters/local-kind/perfeng-local/cluster-config.yaml` |

The filtered tree contained no other files. The source guide was not imported
because it combines cluster lifecycle with Perfeng workload composition. This
repository provides a boundary-specific guide instead.

## Commit mapping

| Donor commit | Filtered commit | Original purpose |
|:--|:--|:--|
| `0c022102f159d7098a233441da90bd7253d5c61f` | `8110057576b0c653869c2850eb145fd8303d68a0` | Establish the initial local kind configuration |
| `05df711fe40c251a41e0fe2f77a310b38af58ccc` | `555007a17c4aa3e57a69240a1b24a95d53a7cc42` | Add the safe Python lifecycle and focused tests |
| `5d9875b86b98be2d45133212d030c1831762651c` | `cac0d53829f09b12ae6a3d9e40ea5f3a7a5263a9` | Join the earlier infrastructure ancestry |
| `21551f0ae605e86ee19e01553055a89d8833de3f` | `f9fafc279bb580a3428bec2290ead1b826cd85f0` | Make subprocess decoding independent of Windows locale |
| `c0e05341e40024fa1e537eed0bd6399aae32cc39` | `4ca2376b23f63c84d34ad3d9ed4405489fd8d7c1` | Strengthen local command and identity safety |
| `fd02c35ca4979b64cbe7c953bb7f42859b4d417a` | `d2c832a57af186dc4ec43eb4194d8d21bfd027e0` | Remove the retired workload deployment action |
| `dc0c57836ca1dc8bfd399403756e7d9a2b2c6d9d` | `477163284224c86be067f4b91bc8e3e3a30f6e42` | Align cluster lifecycle with the operator command surface |

Commits with no selected-path change were pruned. Tags were not imported,
because Perfeng release tags do not identify infrastructure releases.

## Adaptation after filtering

The issue merge adapts the filtered tip without rewriting imported commits:

- moves profile lookup to the infrastructure-owned path;
- removes the consumer namespace-bootstrap action;
- adds strict offline profile validation;
- adds an execution refusal before external processes;
- changes imports and tests to the new module path;
- adds repository-local documentation and locked YAML dependencies; and
- records the activation gates.

These adaptations belong to the final issue merge commit so the imported
history remains distinguishable from its new repository boundary.

## Ownership and rollback

`perfeng-environment` remains the active owner. This import does not run a
command, create a kubeconfig, access Docker or reconcile Kubernetes.

Before activation, rollback is a Git revert of the import merge. It removes the
dormant files and their reachable filtered ancestry without touching live
resources. After activation, rollback must use the separately reviewed cutover
procedure rather than removing code alone.

## Verification

Repository checks prove formatting, typing, unit behavior, strict profile
identity, offline preview and pre-process execution refusal. History inspection
uses `git log --follow` on each imported path and verifies the graph contains
both the infrastructure repository parent and filtered donor parent.
