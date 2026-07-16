# coderaxis/github-actions

Shared **reusable composite actions** and **reusable workflows** for the coderaxis
platform CI/CD.

Reference an action by its subfolder and a major-version tag:

```yaml
- uses: coderaxis/github-actions/module-auth@v1
  with:
    app-id: ${{ secrets.CODERAXIS_APP_ID }}
    private-key: ${{ secrets.CODERAXIS_APP_PRIVATE_KEY }}
```

This repo is **public** so workflows in every owner (`coderaxis`, `InboxxHQ-CoderAxis`,
`skentra`) can consume the actions. The actions contain **no secrets** — callers pass
credentials as inputs at call time.

## Actions

| Action | Purpose |
| ------ | ------- |
| [`module-auth`](module-auth/action.yml) | Mint a short-lived GitHub App installation token (`coderaxis-module-reader`) and configure git for private module reads. Replaces long-lived `CROSS_REPO_TOKEN` / `WORKFLOW_GH_PAT`. |

Future actions (e.g. `docker-login`, `slack-notify`, `aws-login`) live as sibling folders.

## Reusable workflows

Whole workflows (job-level OIDC, `permissions`, `concurrency`, multi-job orchestration)
live under `.github/workflows/` and are consumed via `uses:` at the **job** level.

| Workflow | Purpose |
| -------- | ------- |
| [`deploy-reusable.yml`](.github/workflows/deploy-reusable.yml) | InboxxHQ GitOps delivery — CI orchestrates the central canonical build (`inboxxhq-build`), reads back the signed image **digest**, and pins it into the `dev` overlay (first consumer). staging/preprod/prod promote the same digest via the Promotion Controller. **Build once, deploy the digest.** |
| [`seed-contract-check.yml`](.github/workflows/seed-contract-check.yml) | Language-agnostic seeding-contract gate (seeding standard §6b) — Dockerfile seed-binary marker + `seed/data` copy, canonical `system/dev/staging/preprod/prod` tree, placeholder-only qualified envs, no `SEED_COMMAND=""` override. Runs the pinned [`scripts/check-seed-contract.py`](scripts/check-seed-contract.py) (SSOT) against the caller; stateless services self-skip. |

Each service repo carries only a thin caller:

```yaml
# .github/workflows/deploy.yml
on:
  push: { branches: [main] }
  workflow_dispatch: {}
permissions:
  contents: read
  id-token: write
jobs:
  deploy:
    uses: coderaxis/github-actions/.github/workflows/deploy-reusable.yml@v1
    with:
      service_name: auth-service
    secrets: inherit
```

Stateful service repos also carry a thin seed-contract caller:

```yaml
# .github/workflows/seed-contract-check.yml
on:
  push: { branches: ["**"] }
  pull_request:
    paths: ["Dockerfile", "cmd/seed/**", "internal/**/seed/**", ".github/workflows/seed-contract-check.yml"]
permissions:
  contents: read
jobs:
  seed-contract:
    uses: coderaxis/github-actions/.github/workflows/seed-contract-check.yml@v1
```

Delivery logic changes are made **once** here and rolled out by moving the `@v1` tag —
never by editing ~40 service repos.

## Delivery model (enforced, not just documented)

The delivery model is **build once, deploy the digest**. The architectural policy is
owned by the ADR/RFC (single source of truth) — the workflow implements it and a CI
guard **enforces** it:

- Architecture SSOT (in the `core-docs` repo): `ADR-0051` (Artifact Promotion,
  Digest-Pinned Deployment, and Registry Segregation) and `RFC-0020` (Supply-Chain
  Integrity and Artifact Promotion).
- Implementation: [`deploy-reusable.yml`](.github/workflows/deploy-reusable.yml).
- Enforcement: [`scripts/check-delivery-model.py`](scripts/check-delivery-model.py) run
  by the [`delivery-model-guard`](.github/workflows/delivery-model-guard.yml) self-CI
  workflow on every change to the reusable workflow or the checker.

This closes the gap where the model existed only as header comments that could drift
from the implementation. The checker is the **executable form of ADR-0051**; the
following invariants fail CI if violated:

| # | Invariant | Rationale (ADR-0051) |
| - | --------- | -------------------- |
| A | No local container build/publish (`docker build/push`, `buildx`, `podman`, `buildah`, `nerdctl`, `kaniko`, `ko`, `pack`, `buildctl`, `crane push`, `skopeo copy`) in any `run:` block | CI **orchestrates** the central canonical build; CodeBuild `inboxxhq-build` is the **sole publish identity** |
| B | Exactly one canonical build invocation (`aws codebuild start-build`) | Build **once** — multiple builds imply a per-environment rebuild path |
| C | Overlay pins target **`dev` only** (no staging/preprod/prod pin or overlay write) | Dev is the first consumer; qualified envs **promote the same digest** — no rebuilds anywhere |
| D | Least-privilege `permissions` (a subset of `contents: read` + `id-token: write`) | Orchestrator **cannot publish images or mutate clusters** |
| E | A build-from-`main` guard is present | Trunk-based single-main build |
| F | AWS auth assumes the ci-build orchestrator role (`inputs.ci_build_role_arn`); no superseded `*deploy*` / `*terraform-apply*` role ARN | Orchestrator identity only — the per-env deploy roles were deleted |
| G | A `contract_version` `workflow_call` output is declared | The reusable workflow is a versioned public API |
| H | The header cites `ADR-0051` and `RFC-0020` | Implementation and policy SSOT cannot drift |

Consumers can assert the behavioral contract via the workflow outputs:

```yaml
jobs:
  deploy:
    uses: coderaxis/github-actions/.github/workflows/deploy-reusable.yml@v1
    with: { service_name: auth-service }
    secrets: inherit
  verify:
    needs: deploy
    runs-on: ubuntu-latest
    steps:
      - run: test "${{ needs.deploy.outputs.contract_version }}" = "v1"
```

The delivery-model checker is the twin of
[`scripts/check-seed-contract.py`](scripts/check-seed-contract.py): both encode an
enterprise standard as a language-agnostic, stdlib-light gate rather than prose.

## Versioning

- Consumers pin the **major** tag `@v1`, which is a moving tag updated to the latest `v1.x.y`.
- Breaking changes bump to `@v2`.
