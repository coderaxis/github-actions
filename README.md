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
from the implementation. The checker is the **executable form of ADR-0051**.

### Control catalog (declarative + severity-aware)

The controls — their id, human-readable policy, and severity — are declared in
[`controls/delivery-model.yaml`](controls/delivery-model.yaml). The checker binds each
control to a detector and evaluates it. **critical/major** controls fail CI; **minor**
controls are advisory (warnings). Tune the gate with `--fail-on {critical,major,minor}`.

| Control | Policy | Severity |
| ------- | ------ | -------- |
| DM-001 | CI never builds or publishes a container artifact | critical |
| DM-002 | Exactly one canonical build; **no per-environment or per-variant builds** | critical |
| DM-003 | Only the `dev` overlay is written; qualified envs are promoted, never pinned here | critical |
| DM-004 | CI assumes only the ci-build orchestrator identity (no `*deploy*`/`*terraform-apply*` role) | critical |
| DM-005 | **No docs/build-variant tag injected into the canonical build** (no `GO_BUILD_TAGS=swagger`, `-tags swagger`) | critical |
| DM-006 | Least-privilege permissions (⊆ `contents: read` + `id-token: write`) | major |
| DM-007 | The canonical artifact is built from `main` (trunk-based) | major |
| DM-008 | OIDC credentials are configured (no long-lived keys) | major |
| DM-009 | The immutable digest is pinned into GitOps (behaviour, not a specific script name) | major |
| DM-010 | `contract_version` output is declared (versioned public API) | major |
| DM-011 | `image_digest` output is declared | minor |
| DM-012 | The workflow cites its governing policy SSOT (`ADR-0051`, `RFC-0020`) | minor |

Controls are stated as **behaviour** ("never publish a container artifact", "pin the
digest into GitOps") so the policy outlives today's tools; the detector is the swappable
implementation. The checker emits a machine-readable report for dashboards / compliance:

```bash
python3 scripts/check-delivery-model.py .github/workflows/deploy-reusable.yml \
  --format json --report delivery-model-report.json
```

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

### API docs (Swagger) and the single artifact

There is **one** canonical image for all environments — there is **no** "with Swagger"
and "without Swagger" build. Swagger is **never compiled into the canonical image**
(`GO_BUILD_TAGS=""`; the `//go:build !swagger` no-op stub is linked), so the *same*
swagger-less digest is promoted to `dev → staging → preprod → prod`. Building a second
docs/no-docs image would break build-once and is rejected by **DM-002** and **DM-005**.

Developers still get docs — just not from the deployed service:

- **Locally**: `go run -tags swagger …` links the real docs implementation.
- **Centrally**: `docs/openapi.json` is published to the API contract registry
  (`inboxxhq-api-contracts`) and served from a central OpenAPI/Swagger portal.
- **Defense-in-depth**: even if a swagger-tagged build were ever deployed, the runtime
  `swaggerpolicy.DocsEnabled(environment)` policy (dev/staging on, preprod/prod off)
  gates the endpoints. The compile-time exclusion is the primary control; this is the
  backstop. (See `platform/openapiroutes` and `platform/swaggerpolicy` in
  `platform-shared-go`.)

The delivery-model checker is the twin of
[`scripts/check-seed-contract.py`](scripts/check-seed-contract.py): both encode an
enterprise standard as a language-agnostic, stdlib-light gate rather than prose.

## Versioning

- Consumers pin the **major** tag `@v1`, which is a moving tag updated to the latest `v1.x.y`.
- Breaking changes bump to `@v2`.
