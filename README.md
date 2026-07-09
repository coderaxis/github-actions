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

## Versioning

- Consumers pin the **major** tag `@v1`, which is a moving tag updated to the latest `v1.x.y`.
- Breaking changes bump to `@v2`.
