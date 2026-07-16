#!/usr/bin/env python3
"""Delivery-model guard (CI). Executable form of the build-once / promote-by-digest
delivery model.

This is the enforcement twin of the canonical reusable deploy workflow
(.github/workflows/deploy-reusable.yml). It turns the architectural policy that
today lives only in that workflow's header comments into machine-verifiable
invariants, so the model cannot silently regress (e.g. someone adding a local
image build, pinning a qualified-env overlay, or widening permissions).

Policy SSOT (single source of truth for the architecture):
  * ADR-0051 - Artifact Promotion, Digest-Pinned Deployment, and Registry Segregation
  * RFC-0020 - Supply-Chain Integrity and Artifact Promotion
This checker does NOT invent policy; it encodes the binding decisions of ADR-0051:
  1. Build ONCE in the central executor (CodeBuild inboxxhq-build), the SOLE publish
     identity. GitHub Actions is orchestrator ONLY - it uploads source, starts the
     canonical build, reads the digest. It never builds or publishes images itself.
  2. Dev is the FIRST CONSUMER: CI pins the freshly-built digest into the dev overlay.
     staging / preprod / prod PROMOTE the same digest via the Promotion Controller -
     there is no per-environment rebuild and no qualified-env pin in this workflow.
  3. Least privilege: only `contents: read` + `id-token: write`; the OIDC identity is
     the ci-build ORCHESTRATOR role (cannot push images or mutate clusters).
  4. Trunk-based: the canonical artifact is built from `main`.
  5. Versioned contract: the workflow exposes a `contract_version` output so consumers
     can reason about breaking changes.

Enforced invariants (HARD failures -> exit 1):
  A. No local container build/publish command anywhere in a `run:` block.
  B. Exactly one canonical build invocation (`aws codebuild start-build`).
  C. Overlay pins target ONLY `dev` (no staging/preprod/prod pin or overlay write).
  D. Least-privilege permissions (top-level and per-job): a subset of
     {contents: read, id-token: write}; no write scopes beyond id-token.
  E. A build-from-`main` guard is present.
  F. AWS auth assumes the ci-build orchestrator role (inputs.ci_build_role_arn); no
     superseded deploy / terraform-apply role ARN is referenced.
  G. A `contract_version` workflow_call output is declared.
  H. The header cites the governing ADR-0051 and RFC-0020 (policy traceability).

Usage:
  check-delivery-model.py [path-to-workflow]      # default: .github/workflows/deploy-reusable.yml

SSOT: this file lives in coderaxis/github-actions and is invoked by the self-CI
delivery-model-guard workflow. It is the delivery-model analogue of
scripts/check-seed-contract.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

DEFAULT_TARGET = ".github/workflows/deploy-reusable.yml"

# ---- Invariant A: local container build / publish is forbidden ---------------
# CI ORCHESTRATES the central canonical build (aws codebuild start-build) and never
# builds or publishes an image itself (ADR-0051: CodeBuild inboxxhq-build is the sole
# publish identity; GitHub Actions is orchestrator only). Any of these in a run: block
# means the workflow is building/publishing locally - a model violation.
FORBIDDEN_BUILD = [
    (re.compile(r"\bdocker\s+buildx\b"), "docker buildx"),
    (re.compile(r"\bdocker\s+build\b"), "docker build"),
    (re.compile(r"\bdocker\s+push\b"), "docker push"),
    (re.compile(r"\bpodman\s+(?:build|push)\b"), "podman build/push"),
    (re.compile(r"\bbuildah\b"), "buildah"),
    (re.compile(r"\bnerdctl\s+(?:build|push)\b"), "nerdctl build/push"),
    (re.compile(r"\bbuildctl\b"), "buildctl (buildkit)"),
    (re.compile(r"\bkaniko\b|/kaniko/executor"), "kaniko"),
    (re.compile(r"\bko\s+(?:build|publish|apply)\b"), "ko build/publish"),
    (re.compile(r"\bpack\s+build\b"), "pack build (buildpacks)"),
    (re.compile(r"\bimg\s+build\b"), "img build"),
    (re.compile(r"\bcrane\s+(?:push|copy|append)\b"), "crane push/copy"),
    (re.compile(r"\bskopeo\s+copy\b"), "skopeo copy"),
]

# ---- Invariant B: the one allowed way to produce an artifact -----------------
CANONICAL_BUILD_RE = re.compile(r"\baws\s+codebuild\s+start-build\b")

# ---- Invariant C: overlay pins are dev-only ---------------------------------
# The reusable workflow pins the dev overlay via helper scripts. Capture the env
# argument (2nd positional, after the service name) and require it to be `dev`.
PIN_ENV_RE = re.compile(
    r"(gitops-set-image\.sh|write-release-metadata\.sh)\s+(\S+)\s+(\S+)"
)
# Writing a qualified-env overlay path directly is also forbidden here.
QUALIFIED_OVERLAY_RE = re.compile(r"overlays/(staging|preprod|prod)\b")
QUALIFIED_ENVS = ("staging", "preprod", "prod")

# ---- Invariant F: superseded deploy/publish identities (removed by ADR-0051) --
# CI may only assume the ci-build ORCHESTRATOR role. Role ARNs whose name carries
# deploy / terraform-apply are the retired per-env deploy identities and must never
# reappear. (The source-artifacts S3 bucket contains "terraform" but is an s3:// URI,
# not a :role/ ARN, so it is not matched.)
FORBIDDEN_ROLE_RE = re.compile(r":role/[\w.+=,@-]*(deploy|terraform-apply)[\w.+=,@-]*", re.IGNORECASE)


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"::error::{path}: not a mapping / invalid workflow YAML")
    return data


def get_on_block(data: dict) -> dict:
    # YAML 1.1 parses the bare key `on:` as boolean True; GitHub workflows rely on it.
    on_block = data.get("on")
    if on_block is None:
        on_block = data.get(True)
    return on_block if isinstance(on_block, dict) else {}


def collect_run_blocks(data: dict) -> list[str]:
    runs: list[str] = []
    jobs = data.get("jobs") or {}
    if isinstance(jobs, dict):
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    runs.append(step["run"])
    return runs


def iter_steps(data: dict):
    jobs = data.get("jobs") or {}
    if isinstance(jobs, dict):
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    yield job_name, step


def check_no_local_build(runs: list[str], errors: list[str]) -> None:
    for block in runs:
        for pat, label in FORBIDDEN_BUILD:
            if pat.search(block):
                errors.append(
                    f"local image build/publish '{label}' found in a run: block; CI must "
                    "ORCHESTRATE the central canonical build (aws codebuild start-build), "
                    "never build or publish images itself (ADR-0051 \u00a7build-once, sole publish identity)"
                )


def check_single_canonical_build(runs: list[str], errors: list[str]) -> None:
    count = sum(len(CANONICAL_BUILD_RE.findall(b)) for b in runs)
    if count == 0:
        errors.append(
            "no canonical build invocation ('aws codebuild start-build'); the reusable "
            "deploy workflow must orchestrate exactly one central build (ADR-0051)"
        )
    elif count > 1:
        errors.append(
            f"{count} 'aws codebuild start-build' invocations; the model is build-ONCE. "
            "Multiple builds indicate a per-environment rebuild path (ADR-0051 forbids it)"
        )


def _strip(arg: str) -> str:
    return arg.strip().strip('"').strip("'")


def check_dev_only_pin(runs: list[str], errors: list[str], notices: list[str]) -> None:
    saw_pin = False
    for block in runs:
        for m in PIN_ENV_RE.finditer(block):
            saw_pin = True
            script, _service, env_arg = m.group(1), m.group(2), _strip(m.group(3))
            if env_arg in QUALIFIED_ENVS:
                errors.append(
                    f"{script} pins the '{env_arg}' overlay; this workflow may pin ONLY 'dev' "
                    "(dev is the first consumer; staging/preprod/prod are promoted by the "
                    "Promotion Controller, never pinned/built here - ADR-0051 \u00a7no env rebuilds)"
                )
            elif env_arg == "dev":
                continue
            else:
                notices.append(
                    f"{script} pins overlay env '{env_arg}' (not a literal 'dev'); cannot "
                    "statically prove dev-only pinning - keep the env argument a literal 'dev'"
                )
        for m in QUALIFIED_OVERLAY_RE.finditer(block):
            errors.append(
                f"writes a qualified-env overlay path 'overlays/{m.group(1)}'; only the dev "
                "overlay may be written here (ADR-0051 \u00a7promote-not-rebuild)"
            )
    if not saw_pin:
        notices.append(
            "no gitops-set-image.sh / write-release-metadata.sh pin found; if the pin step "
            "was renamed, extend PIN_ENV_RE so dev-only pinning stays verifiable"
        )


def _validate_perms(perms, where: str, errors: list[str]) -> None:
    if perms is None:
        return
    if isinstance(perms, str):
        # 'read-all' is acceptable; 'write-all' (or 'write') is not.
        if "write" in perms:
            errors.append(f"{where}: permissions '{perms}' grants write; use least privilege "
                          "(contents: read + id-token: write)")
        return
    if not isinstance(perms, dict):
        errors.append(f"{where}: unrecognised permissions block")
        return
    allowed = {"contents": {"read", "none"}, "id-token": {"write", "none"}}
    for scope, level in perms.items():
        if scope not in allowed:
            if str(level) == "write":
                errors.append(
                    f"{where}: grants '{scope}: {level}'; the delivery workflow needs only "
                    "'contents: read' + 'id-token: write' (ADR-0051 \u00a7orchestrator cannot "
                    "publish images or mutate clusters)"
                )
            continue
        if str(level) not in allowed[scope]:
            errors.append(
                f"{where}: '{scope}: {level}' exceeds least privilege "
                "(allowed: contents: read, id-token: write)"
            )


def check_permissions(data: dict, errors: list[str]) -> None:
    top = data.get("permissions")
    if top is None:
        errors.append("no top-level 'permissions:' block; declare least privilege explicitly "
                      "(contents: read + id-token: write)")
    else:
        _validate_perms(top, "top-level permissions", errors)
    jobs = data.get("jobs") or {}
    if isinstance(jobs, dict):
        for name, job in jobs.items():
            if isinstance(job, dict) and "permissions" in job:
                _validate_perms(job["permissions"], f"job '{name}' permissions", errors)


def check_main_guard(runs: list[str], errors: list[str]) -> None:
    for block in runs:
        if "GITHUB_REF_NAME" in block and "main" in block and ("exit 1" in block or "::error" in block):
            return
    errors.append(
        "missing build-from-main guard; a run: step must fail the build when "
        "GITHUB_REF_NAME != main (trunk-based single-main build; RFC-0019 / ADR-0051)"
    )


def check_oidc_role(data: dict, text: str, errors: list[str]) -> None:
    found_configure = False
    for _job, step in iter_steps(data):
        uses = str(step.get("uses", ""))
        if "aws-actions/configure-aws-credentials" in uses:
            found_configure = True
            with_block = step.get("with") or {}
            role = str(with_block.get("role-to-assume", ""))
            if "ci_build_role_arn" not in role:
                errors.append(
                    "configure-aws-credentials role-to-assume must reference "
                    "inputs.ci_build_role_arn (the ci-build ORCHESTRATOR identity), got "
                    f"{role!r} (ADR-0051 \u00a7orchestrator only)"
                )
    if not found_configure:
        errors.append("no aws-actions/configure-aws-credentials step (OIDC assume of the "
                      "ci-build orchestrator role is required; no long-lived keys)")
    m = FORBIDDEN_ROLE_RE.search(text)
    if m:
        errors.append(
            f"references a superseded deploy/terraform role ARN '{m.group(0)}'; CI may only "
            "assume the ci-build orchestrator role (ADR-0051 deleted the per-env deploy roles)"
        )


def check_contract_version(data: dict, errors: list[str]) -> None:
    wc = get_on_block(data).get("workflow_call") or {}
    outputs = wc.get("outputs") or {}
    if "contract_version" not in outputs:
        errors.append(
            "no 'contract_version' output under on.workflow_call.outputs; version the "
            "reusable workflow contract so breaking changes are explicit (treat it as an API)"
        )


def check_ssot_refs(text: str, errors: list[str]) -> None:
    for ref in ("ADR-0051", "RFC-0020"):
        if ref not in text:
            errors.append(
                f"header does not cite {ref}; the workflow must reference its governing "
                "policy SSOT so implementation and ADR/RFC cannot drift"
            )


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_TARGET)
    if not target.is_file():
        print(f"::error::delivery-model: target workflow not found: {target}")
        return 1

    text = target.read_text(encoding="utf-8")
    data = load_yaml(target)
    runs = collect_run_blocks(data)

    errors: list[str] = []
    notices: list[str] = []

    check_no_local_build(runs, errors)
    check_single_canonical_build(runs, errors)
    check_dev_only_pin(runs, errors, notices)
    check_permissions(data, errors)
    check_main_guard(runs, errors)
    check_oidc_role(data, text, errors)
    check_contract_version(data, errors)
    check_ssot_refs(text, errors)

    if notices:
        print("::group::delivery-model observations")
        for n in notices:
            print(f"::warning::{n}")
        print("::endgroup::")

    if errors:
        print("::group::delivery-model violations")
        for e in errors:
            print(f"::error::{e}")
        print("::endgroup::")
        print(f"delivery-model: FAILED with {len(errors)} violation(s) in {target}.")
        print("Policy SSOT: ADR-0051 + RFC-0020. This check is the executable form of that model.")
        return 1

    print(f"delivery-model: OK ({target} upholds build-once / promote-by-digest: "
          "no local build/publish, single canonical build, dev-only pin, least-privilege "
          "permissions, main-only, ci-build OIDC role, versioned contract). SSOT: ADR-0051/RFC-0020.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
