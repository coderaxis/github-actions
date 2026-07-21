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
| [`schema-compatibility.yml`](.github/workflows/schema-compatibility.yml) | Schema-migration + canonical-outbox-conformance gate for every `*-core-postgres` repo — spins an ephemeral `postgres:18`, applies the repo's migrations to HEAD via an auto-detecting ladder (goose round-trip test → `schema.GooseUpDSN` → embedded `schema.Migrate` → static lint; fixes the old "goose gap" where pure-goose repos never actually migrated), then runs the centrally-pinned canonical **outbox verifier** (RFC-0032 / ADR-0069) and fails closed on ANY semantic drift (columns/types/defaults/domain/PK/unique/checks/partitioning). Runs the SSOT [`scripts/schema-compat.sh`](scripts/schema-compat.sh) against the caller. |

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

Every `*-core-postgres` repo carries a thin schema-compatibility caller (the only
per-repo input is the outbox table name; omit it for a repo without an outbox):

```yaml
# .github/workflows/schema-compatibility.yml
on:
  pull_request:
    paths: ["schema/**", "sql/**", "sqlc.yaml", "go.mod", "go.sum", ".github/workflows/schema-compatibility.yml"]
  push: { branches: ["**"] }
  workflow_dispatch: {}
permissions:
  contents: read
jobs:
  schema-compatibility:
    uses: coderaxis/github-actions/.github/workflows/schema-compatibility.yml@v1
    with:
      table: auth_service_outbox   # the repo's outbox table; omit to skip outbox conformance
    secrets: inherit               # REQUIRED: inherits the module-read App creds for private go deps
```

Delivery logic changes are made **once** here and rolled out by moving the `@v1` tag —
never by editing ~40 service repos. The outbox **contract version** is likewise pinned
once here (`outbox_verify_version`), so tightening it is a one-line change in this repo,
not a fleet-wide `go.mod` bump.

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

### Control catalog (policy-as-code)

The controls are declared in
[`controls/delivery-model.yaml`](controls/delivery-model.yaml) — the catalog defines
**policy only** (never how detection is implemented). Each control carries a stable id,
`policy`, `rationale`, `remediation`, `severity`, `scope`, `owner`, and lifecycle
`status`; the checker binds each `detector` to an implementation that may evolve
(regex → AST → CodeQL) without touching the catalog. **critical/major** controls fail
CI; **minor** controls are advisory. Tune with `--fail-on {critical,major,minor}`.

Control IDs (`DM-NNN`) are **stable and permanent** — never rename or recycle; retire a
control via `status: deprecated|superseded`. The table below is **generated** from the
catalog (drift-gated in CI via `--verify-docs`), so docs and policy never diverge:

<!-- BEGIN delivery-controls (generated: scripts/check-delivery-model.py --write-docs) -->

_Generated from `controls/delivery-model.yaml` by `scripts/check-delivery-model.py --write-docs` — do not edit by hand._

| Control | Policy | Severity | Scope | Owner | Status |
| ------- | ------ | -------- | ----- | ----- | ------ |
| DM-001 | CI orchestrates the central canonical build and must never build or publish a container image itself. The central build executor is the sole publish identity. | critical | reusable-workflow | platform-infrastructure | active |
| DM-002 | A single immutable artifact is built once and promoted unchanged. There must be no second build for another environment or build variant. | critical | reusable-workflow | architecture-review-board | active |
| DM-003 | Dev is the first consumer. staging / preprod / prod receive the same digest via the Promotion Controller and must never be pinned, built, or written by this workflow. | critical | reusable-workflow | platform-infrastructure | active |
| DM-004 | AWS auth uses OIDC to assume the ci-build orchestrator role (inputs.ci_build_role_arn). Superseded per-env deploy / terraform-apply role ARNs must never be referenced. | critical | reusable-workflow | security | active |
| DM-005 | The workflow must not pass swagger/docs build-variant flags (e.g. GO_BUILD_TAGS=swagger, -tags swagger) to the canonical build. The same swagger-less artifact is promoted to every environment. | critical | reusable-workflow | architecture-review-board | active |
| DM-006 | Workflow and job permissions are a subset of {contents: read, id-token: write}. No write scope beyond id-token (no packages: write, no contents: write). | major | reusable-workflow | security | active |
| DM-007 | A run step must fail the build when the ref is not main. | major | reusable-workflow | platform-infrastructure | active |
| DM-008 | An OIDC configure-aws-credentials step must be present and id-token: write must be granted, so credentials are short-lived and keyless. | major | reusable-workflow | security | active |
| DM-009 | The workflow must pin the built image (by digest/ref) into the GitOps infra repo dev overlay. This asserts the pin behaviour exists; it does not mandate a specific helper-script name. | major | reusable-workflow | platform-infrastructure | active |
| DM-010 | The reusable workflow exposes a contract_version workflow_call output (versioned public API). | major | reusable-workflow | platform-infrastructure | active |
| DM-011 | The reusable workflow exposes the promoted image_digest as a workflow_call output. | minor | reusable-workflow | platform-infrastructure | active |
| DM-012 | The header references ADR-0051 and RFC-0020 so implementation and policy SSOT cannot drift apart. | minor | reusable-workflow | architecture-review-board | active |

<!-- END delivery-controls -->

Controls are stated as **behaviour** ("never publish a container artifact", "pin the
digest into GitOps"), and every run produces **evidence** (with line numbers) plus
actionable **remediation** on failure. The checker emits a machine-readable report for
dashboards / compliance, and regenerates its own docs:

```bash
# evaluate + JSON report (uploaded as a CI artifact by the guard workflow)
python3 scripts/check-delivery-model.py .github/workflows/deploy-reusable.yml \
  --format json --report delivery-model-report.json

# regenerate the control table in this README from the catalog
python3 scripts/check-delivery-model.py --write-docs README.md
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
