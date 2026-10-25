# ADR-0011: Pin the local Argo CD Redis image and avoid forced registry refresh

- Status: Proposed
- Date: 2026-10-25
- Milestone: M06 — Production Readiness

## TL;DR

- The owner approved canonical Docker Hub Redis by immutable multi-platform digest.
- Keep upstream Argo CD v3.5.2 and set only its Redis container to `IfNotPresent`.
- Scope this exception to the Perfeng local-kind bootstrap and validate it exactly.

## Context

Two independent trusted-main jobs in Perfeng run 40214268159 report Redis
`ImagePullBackOff` with UID-correlated rate-limit events after successful init.
The upstream Redis container uses `public.ecr.aws/docker/library/redis:8.2.3-alpine`
and `Always`. The reports establish a throttling category without identifying
the quota, account or IP. AWS documents a lower unauthenticated pull quota.
Preloading alone would still leave the `Always` registry check in place.

Read-only registry metadata checks found identical OCI index digest
`sha256:08ad0b1d280850169a790dba1393ff7a90aef951fc19632cf4d3ce4f78e679ba`
on ECR Public and Docker Hub. This preserves the represented platforms and
image content while allowing the canonical Docker Official Image source.

## Decision

The `local-kind/perfeng-local` Argo CD Kustomization retains the official
v3.5.2 installation and adds exactly one image transformation to
`docker.io/library/redis` at that index digest. A targeted strategic merge
patch sets only Deployment `argocd-redis` container `redis` to `IfNotPresent`.
All other resource fields and identities retain their upstream configuration.

The offline validator permits exactly that transformation and patch content.
It rejects arbitrary images, digests, patch paths, targets and extra fields.
The exception is bound to v3.5.2; upgrading the controller requires reviewing
its Redis image and pull policy together. No new orchestrator or script is added.

## Alternatives and limits

Authenticated ECR Public pulls would require AWS identity and credential
delivery decisions. Shared CI image artifacts or caching would add a delivery
path and still require resolving the upstream `Always` policy. Longer readiness
deadlines would leave the throttled registry dependency intact.

Docker Hub also has pull quotas and can be unavailable. A missing image still
requires a registry pull. This choice does not qualify non-local registry
credentials, an internal mirror or offline bootstrap, and does not change
isolated/delegated consumer assets.

## Security, compatibility and operations

The owner explicitly approved this registry/digest/policy choice. No new
credentials, CI jobs, network listener, cloud resource or paid service is added.
The digest preserves image identity across the source change, and the index
preserves multi-platform selection. Existing security contexts, Redis Secret
handling, namespace ownership and bounded controller checks remain intact.

Consumer infrastructure selection remains a separate reviewed commit. No
running environment changes on merge. Explicit application can replace the
Redis Pod while retaining resource and Secret ownership. Rollback explicitly
selects and reapplies the previous provider release through the existing owner;
it also restores that release's registry and pull policy. No data, Secret or
namespace deletion is authorized.

## Verification

Exercise allowed and refused selections and patch content offline, retain
existing preview/ownership tests, and compare actual Kustomize renders against
the same upstream bytes. Require an unchanged object inventory and exactly
the Redis image and pull-policy differences. Run `make validate`; live cold
pull and complete consumer acceptance remain separate explicit operations.

References: [upstream manifest](https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.2/manifests/install.yaml),
[AWS ECR Public quotas](https://docs.aws.amazon.com/general/latest/gr/ecr-public.html),
[Docker Hub pull limits](https://docs.docker.com/docker-hub/usage/pulls/).
