#!/usr/bin/env python3
"""Delivery-model guard (CI). Executable, data-driven policy-as-code for the
build-once / promote-by-digest delivery model.

This is the enforcement twin of the canonical reusable deploy workflow
(.github/workflows/deploy-reusable.yml). It turns architectural policy into
machine-verifiable controls so the model cannot silently regress.

Framework (how mature governance systems layer):
  ADR/RFC (intent) -> control catalog (policy + severity + ownership + lifecycle)
                   -> detector (verifies compliance) -> CI (executes).

Design:
  * DATA-DRIVEN. The control catalog (controls/delivery-model.yaml) defines POLICY ONLY.
    This file provides DETECTOR implementations; each control's `detector` binds it to a
    function here. The detection mechanism is implementation-independent and may evolve
    (regex -> AST -> CodeQL) without touching the catalog.
  * SCHEMA-VALIDATED. The catalog is validated (required fields, enums, unique stable IDs)
    - the seed of a shared cross-domain control schema.
  * SEVERITY- + LIFECYCLE-AWARE. critical/major fail CI; minor is advisory (--fail-on).
    Only `active` controls are evaluated; deprecated/superseded are skipped.
  * EVIDENCE-PRODUCING. Every evaluated control emits evidence (pass or fail), with line
    numbers where relevant, so results are auditable - not a bare PASS/FAIL.
  * ACTIONABLE. Failing controls print the catalog `remediation`.
  * MACHINE-READABLE. --format json (and --report PATH) emit per-control results.
  * SELF-DOCUMENTING. --write-docs / --verify-docs generate and drift-gate the control
    table in a Markdown file so docs stay in sync with the catalog.

Policy SSOT (architecture owned by the ADR/RFC; this checker only enforces it):
  ADR-0051 - Artifact Promotion, Digest-Pinned Deployment, and Registry Segregation
  RFC-0020 - Supply-Chain Integrity and Artifact Promotion

Usage:
  check-delivery-model.py [workflow] [--controls PATH] [--format text|json|markdown]
                          [--fail-on critical|major|minor] [--scope SCOPE] [--report PATH]
  check-delivery-model.py --write-docs README.md      # regenerate the docs block
  check-delivery-model.py --verify-docs README.md     # fail if the docs block drifted

SSOT: this file lives in coderaxis/github-actions and is invoked by the self-CI
delivery-model-guard workflow. It is the delivery-model analogue of
scripts/check-seed-contract.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

DEFAULT_TARGET = ".github/workflows/deploy-reusable.yml"
DEFAULT_CONTROLS = Path(__file__).resolve().parent.parent / "controls" / "delivery-model.yaml"
SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
VALID_SEVERITY = set(SEVERITY_ORDER)
VALID_STATUS = {"active", "deprecated", "superseded"}
VALID_SCOPE = {"reusable-workflow", "caller-workflow", "promotion-controller"}
REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity",
                   "policy", "rationale", "remediation", "detector", "refs")

DOCS_BEGIN = "<!-- BEGIN delivery-controls (generated: scripts/check-delivery-model.py --write-docs) -->"
DOCS_END = "<!-- END delivery-controls -->"

# --- detection surfaces (implementation of the policy; not referenced by the catalog) --
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
PIN_ENV_RE = re.compile(r"(?:set-image|release-metadata|gitops[\w-]*)[\w.-]*\.sh\s+\S+\s+([^\s;)&|]+)")
QUALIFIED_OVERLAY_RE = re.compile(r"overlays/(staging|preprod|prod)\b")
QUALIFIED_ENVS = ("staging", "preprod", "prod")
FORBIDDEN_ROLE_RE = re.compile(r":role/[\w.+=,@-]*(deploy|terraform-apply)[\w.+=,@-]*", re.IGNORECASE)
DOCS_TAG_RES = [
    (re.compile(r"GO_BUILD_TAGS\s*=\s*[\"']?[^\"'\n]*swagger", re.IGNORECASE), "GO_BUILD_TAGS=...swagger"),
    (re.compile(r"name=GO_BUILD_TAGS,value=[^,\n]*swagger", re.IGNORECASE), "CodeBuild GO_BUILD_TAGS override =...swagger"),
    (re.compile(r"--build-arg\s+GO_BUILD_TAGS=[^\s]*swagger", re.IGNORECASE), "--build-arg GO_BUILD_TAGS=...swagger"),
    (re.compile(r"-tags[=\s]+[\"']?[^\s\"']*swagger", re.IGNORECASE), "-tags swagger"),
]


@dataclass
class Finding:
    ok: bool
    evidence: str


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
        on = self.data.get("on")
        if on is None:  # YAML 1.1 parses the bare key `on:` as boolean True
            on = self.data.get(True)
        return on if isinstance(on, dict) else {}

    def wc_outputs(self) -> dict:
        wc = self.on_block().get("workflow_call") or {}
        return wc.get("outputs") or {}

    def lines(self, pattern) -> list[int]:
        rx = pattern if hasattr(pattern, "search") else re.compile(re.escape(str(pattern)))
        return [i for i, line in enumerate(self.text.splitlines(), 1) if rx.search(line)]

    def first_line(self, pattern) -> str:
        hits = self.lines(pattern)
        return f" (line {hits[0]})" if hits else ""


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


# --- detectors: (Workflow) -> Finding(ok, evidence) --------------------------

def no_local_artifact_publish(wf: Workflow) -> Finding:
    hits = []
    for pat, label in FORBIDDEN_BUILD:
        if any(pat.search(block) for block in wf.runs):
            hits.append(f"{label}{wf.first_line(pat)}")
    if hits:
        return Finding(False, "local build/publish present: " + ", ".join(sorted(set(hits))))
    return Finding(True, f"no local build/publish commands across {len(wf.runs)} run block(s)")


def single_canonical_build(wf: Workflow) -> Finding:
    n = sum(len(CANONICAL_BUILD_RE.findall(b)) for b in wf.runs)
    locs = wf.lines(CANONICAL_BUILD_RE)
    if n == 0:
        return Finding(False, "no 'aws codebuild start-build' invocation found")
    if n > 1:
        return Finding(False, f"{n} 'aws codebuild start-build' invocations (lines "
                              f"{', '.join(map(str, locs))}); build-once violated")
    return Finding(True, f"exactly one 'aws codebuild start-build'{wf.first_line(CANONICAL_BUILD_RE)}")


def no_qualified_env_write(wf: Workflow) -> Finding:
    bad: list[str] = []
    envs: set[str] = set()
    for block in wf.runs:
        for m in PIN_ENV_RE.finditer(block):
            env = _strip(m.group(1))
            envs.add(env)
            if env in QUALIFIED_ENVS:
                bad.append(f"pin -> {env}")
        for m in QUALIFIED_OVERLAY_RE.finditer(block):
            bad.append(f"writes overlays/{m.group(1)}")
    if bad:
        return Finding(False, "qualified-env write(s): " + "; ".join(sorted(set(bad))))
    return Finding(True, "overlay pin targets only: " + (", ".join(sorted(envs)) or "none detected"))


def orchestrator_role_only(wf: Workflow) -> Finding:
    m = FORBIDDEN_ROLE_RE.search(wf.text)
    if m:
        return Finding(False, f"superseded deploy/terraform role ARN '{m.group(0)}'"
                              f"{wf.first_line(re.compile(re.escape(m.group(0))))}")
    for step in wf.steps:
        if "aws-actions/configure-aws-credentials" in str(step.get("uses", "")):
            role = str((step.get("with") or {}).get("role-to-assume", ""))
            if role and "ci_build_role_arn" not in role:
                return Finding(False, f"role-to-assume={role!r} is not inputs.ci_build_role_arn")
            return Finding(True, "assumes inputs.ci_build_role_arn (ci-build orchestrator)")
    return Finding(True, "no forbidden deploy/terraform role ARN referenced")


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
            out.append(f"{where} grants '{scope}: {level}'")


def least_privilege_permissions(wf: Workflow) -> Finding:
    problems: list[str] = []
    top = wf.data.get("permissions")
    if top is None:
        problems.append("no top-level permissions block")
    else:
        _perm_problems(top, "top-level", problems)
    for name, job in (wf.data.get("jobs") or {}).items():
        if isinstance(job, dict) and "permissions" in job:
            _perm_problems(job["permissions"], f"job '{name}'", problems)
    if problems:
        return Finding(False, "; ".join(problems))
    return Finding(True, f"permissions = {top}")


def build_from_main_guard(wf: Workflow) -> Finding:
    for block in wf.runs:
        if "GITHUB_REF_NAME" in block and "main" in block and ("exit 1" in block or "::error" in block):
            return Finding(True, f"build-from-main guard present{wf.first_line('GITHUB_REF_NAME')}")
    return Finding(False, "no run step fails the build when GITHUB_REF_NAME != main")


def _grants_id_token(wf: Workflow) -> bool:
    def has(perms) -> bool:
        return isinstance(perms, dict) and str(perms.get("id-token")) == "write"
    if has(wf.data.get("permissions")):
        return True
    return any(isinstance(j, dict) and has(j.get("permissions")) for j in (wf.data.get("jobs") or {}).values())


def oidc_configured(wf: Workflow) -> Finding:
    steps = [s for s in wf.steps if "aws-actions/configure-aws-credentials" in str(s.get("uses", ""))]
    if not steps:
        return Finding(False, "no configure-aws-credentials (OIDC) step")
    if not _grants_id_token(wf):
        return Finding(False, "id-token: write is not granted, so OIDC cannot mint credentials")
    return Finding(True, f"OIDC via {steps[0].get('uses')} + id-token: write")


def gitops_digest_pin_present(wf: Workflow) -> Finding:
    for block in wf.runs:
        refs_infra = "INFRA_REPO" in block or "infra_repo" in block
        refs_image = any(t in block for t in ("image_ref", "image_digest", "IMAGE_REF", "IMAGE_DIGEST"))
        if refs_infra and refs_image:
            return Finding(True, "built image (by digest/ref) is pinned into the GitOps infra repo")
    return Finding(False, "no step pins the built image into the GitOps infra repo (dev overlay pin missing)")


def output_contract_version(wf: Workflow) -> Finding:
    ok = "contract_version" in wf.wc_outputs()
    return Finding(ok, "on.workflow_call.outputs.contract_version declared" if ok
                   else "on.workflow_call.outputs.contract_version is missing")


def output_image_digest(wf: Workflow) -> Finding:
    ok = "image_digest" in wf.wc_outputs()
    return Finding(ok, "on.workflow_call.outputs.image_digest declared" if ok
                   else "on.workflow_call.outputs.image_digest is missing")


def no_docs_build_variant(wf: Workflow) -> Finding:
    for pat, label in DOCS_TAG_RES:
        if any(pat.search(block) for block in wf.runs):
            return Finding(False, f"docs/build-variant tag injected: {label}{wf.first_line(pat)}")
    return Finding(True, "no swagger/docs build-variant tag injected into the canonical build")


def cites_policy_ssot(wf: Workflow) -> Finding:
    missing = [r for r in ("ADR-0051", "RFC-0020") if r not in wf.text]
    if missing:
        return Finding(False, f"header omits policy SSOT citation(s): {', '.join(missing)}")
    return Finding(True, "header cites ADR-0051 and RFC-0020")


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
    errors: list[str] = []
    seen: set[str] = set()
    for i, c in enumerate(doc["controls"]):
        cid = c.get("id", f"#{i}")
        missing = [f for f in REQUIRED_FIELDS if not c.get(f)]
        if missing:
            errors.append(f"{cid}: missing required field(s) {missing}")
        if c.get("severity") not in VALID_SEVERITY:
            errors.append(f"{cid}: severity {c.get('severity')!r} not in {sorted(VALID_SEVERITY)}")
        if c.get("status") not in VALID_STATUS:
            errors.append(f"{cid}: status {c.get('status')!r} not in {sorted(VALID_STATUS)}")
        if c.get("scope") not in VALID_SCOPE:
            errors.append(f"{cid}: scope {c.get('scope')!r} not in {sorted(VALID_SCOPE)}")
        if c.get("detector") not in DETECTORS:
            errors.append(f"{cid}: unknown detector {c.get('detector')!r}")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id (IDs must be unique and stable)")
        seen.add(cid)
    if errors:
        for e in errors:
            print(f"::error::delivery-model catalog invalid: {e}")
        raise SystemExit(1)
    return doc


def evaluate(wf: Workflow, controls: list[dict], scope_filter: str | None = None) -> list[dict]:
    results = []
    for c in controls:
        rec = {
            "control": c["id"], "title": c["title"], "severity": c["severity"],
            "scope": c["scope"], "owner": c["owner"], "status": c["status"],
            "result": None, "evidence": "", "remediation": "",
        }
        if c["status"] != "active":
            rec.update(result="skipped", evidence=f"lifecycle status={c['status']} (not evaluated)")
        elif scope_filter and c["scope"] != scope_filter:
            rec.update(result="skipped", evidence=f"scope {c['scope']} filtered out (--scope {scope_filter})")
        else:
            f = DETECTORS[c["detector"]](wf)
            rec.update(result="pass" if f.ok else "fail", evidence=f.evidence,
                       remediation="" if f.ok else " ".join(str(c["remediation"]).split()))
        results.append(rec)
    return results


def is_enforced(rec: dict, threshold: int) -> bool:
    if rec["result"] == "error":
        return True
    return rec["result"] == "fail" and SEVERITY_ORDER.get(rec["severity"], 2) >= threshold


def build_report(wf: Workflow, results: list[dict], ssot: list[str], fail_on: str, threshold: int) -> dict:
    enforced = [r for r in results if is_enforced(r, threshold)]
    return {
        "workflow": str(wf.path),
        "policy_ssot": ssot,
        "fail_on": fail_on,
        "controls_total": len(results),
        "evaluated": sum(1 for r in results if r["result"] in ("pass", "fail")),
        "passed": sum(1 for r in results if r["result"] == "pass"),
        "failed": sum(1 for r in results if r["result"] in ("fail", "error")),
        "skipped": sum(1 for r in results if r["result"] == "skipped"),
        "enforced_failures": len(enforced),
        "ok": not enforced,
        "results": results,
    }


def render_text(report: dict, threshold: int) -> None:
    results = report["results"]
    evaluated = [r for r in results if r["result"] in ("pass", "fail", "error")]
    print("::group::delivery-model controls (evidence)")
    for r in evaluated:
        mark = "ok" if r["result"] == "pass" else "XX"
        print(f"[{mark}] {r['control']} [{r['severity']}/{r['scope']}/{r['owner']}] "
              f"{r['title']}: {r['evidence']}")
    skipped = [r for r in results if r["result"] == "skipped"]
    for r in skipped:
        print(f"[--] {r['control']} skipped: {r['evidence']}")
    print("::endgroup::")

    advisory = [r for r in results if r["result"] in ("fail", "error") and not is_enforced(r, threshold)]
    for r in advisory:
        print(f"::warning::[{r['control']}][{r['severity']}] {r['title']} - {r['evidence']} "
              f"(advisory; below fail-on)")

    enforced = [r for r in results if is_enforced(r, threshold)]
    for r in enforced:
        rem = f" | fix: {r['remediation']}" if r["remediation"] else ""
        print(f"::error::[{r['control']}][{r['severity']}] {r['title']} - {r['evidence']}{rem}")

    ssot = ", ".join(report["policy_ssot"])
    if enforced:
        print(f"delivery-model: FAILED - {len(enforced)} enforced violation(s), {len(advisory)} advisory, "
              f"{report['passed']}/{report['evaluated']} evaluated controls passing in {report['workflow']}.")
        print(f"Policy SSOT: {ssot}. Executable form of that model (fail-on={report['fail_on']}).")
    else:
        print(f"delivery-model: OK - {report['passed']}/{report['evaluated']} controls upheld "
              f"({report['skipped']} skipped) in {report['workflow']}"
              + (f"; {len(advisory)} advisory" if advisory else "") + ".")
        print(f"Policy SSOT: {ssot} (fail-on={report['fail_on']}).")


# --- generated docs (single source: the catalog) -----------------------------

def render_docs(doc: dict) -> str:
    domain = doc.get("domain", "delivery-model")
    lines = [
        DOCS_BEGIN,
        "",
        f"_Generated from `controls/{domain}.yaml` by `scripts/check-delivery-model.py "
        "--write-docs` — do not edit by hand._",
        "",
        "| Control | Policy | Severity | Scope | Owner | Status |",
        "| ------- | ------ | -------- | ----- | ----- | ------ |",
    ]
    for c in doc["controls"]:
        policy = " ".join(str(c["policy"]).split())
        lines.append(f"| {c['id']} | {policy} | {c['severity']} | {c['scope']} | {c['owner']} | {c['status']} |")
    lines += ["", DOCS_END]
    return "\n".join(lines)


def _extract_block(text: str) -> str | None:
    if DOCS_BEGIN in text and DOCS_END in text:
        return DOCS_BEGIN + text.split(DOCS_BEGIN, 1)[1].split(DOCS_END, 1)[0] + DOCS_END
    return None


def write_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    block = render_docs(doc)
    if DOCS_BEGIN not in text or DOCS_END not in text:
        print(f"::error::{path}: markers not found. Add these two lines where the table should go:\n"
              f"  {DOCS_BEGIN}\n  {DOCS_END}")
        return 1
    new = text.split(DOCS_BEGIN, 1)[0] + block + text.split(DOCS_END, 1)[1]
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"delivery-model: wrote generated control table into {path}")
    else:
        print(f"delivery-model: {path} control table already up to date")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    current = _extract_block(text)
    if current is None:
        print(f"::error::{path}: generated-controls markers not found; run --write-docs")
        return 1
    if current.strip() != render_docs(doc).strip():
        print(f"::error::{path}: control table is out of sync with controls catalog; "
              "run: python3 scripts/check-delivery-model.py --write-docs " + str(path))
        return 1
    print(f"delivery-model: {path} control table is in sync with the catalog")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Delivery-model guard (executable form of ADR-0051).")
    ap.add_argument("workflow", nargs="?", default=DEFAULT_TARGET, help="workflow file to check")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    ap.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    ap.add_argument("--scope", choices=sorted(VALID_SCOPE), help="only evaluate controls in this scope")
    ap.add_argument("--report", help="write the JSON report to this path")
    ap.add_argument("--write-docs", metavar="FILE", help="regenerate the control table in FILE and exit")
    ap.add_argument("--verify-docs", metavar="FILE", help="fail if FILE's control table drifted; then exit")
    args = ap.parse_args(argv[1:])

    controls_path = Path(args.controls)
    if not controls_path.is_file():
        print(f"::error::delivery-model: control catalog not found: {controls_path}")
        return 1
    doc = load_controls(controls_path)

    if args.write_docs:
        return write_docs(doc, Path(args.write_docs))
    if args.verify_docs:
        return verify_docs(doc, Path(args.verify_docs))
    if args.format == "markdown":
        print(render_docs(doc))
        return 0

    target = Path(args.workflow)
    if not target.is_file():
        print(f"::error::delivery-model: target workflow not found: {target}")
        return 1

    wf = Workflow(target)
    results = evaluate(wf, doc["controls"], scope_filter=args.scope)
    threshold = SEVERITY_ORDER[args.fail_on]
    report = build_report(wf, results, doc.get("policy_ssot", []), args.fail_on, threshold)

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        render_text(report, threshold)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
