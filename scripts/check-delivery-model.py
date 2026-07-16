#!/usr/bin/env python3
"""Delivery-model guard (CI). Executable, data-driven form of the build-once /
promote-by-digest delivery model.

This is the enforcement twin of the canonical reusable deploy workflow
(.github/workflows/deploy-reusable.yml). It turns architectural policy into
machine-verifiable controls so the model cannot silently regress.

Design (so the governance engine ages well):
  * DATA-DRIVEN. The catalog of controls - their id, human-readable policy, and
    severity - lives in controls/delivery-model.yaml. This file provides the DETECTOR
    implementations; each control's `detector` key binds it to a function here. Add or
    retune a control in YAML without editing detection logic.
  * SEMANTIC / BEHAVIOUR-ORIENTED. Controls state a policy ("never publish a container
    artifact", "pin the digest into GitOps") that outlives today's tools. Detectors are
    the swappable implementation of that policy.
  * POSITIVE + NEGATIVE. It both rejects regressions (a docker build, a prod pin, a
    deploy role) and requires invariants to EXIST (OIDC configured, digest pinned,
    contract_version / image_digest outputs) so accidental deletions also fail.
  * SEVERITY-AWARE. critical/major fail CI; minor is advisory (warning). Tune with
    --fail-on {critical,major,minor}.
  * MACHINE-READABLE. --format json (and --report PATH) emit per-control results for
    dashboards / compliance tooling.

Policy SSOT (architecture owned by the ADR/RFC; this checker only enforces it):
  ADR-0051 - Artifact Promotion, Digest-Pinned Deployment, and Registry Segregation
  RFC-0020 - Supply-Chain Integrity and Artifact Promotion

Usage:
  check-delivery-model.py [workflow] [--controls PATH] [--format text|json]
                          [--fail-on critical|major|minor] [--report PATH]

SSOT: this file lives in coderaxis/github-actions and is invoked by the self-CI
delivery-model-guard workflow. It is the delivery-model analogue of
scripts/check-seed-contract.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

DEFAULT_TARGET = ".github/workflows/deploy-reusable.yml"
DEFAULT_CONTROLS = Path(__file__).resolve().parent.parent / "controls" / "delivery-model.yaml"
SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}

# --- detection surfaces -------------------------------------------------------
# Local container build/publish tools. This is the CURRENT detection surface for the
# DM-001 policy ("CI never builds or publishes a container artifact"); the policy, not
# the list, is the invariant. Extend the list as new tools appear.
FORBIDDEN_BUILD = [
    (re.compile(r"\bdocker\s+buildx\b"), "docker buildx"),
    (re.compile(r"\bdocker\s+build\b"), "docker build"),
    (re.compile(r"\bdocker\s+push\b"), "docker push"),
    (re.compile(r"\bpodman\s+(?:build|push)\b"), "podman build/push"),
    (re.compile(r"\bbuildah\b"), "buildah"),
    (re.compile(r"\bnerdctl\s+(?:build|push)\b"), "nerdctl build/push"),
    (re.compile(r"\bbuildctl\b"), "buildctl"),
    (re.compile(r"\bkaniko\b|/kaniko/executor"), "kaniko"),
    (re.compile(r"\bko\s+(?:build|publish|apply)\b"), "ko build/publish"),
    (re.compile(r"\bpack\s+build\b"), "pack build"),
    (re.compile(r"\bimg\s+build\b"), "img build"),
    (re.compile(r"\bcrane\s+(?:push|copy|append)\b"), "crane push/copy"),
    (re.compile(r"\bskopeo\s+copy\b"), "skopeo copy"),
]
CANONICAL_BUILD_RE = re.compile(r"\baws\s+codebuild\s+start-build\b")
# Pin-script invocations (behaviour): capture the env argument for any gitops-ish pin
# helper, not just one specific script name.
PIN_ENV_RE = re.compile(
    r"(?:set-image|release-metadata|gitops[\w-]*)[\w.-]*\.sh\s+\S+\s+([^\s;)&|]+)"
)
QUALIFIED_OVERLAY_RE = re.compile(r"overlays/(staging|preprod|prod)\b")
QUALIFIED_ENVS = ("staging", "preprod", "prod")
FORBIDDEN_ROLE_RE = re.compile(r":role/[\w.+=,@-]*(deploy|terraform-apply)[\w.+=,@-]*", re.IGNORECASE)
# Docs/build-variant tag injection (would fork the artifact / put swagger in prod).
DOCS_TAG_RES = [
    (re.compile(r"GO_BUILD_TAGS\s*=\s*[\"']?[^\"'\n]*swagger", re.IGNORECASE), "GO_BUILD_TAGS=...swagger"),
    (re.compile(r"name=GO_BUILD_TAGS,value=[^,\n]*swagger", re.IGNORECASE), "CodeBuild GO_BUILD_TAGS override =...swagger"),
    (re.compile(r"--build-arg\s+GO_BUILD_TAGS=[^\s]*swagger", re.IGNORECASE), "--build-arg GO_BUILD_TAGS=...swagger"),
    (re.compile(r"-tags[=\s]+[\"']?[^\s\"']*swagger", re.IGNORECASE), "-tags swagger"),
]


class Workflow:
    """Parsed view of the target workflow shared by all detectors."""

    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(self.text)
        if not isinstance(data, dict):
            raise SystemExit(f"::error::{path}: not a mapping / invalid workflow YAML")
        self.data = data
        self.runs = _collect_runs(data)
        self.steps = list(_iter_steps(data))

    def on_block(self) -> dict:
        # YAML 1.1 parses the bare key `on:` as boolean True; workflows rely on it.
        on = self.data.get("on")
        if on is None:
            on = self.data.get(True)
        return on if isinstance(on, dict) else {}

    def wc_outputs(self) -> dict:
        wc = self.on_block().get("workflow_call") or {}
        return wc.get("outputs") or {}


def _collect_runs(data: dict) -> list[str]:
    runs: list[str] = []
    for job in (data.get("jobs") or {}).values():
        if isinstance(job, dict):
            for step in job.get("steps") or []:
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    runs.append(step["run"])
    return runs


def _iter_steps(data: dict):
    for job in (data.get("jobs") or {}).values():
        if isinstance(job, dict):
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    yield step


def _strip(arg: str) -> str:
    return arg.strip().strip('"').strip("'")


# --- detectors: (Workflow) -> None if OK, else a reason string ---------------

def no_local_artifact_publish(wf: Workflow):
    hits = sorted({label for block in wf.runs for pat, label in FORBIDDEN_BUILD if pat.search(block)})
    if hits:
        return (f"CI builds/publishes a container artifact locally ({', '.join(hits)}); the central "
                "build executor is the sole publish identity")
    return None


def single_canonical_build(wf: Workflow):
    n = sum(len(CANONICAL_BUILD_RE.findall(b)) for b in wf.runs)
    if n == 0:
        return "no canonical build orchestrated ('aws codebuild start-build' is absent)"
    if n > 1:
        return (f"{n} 'aws codebuild start-build' invocations; build-once is violated - a second build "
                "means a per-environment or per-variant (e.g. docs) image")
    return None


def no_qualified_env_write(wf: Workflow):
    bad: list[str] = []
    for block in wf.runs:
        for m in PIN_ENV_RE.finditer(block):
            env = _strip(m.group(1))
            if env in QUALIFIED_ENVS:
                bad.append(f"pin targets '{env}'")
        for m in QUALIFIED_OVERLAY_RE.finditer(block):
            bad.append(f"writes overlays/{m.group(1)}")
    if bad:
        return ("; ".join(sorted(set(bad))) + " - only the dev overlay may be written here; "
                "qualified envs are promoted by the Promotion Controller, never pinned/built here")
    return None


def orchestrator_role_only(wf: Workflow):
    m = FORBIDDEN_ROLE_RE.search(wf.text)
    if m:
        return (f"references a superseded deploy/terraform role ARN '{m.group(0)}'; CI may only assume "
                "the ci-build orchestrator role")
    for step in wf.steps:
        if "aws-actions/configure-aws-credentials" in str(step.get("uses", "")):
            role = str((step.get("with") or {}).get("role-to-assume", ""))
            if role and "ci_build_role_arn" not in role:
                return (f"configure-aws-credentials assumes {role!r}, not inputs.ci_build_role_arn "
                        "(the ci-build orchestrator identity)")
    return None


ALLOWED_PERMS = {"contents": {"read", "none"}, "id-token": {"write", "none"}}


def _perm_problems(perms, where: str, out: list[str]) -> None:
    if perms is None:
        return
    if isinstance(perms, str):
        if "write" in perms:
            out.append(f"{where} permissions '{perms}' grants write")
        return
    if not isinstance(perms, dict):
        out.append(f"{where} permissions block is malformed")
        return
    for scope, level in perms.items():
        lvl = str(level)
        if scope in ALLOWED_PERMS:
            if lvl not in ALLOWED_PERMS[scope]:
                out.append(f"{where} '{scope}: {level}' exceeds least privilege")
        elif lvl == "write":
            out.append(f"{where} grants '{scope}: {level}' (only contents: read + id-token: write allowed)")


def least_privilege_permissions(wf: Workflow):
    problems: list[str] = []
    top = wf.data.get("permissions")
    if top is None:
        problems.append("no top-level permissions block (declare least privilege explicitly)")
    else:
        _perm_problems(top, "top-level", problems)
    for name, job in (wf.data.get("jobs") or {}).items():
        if isinstance(job, dict) and "permissions" in job:
            _perm_problems(job["permissions"], f"job '{name}'", problems)
    return "; ".join(problems) if problems else None


def build_from_main_guard(wf: Workflow):
    for block in wf.runs:
        if "GITHUB_REF_NAME" in block and "main" in block and ("exit 1" in block or "::error" in block):
            return None
    return "no build-from-main guard: a run step must fail the build when GITHUB_REF_NAME != main"


def _grants_id_token(wf: Workflow) -> bool:
    def has(perms) -> bool:
        return isinstance(perms, dict) and str(perms.get("id-token")) == "write"
    if has(wf.data.get("permissions")):
        return True
    return any(isinstance(j, dict) and has(j.get("permissions")) for j in (wf.data.get("jobs") or {}).values())


def oidc_configured(wf: Workflow):
    has_step = any("aws-actions/configure-aws-credentials" in str(s.get("uses", "")) for s in wf.steps)
    if not has_step:
        return "no OIDC configure-aws-credentials step (short-lived, keyless credentials are required)"
    if not _grants_id_token(wf):
        return "id-token: write is not granted, so OIDC cannot mint credentials"
    return None


def gitops_digest_pin_present(wf: Workflow):
    for block in wf.runs:
        refs_infra = "INFRA_REPO" in block or "infra_repo" in block
        refs_image = any(t in block for t in ("image_ref", "image_digest", "IMAGE_REF", "IMAGE_DIGEST"))
        if refs_infra and refs_image:
            return None
    return ("no step pins the built image (by digest/ref) into the GitOps infra repo; the dev "
            "overlay pin appears to be missing")


def output_contract_version(wf: Workflow):
    return None if "contract_version" in wf.wc_outputs() else "on.workflow_call.outputs.contract_version is missing"


def output_image_digest(wf: Workflow):
    return None if "image_digest" in wf.wc_outputs() else "on.workflow_call.outputs.image_digest is missing"


def no_docs_build_variant(wf: Workflow):
    for block in wf.runs:
        for pat, label in DOCS_TAG_RES:
            if pat.search(block):
                return (f"injects a docs/build-variant tag ({label}) into the canonical build; swagger "
                        "is never compiled into the promoted image (single-artifact parity across envs)")
    return None


def cites_policy_ssot(wf: Workflow):
    missing = [r for r in ("ADR-0051", "RFC-0020") if r not in wf.text]
    return f"header omits policy SSOT citation(s): {', '.join(missing)}" if missing else None


DETECTORS = {
    "no_local_artifact_publish": no_local_artifact_publish,
    "single_canonical_build": single_canonical_build,
    "no_qualified_env_write": no_qualified_env_write,
    "orchestrator_role_only": orchestrator_role_only,
    "no_docs_build_variant": no_docs_build_variant,
    "least_privilege_permissions": least_privilege_permissions,
    "build_from_main_guard": build_from_main_guard,
    "oidc_configured": oidc_configured,
    "gitops_digest_pin_present": gitops_digest_pin_present,
    "output_contract_version": output_contract_version,
    "output_image_digest": output_image_digest,
    "cites_policy_ssot": cites_policy_ssot,
}


def load_controls(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("controls"), list):
        raise SystemExit(f"::error::{path}: invalid control catalog (expected a 'controls:' list)")
    return doc


def evaluate(wf: Workflow, controls: list[dict]) -> list[dict]:
    results = []
    for c in controls:
        cid, sev = c.get("id", "?"), c.get("severity", "major")
        det = DETECTORS.get(c.get("detector", ""))
        if det is None:
            results.append({"control": cid, "title": c.get("title", ""), "severity": sev,
                            "status": "error", "reason": f"unknown detector {c.get('detector')!r}"})
            continue
        if sev not in SEVERITY_ORDER:
            results.append({"control": cid, "title": c.get("title", ""), "severity": sev,
                            "status": "error", "reason": f"unknown severity {sev!r}"})
            continue
        reason = det(wf)
        results.append({"control": cid, "title": c.get("title", ""), "severity": sev,
                        "status": "fail" if reason else "pass", "reason": reason})
    return results


def is_enforced(result: dict, threshold: int) -> bool:
    if result["status"] == "error":
        return True
    return result["status"] == "fail" and SEVERITY_ORDER.get(result["severity"], 2) >= threshold


def render_text(wf: Workflow, results: list[dict], ssot: list[str], fail_on: str, threshold: int) -> bool:
    enforced = [r for r in results if is_enforced(r, threshold)]
    advisory = [r for r in results if r["status"] in ("fail", "error") and not is_enforced(r, threshold)]
    passed = [r for r in results if r["status"] == "pass"]

    if advisory:
        print("::group::delivery-model advisories (below fail-on)")
        for r in advisory:
            print(f"::warning::[{r['control']}][{r['severity']}] {r['title']} - {r['reason']}")
        print("::endgroup::")

    if enforced:
        print("::group::delivery-model violations")
        for r in enforced:
            print(f"::error::[{r['control']}][{r['severity']}] {r['title']} - {r['reason']}")
        print("::endgroup::")
        print(f"delivery-model: FAILED - {len(enforced)} enforced violation(s), "
              f"{len(advisory)} advisory, {len(passed)}/{len(results)} controls passing in {wf.path}.")
        print(f"Policy SSOT: {', '.join(ssot)}. This check is the executable form of that model "
              f"(fail-on={fail_on}).")
        return False

    passed_ids = ", ".join(r["control"] for r in passed)
    print(f"delivery-model: OK - {len(passed)}/{len(results)} controls upheld in {wf.path} "
          f"[{passed_ids}]" + (f"; {len(advisory)} advisory" if advisory else "") + ".")
    print(f"Policy SSOT: {', '.join(ssot)} (fail-on={fail_on}).")
    return True


def build_report(wf: Workflow, results: list[dict], ssot: list[str], fail_on: str, threshold: int) -> dict:
    enforced = [r for r in results if is_enforced(r, threshold)]
    return {
        "workflow": str(wf.path),
        "policy_ssot": ssot,
        "fail_on": fail_on,
        "controls_total": len(results),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "failed": sum(1 for r in results if r["status"] in ("fail", "error")),
        "enforced_failures": len(enforced),
        "ok": not enforced,
        "results": results,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Delivery-model guard (executable form of ADR-0051).")
    ap.add_argument("workflow", nargs="?", default=DEFAULT_TARGET, help="workflow file to check")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    ap.add_argument("--report", help="write the JSON report to this path (in any format mode)")
    args = ap.parse_args(argv[1:])

    target = Path(args.workflow)
    if not target.is_file():
        print(f"::error::delivery-model: target workflow not found: {target}")
        return 1
    controls_path = Path(args.controls)
    if not controls_path.is_file():
        print(f"::error::delivery-model: control catalog not found: {controls_path}")
        return 1

    doc = load_controls(controls_path)
    ssot = doc.get("policy_ssot", [])
    wf = Workflow(target)
    results = evaluate(wf, doc["controls"])
    threshold = SEVERITY_ORDER[args.fail_on]
    report = build_report(wf, results, ssot, args.fail_on, threshold)

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps(report, indent=2))
        ok = report["ok"]
    else:
        ok = render_text(wf, results, ssot, args.fail_on, threshold)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
