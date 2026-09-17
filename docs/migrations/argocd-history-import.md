# Argo CD history import

- Status: Imported, not activated
- Import date: 2026-09-17
- Donor repository: `stanimirivanov/perfeng-environment`
- Donor revision: `18679d6ea8a456402e3d98ce7246d5cedb087fd4`
- Filtered tip: `b913a3883e3655204abb77196264f7d23e2e3c01`

## TL;DR

- The import retains only the Argo CD manifests, bootstrap/readiness code and
  focused tests selected for the reusable infrastructure boundary.
- Built-in Git history filtering rewrote commit identifiers while preserving
  authors, dates, messages and relevant ancestry.
- Donor paths were renamed into infrastructure-owned locations during filtering.
- Consumer Applications, Projects, credentials and workload health were removed
  by the adaptation layered over the imported tip.
- The merge remains dormant and transfers no live ownership.

## Selected paths

| Donor path | Imported path |
|:--|:--|
| `gitops/bootstrap/argocd/kustomization.yaml` | `clusters/local-kind/perfeng-local/addons/argocd/kustomization.yaml` |
| `gitops/bootstrap/argocd/namespace.yaml` | `clusters/local-kind/perfeng-local/addons/argocd/namespace.yaml` |
| `scripts/gitops.py` | `scripts/argocd.py` |
| `scripts/gitops_status.py` | `scripts/argocd_status.py` |
| `tests/test_gitops.py` | `tests/test_argocd.py` |
| `tests/test_gitops_status.py` | `tests/test_argocd_status.py` |

The filtered tree contains exactly these six paths. The donor operational guide
was not imported because it combines controller installation with Perfeng
repository credentials, Applications and workload acceptance. This repository
provides a boundary-specific guide instead.

## Commit mapping

| Donor commit | Filtered commit | Original purpose |
|:--|:--|:--|
| `bf715f4e2e208e280625f3723485f206006490fb` | `00ad5e41dd6f435c7e9668804b9a8a19ee48a5d5` | Bootstrap local Argo CD GitOps |
| `bd971882bc427dc43fe4f12e046e1855903b3c1c` | `cde9b639683fa8b74f158ebfe9f8667648465ec1` | Select the readable official Argo CD release |
| `44a68ef984ad190df53e986e28b55a4e720d06bd` | `ab07e5d79abb945871f3429580dce159481712cd` | Add the first consumer Application behavior |
| `a4c2cd568480cfa6e9a3096db3cc6faf987f97b7` | `53ba468bf248982015d86deebcfc97c2c6a72882` | Extend consumer deployment behavior |
| `fff93e97fbfc971e5e907efac7bd777124b961d4` | `2bfd2baaf2db2961246c99fdbf790a13bd09b5d6` | Add controlled consumer self-healing |
| `24f80d0faa6cdded2d21493d699b85eeec485876` | `6f8334d8710b3daf96d00e5785b476da724811b7` | Adopt content-addressed consumer configuration |
| `3dbcb758b462f86b7a99a731a6385ccf1699eb95` | `b523632f88d96a64832353b6acec528737f0cafe` | Improve bounded GitOps timeout diagnostics |
| `518125a4e95e3f71412014dbd5d0191ed7fc7f10` | `c6ac205c9d3e8ad42e4d9dfd6aaaac572b222e24` | Promote immutable consumer runtime configuration |
| `c685cf63dafeaad430b7e16f5bb0acfb55ba1346` | `9f5717afd03881c8c83c3b9ba2d2f353889a647b` | Add stateful consumer ownership checks |
| `6ebc99f36bb79dcc96c9953af8da59b37ca78d35` | `628fa1db47ed87e965f041c0e8e328142cb4e891` | Rehearse consumer rollback |
| `b56993cf7cf85bb3a86ac334b9661b04451c421e` | `a6749e8cb41c995bc653e32adefebc5424a8316a` | Re-adopt a consumer after rollback |
| `d813244c19de24cf411e69702003a726aee9d4a9` | `b6234485fec702d508f7200925b04d567785b8a7` | Enable stateful consumer self-healing |
| `7e5996305cf95dcfe8ec231097cb21c82df3f4bd` | `ffeedcce6b39adcd450c77da32047a1cf42f04d4` | Add object-store consumer ownership |
| `4ef415cf0f7eaeb8643af23754b4b1940bd17c5e` | `247066dfa5c64fd3ecc703bd1d2e32198d382bcc` | Improve controller and drift diagnostics |
| `a68c8bb9759c772fc155a44e54f142423ea8404d` | `00571d7a9b2346e74dbf4db3347fa71770c7e001` | Rehearse object-store rollback |
| `aded0e5cd175570ab0ec9c356ab74bfab62fe2a8` | `3210a52a6d16bd19c2afa1e722d451c948b2b099` | Re-adopt the object store |
| `a72edeb08e1c4d51360735e2f8a670b25a648aef` | `17ef21e86636472d291ff11231d98bb1e0503052` | Enable object-store self-healing |
| `fd02c35ca4979b64cbe7c953bb7f42859b4d417a` | `0138c05aed987fa7cc09592ef399162d5426907e` | Retire a legacy consumer deployment path |
| `1de97260d4df557bac233ec7baa559509a3083e2` | `87baf69f8ba6ededeed29178c6a657100727eef9` | Bootstrap stateful consumers without Helm |
| `4fdfed6d8199a6c33692ee82d511ef452467d3c1` | `500eeb3fecc511fa9d0ae0338ba1193471ded73e` | Retire the control-plane Helm path |
| `a3dea967396884015cab7bfece55bed9a7f3039a` | `b567253a06f9ce317161455c5c742d021b798b17` | Complete the donor GitOps migration |
| `18679d6ea8a456402e3d98ce7246d5cedb087fd4` | `b913a3883e3655204abb77196264f7d23e2e3c01` | Register this infrastructure repository |

Commits with no selected-path change were pruned. Tags were not imported because
Perfeng release tags do not identify infrastructure releases. Some retained
commits record consumer behavior because controller and consumer orchestration
previously shared the same modules; the final adaptation removes that behavior
without rewriting the imported evidence.

## Adaptation after filtering

The issue merge adapts the filtered tip while keeping its commits unchanged:

- changes namespace ownership from the consumer to this repository;
- keeps the official readable `v3.5.2` release tag and removes no upstream
  release responsibility;
- reduces the bootstrap to validation, command preview and future controller
  installation;
- retains bounded controller rollout diagnostics and credential redaction;
- removes repository credential, Project, Application and workload-health code;
- adds a refusal before any live process execution;
- adds infrastructure-boundary tests and operational documentation; and
- records the future activation and rollback gates.

These adaptations belong to the final issue merge commit, so reviewers can
distinguish the imported donor state from the new repository boundary.

## Ownership and rollback

`perfeng-environment` remains the active owner. The import does not render the
remote manifest, invoke a process, access a kubeconfig, contact Kubernetes,
create credentials or reconcile a workload.

Before activation, rollback is a Git revert of the import merge. It removes the
dormant files and their reachable filtered ancestry without touching live
resources. After activation, rollback must follow the separately reviewed
cutover procedure; deleting code alone cannot restore operational ownership.

## Verification

Repository checks prove formatting, typing, unit behavior, exact release and
namespace validation, offline previews, bounded readiness diagnostics and
pre-process execution refusal. History inspection verifies the six-path
filtered parent, the 22-commit mapping and the two-parent final merge shape.
