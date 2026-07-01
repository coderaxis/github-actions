# coderaxis/github-actions

Shared **reusable composite actions** for the coderaxis platform CI/CD.

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

## Versioning

- Consumers pin the **major** tag `@v1`, which is a moving tag updated to the latest `v1.x.y`.
- Breaking changes bump to `@v2`.
