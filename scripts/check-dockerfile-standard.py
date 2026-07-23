#!/usr/bin/env python3
"""Enterprise Dockerfile Standard guard (CI). Run from a service/gateway repo root.

Executable, data-driven policy-as-code for the platform's single canonical Dockerfile
pattern (docs/core-docs/standards/infrastructure/dockerfile-standard.md, ADR-0072). This is
the Dockerfile-domain analogue of check-delivery-model.py / check-seed-contract.py /
event-compliance.sh: the control catalog (controls/dockerfile-standard.yaml) defines POLICY
ONLY; this file provides DETECTOR implementations bound to it by name.

Design (identical framework to check-delivery-model.py):
  ADR (intent) -> control catalog (policy + severity + ownership + lifecycle)
                 -> detector (verifies compliance) -> CI (executes).
  * DATA-DRIVEN, SEVERITY-AWARE (critical/major fail; minor advisory via --fail-on).
  * CAPABILITY-SCOPED: a control with a non-empty `applies_to_capabilities` is only
    evaluated when the caller's declared --capabilities intersects it.
  * DECLARATION-VERIFIED: --capabilities is a claim, not ground truth; DS-013 cross-checks
    it against repo structure (go.mod core-postgres require, cmd/seed, cmd/backfill,
    cmd/canary) so a stale/wrong declaration is itself a failure.
  * STATIC ANALYSIS ONLY. This script never runs `docker build` and never touches a
registry - by design, so it cannot conflict with ADR-0051 DM-001 (CI never builds/
    publishes a container artifact; the central build `inboxxhq-build` is the sole
    builder/publisher).
  * NO DUPLICATE LOGIC: seeding's full contract (binary + data tree + placeholder-only
    qualified envs) is hard-enforced by seed-contract-check.yml; DS-012 here is advisory-
    only cross-check, never a second hard gate for the same fact.
  * SELF-DOCUMENTING: --write-docs / --verify-docs, exactly like check-delivery-model.py.

Usage:
  check-dockerfile-standard.py [dockerfile] --capabilities http-api,db-owner,seed
                              [--controls PATH] [--format text|json|markdown]
                              [--fail-on critical|major|minor] [--report PATH]
  check-dockerfile-standard.py --write-docs README.md
  check-dockerfile-standard.py --verify-docs README.md

SSOT: this file lives in coderaxis/github-actions and is invoked by the central reusable
workflow .github/workflows/dockerfile-standard.yml.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

DEFAULT_TARGET = "Dockerfile"
DEFAULT_CONTROLS = Path(__file__).resolve().parent.parent / "controls" / "dockerfile-standard.yaml"
SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
VALID_SEVERITY = set(SEVERITY_ORDER)
VALID_STATUS = {"active", "deprecated", "superseded"}
VALID_SCOPE = {"caller-workflow"}
REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity", "policy",
                   "rationale", "remediation", "detector", "refs")
KNOWN_CAPABILITIES = {"http-api", "db-owner", "seed", "backfill", "canary",
                      "kafka-producer-dk", "worker", "gateway", "stateless"}

DOCS_BEGIN = "<!-- BEGIN dockerfile-standard-controls (generated: scripts/check-dockerfile-standard.py --write-docs) -->"
DOCS_END = "<!-- END dockerfile-standard-controls -->"

# --- centrally pinned expectations (mirrors dockerfile-version-matrix.yaml) -----------
EXPECTED_BUILDER_TAG = "1.26-alpine3.19"
EXPECTED_RUNTIME_TAG = "3.19"
ALLOWED_VERSION_ARGS = {"BUILDER_IMAGE_TAG", "RUNTIME_IMAGE_TAG", "GO_LANG_VERSION",
                        "TARGETARCH", "RUNTIME_UID", "RUNTIME_GID", "IMAGE_REVISION",
                        "IMAGE_CREATED", "GO_BUILD_TAGS"}
BUILDER_APK_ALLOWLIST = {"ca-certificates", "git", "tzdata"}
RUNTIME_APK_ALLOWLIST = {"ca-certificates", "tzdata", "wget"}
FORBIDDEN_CODEGEN_RE = re.compile(
    r"\bsqlc\s+generate\b|\bprotoc\b|\bbuf\s+generate\b|\bswag\s+init\b|\bopenapi-generator\b"
)
REQUIRED_LABEL_KEYS = (
    "org.opencontainers.image.title", "org.opencontainers.image.source",
    "org.opencontainers.image.vendor", "org.opencontainers.image.licenses",
    "org.opencontainers.image.revision", "org.opencontainers.image.created",
    "com.coderaxis.capabilities",
)


@dataclass
class Finding:
    ok: bool
    evidence: str


@dataclass
class Instr:
    stage: int
    lineno: int
    op: str      # uppercase instruction name, e.g. "FROM", "RUN"
    args: str    # raw remainder of the line (continuations already joined)


class Dockerfile:
    """Parsed view of the target Dockerfile shared by all detectors."""

    def __init__(self, path: Path, repo_root: Path):
        self.path = path
        self.repo_root = repo_root
        self.text = path.read_text(encoding="utf-8")
        self.instructions = _parse_instructions(self.text)
        self.stage_froms = [i for i in self.instructions if i.op == "FROM"]

    def stage_count(self) -> int:
        return len(self.stage_froms)

    def instrs(self, op: str, stage: int | None = None) -> list[Instr]:
        return [i for i in self.instructions
                if i.op == op and (stage is None or i.stage == stage)]

    def all_text(self) -> str:
        return self.text

    def runtime_stage_index(self) -> int:
        return max(0, self.stage_count() - 1)


def _parse_instructions(text: str) -> list[Instr]:
    """Minimal Dockerfile instruction parser: joins line continuations (trailing `\\`),
    strips comments (lines starting with `#`, except we keep the raw text separately for
    marker-comment detectors via all_text()), and tracks stage index (incremented on each
    FROM)."""
    lines = text.splitlines()
    instrs: list[Instr] = []
    stage = -1
    buf: list[str] = []
    start_lineno = None
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if not buf:
            start_lineno = lineno
        # strip trailing continuation backslash (not inside a comment)
        cont = stripped.endswith("\\") and not stripped.startswith("#")
        content = stripped[:-1] if cont else stripped
        buf.append(content)
        if cont:
            continue
        joined = " ".join(b.strip() for b in buf)
        buf = []
        m = re.match(r"^([A-Za-z]+)\s*(.*)$", joined)
        if not m:
            continue
        op = m.group(1).upper()
        args = m.group(2)
        if op == "FROM":
            stage += 1
        instrs.append(Instr(stage=max(stage, 0), lineno=start_lineno or lineno, op=op, args=args))
    return instrs


def _first(hits: list[Instr]) -> Instr | None:
    return hits[0] if hits else None


# --- structural (repo) helpers, mirroring check-seed-contract.py's auto-detection ------

def repo_requires_core_postgres(root: Path) -> bool:
    gomod = root / "go.mod"
    if not gomod.is_file():
        return False
    text = gomod.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"github\.com/coderaxis/[\w-]*-core-postgres\b", text))


def repo_has_cmd(root: Path, name: str) -> bool:
    return (root / "cmd" / name).is_dir()


# --- detectors --------------------------------------------------------------------------

def two_stage_layout(df: Dockerfile) -> Finding:
    n = df.stage_count()
    if n != 2:
        return Finding(False, f"found {n} FROM stage(s); expected exactly 2 (builder + runtime)")
    named = re.search(r"\bAS\s+builder\b", df.stage_froms[0].args, re.IGNORECASE)
    if not named:
        return Finding(False, f"line {df.stage_froms[0].lineno}: first stage is not named "
                                "'AS builder'")
    if re.search(r"\bAS\s+\w+", df.stage_froms[1].args, re.IGNORECASE):
        return Finding(False, f"line {df.stage_froms[1].lineno}: runtime stage must be "
                                "unnamed (final stage)")
    return Finding(True, "exactly 2 stages: builder (named) + unnamed runtime")


def cross_compile_builder(df: Dockerfile) -> Finding:
    f = df.stage_froms[0] if df.stage_froms else None
    if f is None:
        return Finding(False, "no FROM found")
    if "--platform=$BUILDPLATFORM" not in f.args:
        return Finding(False, f"line {f.lineno}: builder FROM missing "
                                "--platform=$BUILDPLATFORM")
    if not re.search(r"GOARCH=\$\{?TARGETARCH\}?", df.all_text()):
        return Finding(False, "no build sets GOARCH=${TARGETARCH}")
    return Finding(True, "builder cross-compiles via BUILDPLATFORM/TARGETARCH")


def version_matrix_pinned(df: Dockerfile) -> Finding:
    if not df.stage_froms:
        return Finding(False, "no FROM found")
    errs = []
    builder_tag = _extract_tag(df.stage_froms[0].args, "golang")
    if builder_tag and builder_tag != EXPECTED_BUILDER_TAG and not builder_tag.startswith("${"):
        errs.append(f"builder tag {builder_tag!r} != expected {EXPECTED_BUILDER_TAG!r}")
    if len(df.stage_froms) > 1:
        runtime_tag = _extract_tag(df.stage_froms[1].args, "alpine")
        if runtime_tag and runtime_tag != EXPECTED_RUNTIME_TAG and not runtime_tag.startswith("${"):
            errs.append(f"runtime tag {runtime_tag!r} != expected {EXPECTED_RUNTIME_TAG!r}")
    if errs:
        return Finding(False, "; ".join(errs))
    return Finding(True, f"builder={EXPECTED_BUILDER_TAG}, runtime={EXPECTED_RUNTIME_TAG} "
                          "(or version-matrix ARGs)")


def _extract_tag(from_args: str, image_hint: str) -> str | None:
    m = re.search(rf"{image_hint}:(\S+)", from_args)
    if not m:
        return None
    tag = m.group(1)
    return re.sub(r"\s+AS\s+.*$", "", tag, flags=re.IGNORECASE)


def _from_image_token(from_args: str) -> str:
    """The image[:tag] token of a FROM line, skipping leading `--flag=value` tokens
    (e.g. --platform=$BUILDPLATFORM) which are not the image reference."""
    for tok in from_args.split():
        if not tok.startswith("--"):
            return tok
    return ""


def no_latest_tag(df: Dockerfile) -> Finding:
    for f in df.stage_froms:
        img = _from_image_token(f.args)
        if img.endswith(":latest") or (":" not in img and "@" not in img and img != "scratch"):
            return Finding(False, f"line {f.lineno}: unpinned/`:latest` base image {img!r}")
    return Finding(True, "every FROM pins an explicit, non-latest tag")


# A USER value is "numeric" if it is a literal digit UID[:GID], OR the sanctioned
# ${RUNTIME_UID}[:${RUNTIME_GID}] ARG reference whose *default* (dockerfile-version-
# matrix.yaml) is itself numeric - the ARG is resolved at build time, this is a static
# checker, so the sanctioned ARG name is accepted as equivalent to its pinned numeric
# default (any other ARG name is not - that is what DS-016 catches separately).
NUMERIC_USER_RE = re.compile(
    r"^(\d+|\$\{?RUNTIME_UID\}?)(:(\d+|\$\{?RUNTIME_GID\}?))?$"
)
ROOT_LITERALS = {"0", "0:0"}


def numeric_nonroot_user(df: Dockerfile) -> Finding:
    users = df.instrs("USER")
    if not users:
        return Finding(False, "no USER instruction; container would run as root")
    last = users[-1]
    val = last.args.strip()
    if not NUMERIC_USER_RE.match(val):
        return Finding(False, f"line {last.lineno}: USER {val!r} is not a numeric UID[:GID] "
                                "(or the sanctioned ${RUNTIME_UID}:${RUNTIME_GID} ARG) - a "
                                "symbolic-only user cannot be pinned by a k8s securityContext")
    if val in ROOT_LITERALS:
        return Finding(False, f"line {last.lineno}: USER is root (UID 0)")
    return Finding(True, f"USER {val} (numeric or pinned RUNTIME_UID/GID ARG, non-root)")


def healthcheck_present_and_consistent(df: Dockerfile) -> Finding:
    hcs = df.instrs("HEALTHCHECK")
    if not hcs:
        return Finding(False, "no HEALTHCHECK instruction")
    exposes = df.instrs("EXPOSE")
    hc_port = re.search(r"localhost:(\d+)", hcs[-1].args)
    # EXPOSE may list multiple ports (e.g. app + metrics + admin); the HEALTHCHECK port
    # only needs to be ONE of them, not necessarily the first token.
    exp_ports = set(re.findall(r"\d+", exposes[-1].args)) if exposes else set()
    if hc_port and exp_ports and hc_port.group(1) not in exp_ports:
        return Finding(False, f"HEALTHCHECK targets port {hc_port.group(1)} but EXPOSE "
                                f"only lists {sorted(exp_ports)}")
    return Finding(True, "HEALTHCHECK present and consistent with EXPOSE")


def worker_healthcheck_not_http(df: Dockerfile) -> Finding:
    hcs = df.instrs("HEALTHCHECK")
    if not hcs:
        return Finding(False, "no HEALTHCHECK at all (a worker still needs a liveness signal)")
    if re.search(r"https?://", hcs[-1].args):
        return Finding(False, f"line {hcs[-1].lineno}: worker capability but HEALTHCHECK "
                                "uses an HTTP probe (no HTTP port is bound)")
    return Finding(True, "non-HTTP HEALTHCHECK present for worker capability")


def required_oci_labels(df: Dockerfile) -> Finding:
    labels_text = " ".join(i.args for i in df.instrs("LABEL"))
    missing = [k for k in REQUIRED_LABEL_KEYS if k not in labels_text]
    if missing:
        return Finding(False, f"missing LABEL key(s): {', '.join(missing)}")
    return Finding(True, "all required OCI/platform labels present")


def exec_form_cmd(df: Dockerfile) -> Finding:
    cmds = df.instrs("CMD") + df.instrs("ENTRYPOINT")
    if not cmds:
        return Finding(False, "no CMD or ENTRYPOINT instruction")
    last = cmds[-1]
    if not re.match(r"^\s*\[.*\]\s*$", last.args):
        return Finding(False, f"line {last.lineno}: {last.op} is not JSON exec form "
                                f"(shell-form interposes /bin/sh as PID 1): {last.args!r}")
    if re.search(r"entrypoint\.sh|docker-entrypoint", df.all_text(), re.IGNORECASE):
        return Finding(False, "an entrypoint shell-script wrapper is referenced; the "
                                "compiled binary must be the entrypoint directly")
    return Finding(True, f"{last.op} is exec-form with no shell wrapper")


def stopsignal_declared(df: Dockerfile) -> Finding:
    ss = df.instrs("STOPSIGNAL")
    if not ss:
        return Finding(False, "no STOPSIGNAL instruction")
    if ss[-1].args.strip().upper() not in ("SIGTERM", "15"):
        return Finding(False, f"STOPSIGNAL {ss[-1].args!r} is non-standard; use SIGTERM "
                                "unless an ADR documents why")
    return Finding(True, "STOPSIGNAL SIGTERM declared explicitly")


def dbowner_ships_dbtool(df: Dockerfile) -> Finding:
    text = df.all_text()
    if "cmd/dbtool" not in text:
        return Finding(False, "no `go build ... ./cmd/dbtool` found; db-owner capability "
                                "requires shipping a dbtool binary (ADR-0062)")
    if not re.search(r"#\s*dbtool binary path:\s*/app/\S+", text):
        return Finding(False, "dbtool is built but the required marker comment "
                                "'# dbtool binary path: /app/<binary>' is missing")
    if not re.search(r"COPY\s+--from=builder.*dbtool", text):
        return Finding(False, "dbtool binary is built but never COPY'd into the runtime "
                                "stage")
    return Finding(True, "dbtool built, copied, and marked")


def seed_ships_seed_binary(df: Dockerfile) -> Finding:
    text = df.all_text()
    if "cmd/seed" not in text:
        return Finding(False, "no `go build ... ./cmd/seed` found (informational only - "
                                "seed-contract-check.yml is the authoritative gate)")
    return Finding(True, "seed binary built (authoritative check: seed-contract-check.yml)")


def capability_declaration_matches_reality(df: Dockerfile, capabilities: set[str]) -> Finding:
    mismatches = []
    real_db_owner = repo_requires_core_postgres(df.repo_root)
    if real_db_owner and "db-owner" not in capabilities:
        mismatches.append("go.mod requires a *-core-postgres module but 'db-owner' is not "
                          "declared")
    if not real_db_owner and "db-owner" in capabilities:
        mismatches.append("'db-owner' is declared but go.mod requires no *-core-postgres "
                          "module")
    for cap, cmd in (("backfill", "backfill"), ("canary", "canary")):
        real = repo_has_cmd(df.repo_root, cmd)
        if real and cap not in capabilities:
            mismatches.append(f"cmd/{cmd} exists but '{cap}' is not declared")
        if not real and cap in capabilities:
            mismatches.append(f"'{cap}' is declared but cmd/{cmd} does not exist")
    if mismatches:
        return Finding(False, "; ".join(mismatches))
    return Finding(True, "declared capabilities match repo structure "
                          "(db-owner/backfill/canary)")


def no_build_time_codegen(df: Dockerfile) -> Finding:
    hits = [i for i in df.instructions if i.op == "RUN" and FORBIDDEN_CODEGEN_RE.search(i.args)]
    if hits:
        lines = ", ".join(str(h.lineno) for h in hits)
        return Finding(False, f"forbidden codegen command found at line(s) {lines} "
                                "(sqlc/protoc/buf/swag/openapi-generator must not run in "
                                "the Dockerfile - commit generated code instead)")
    return Finding(True, "no build-time code generation")


def no_add_instruction(df: Dockerfile) -> Finding:
    hits = df.instrs("ADD")
    if hits:
        lines = ", ".join(str(h.lineno) for h in hits)
        return Finding(False, f"ADD used at line(s) {lines}; use COPY instead")
    return Finding(True, "no ADD instruction (COPY only)")


def no_freeform_version_args(df: Dockerfile) -> Finding:
    bad = []
    for i in df.instrs("ARG"):
        name = i.args.split("=")[0].strip()
        looks_version_like = re.search(r"VERSION|_TAG$|IMAGE$", name, re.IGNORECASE)
        if looks_version_like and name not in ALLOWED_VERSION_ARGS:
            bad.append(f"line {i.lineno}: ARG {name!r}")
    if bad:
        return Finding(False, "unauthorized version-bearing ARG(s): " + "; ".join(bad))
    return Finding(True, "no freeform version ARGs")


def secret_mount_for_credentials(df: Dockerfile) -> Finding:
    text = df.all_text()
    if "GH_TOKEN" not in text and "gh_token" not in text:
        return Finding(True, "no private-module credential handling found (nothing to "
                              "check)")
    if "--mount=type=secret" not in text:
        return Finding(False, "GH_TOKEN is referenced but no --mount=type=secret is used "
                                "- credential may be baked into a layer")
    for i in df.instrs("ARG") + df.instrs("ENV"):
        if re.search(r"GH_TOKEN|GITHUB_TOKEN", i.args, re.IGNORECASE):
            return Finding(False, f"line {i.lineno}: token supplied via {i.op}, not the "
                                "secret mount - it will be baked into image history")
    return Finding(True, "private-module credential supplied via BuildKit secret mount only")


def package_allowlist(df: Dockerfile) -> Finding:
    bad = []
    for i in df.instrs("RUN"):
        m = re.search(r"apk add\s+(?:--no-cache\s+)?([\w\s.-]+?)(?:;|&&|$)", i.args)
        if not m:
            continue
        pkgs = set(m.group(1).split())
        allow = BUILDER_APK_ALLOWLIST if i.stage == 0 else RUNTIME_APK_ALLOWLIST
        extra = pkgs - allow
        if extra:
            bad.append(f"line {i.lineno} (stage {i.stage}): {sorted(extra)}")
    if bad:
        return Finding(False, "package(s) outside the approved allow-list: " + "; ".join(bad))
    return Finding(True, "all apk add packages are within the approved allow-list")


DETECTORS = {
    "two_stage_layout": two_stage_layout,
    "cross_compile_builder": cross_compile_builder,
    "version_matrix_pinned": version_matrix_pinned,
    "no_latest_tag": no_latest_tag,
    "numeric_nonroot_user": numeric_nonroot_user,
    "healthcheck_present_and_consistent": healthcheck_present_and_consistent,
    "worker_healthcheck_not_http": worker_healthcheck_not_http,
    "required_oci_labels": required_oci_labels,
    "exec_form_cmd": exec_form_cmd,
    "stopsignal_declared": stopsignal_declared,
    "dbowner_ships_dbtool": dbowner_ships_dbtool,
    "seed_ships_seed_binary": seed_ships_seed_binary,
    "capability_declaration_matches_reality": None,  # takes extra arg; dispatched specially
    "no_build_time_codegen": no_build_time_codegen,
    "no_add_instruction": no_add_instruction,
    "no_freeform_version_args": no_freeform_version_args,
    "secret_mount_for_credentials": secret_mount_for_credentials,
    "package_allowlist": package_allowlist,
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
        for cap in c.get("applies_to_capabilities") or []:
            if cap not in KNOWN_CAPABILITIES:
                errors.append(f"{cid}: unknown capability {cap!r} in applies_to_capabilities")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id (IDs must be unique and stable)")
        seen.add(cid)
    if errors:
        for e in errors:
            print(f"::error::dockerfile-standard catalog invalid: {e}")
        raise SystemExit(1)
    return doc


def evaluate(df: Dockerfile, controls: list[dict], capabilities: set[str]) -> list[dict]:
    results = []
    for c in controls:
        rec = {
            "control": c["id"], "title": c["title"], "severity": c["severity"],
            "owner": c["owner"], "status": c["status"], "result": None, "evidence": "",
            "remediation": "",
        }
        scope_caps = c.get("applies_to_capabilities") or []
        if c["status"] != "active":
            rec.update(result="skipped", evidence=f"lifecycle status={c['status']} (not evaluated)")
        elif scope_caps and not (capabilities & set(scope_caps)):
            rec.update(result="skipped",
                       evidence=f"capability scope {scope_caps} not in declared {sorted(capabilities)}")
        else:
            if c["detector"] == "capability_declaration_matches_reality":
                f = capability_declaration_matches_reality(df, capabilities)
            else:
                f = DETECTORS[c["detector"]](df)
            rec.update(result="pass" if f.ok else "fail", evidence=f.evidence,
                       remediation="" if f.ok else " ".join(str(c["remediation"]).split()))
        results.append(rec)
    return results


def is_enforced(rec: dict, threshold: int) -> bool:
    if rec["result"] == "error":
        return True
    return rec["result"] == "fail" and SEVERITY_ORDER.get(rec["severity"], 2) >= threshold


def build_report(df: Dockerfile, results: list[dict], ssot: list[str], capabilities: set[str],
                  fail_on: str, threshold: int) -> dict:
    enforced = [r for r in results if is_enforced(r, threshold)]
    return {
        "dockerfile": str(df.path),
        "capabilities": sorted(capabilities),
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
    print(f"::group::dockerfile-standard controls (capabilities: {', '.join(report['capabilities'])})")
    for r in evaluated:
        mark = "ok" if r["result"] == "pass" else "XX"
        print(f"[{mark}] {r['control']} [{r['severity']}/{r['owner']}] {r['title']}: {r['evidence']}")
    for r in results:
        if r["result"] == "skipped":
            print(f"[--] {r['control']} skipped: {r['evidence']}")
    print("::endgroup::")

    advisory = [r for r in results if r["result"] in ("fail", "error") and not is_enforced(r, threshold)]
    for r in advisory:
        print(f"::warning::[{r['control']}][{r['severity']}] {r['title']} - {r['evidence']} "
              "(advisory; below fail-on)")

    enforced = [r for r in results if is_enforced(r, threshold)]
    for r in enforced:
        rem = f" | fix: {r['remediation']}" if r["remediation"] else ""
        print(f"::error::[{r['control']}][{r['severity']}] {r['title']} - {r['evidence']}{rem}")

    ssot = ", ".join(report["policy_ssot"])
    if enforced:
        print(f"dockerfile-standard: FAILED - {len(enforced)} enforced violation(s), "
              f"{len(advisory)} advisory, {report['passed']}/{report['evaluated']} evaluated "
              f"controls passing in {report['dockerfile']}.")
        print(f"Policy SSOT: {ssot}. (fail-on={report['fail_on']})")
    else:
        print(f"dockerfile-standard: OK - {report['passed']}/{report['evaluated']} controls "
              f"upheld ({report['skipped']} skipped) in {report['dockerfile']}"
              + (f"; {len(advisory)} advisory" if advisory else "") + ".")
        print(f"Policy SSOT: {ssot} (fail-on={report['fail_on']}).")


# --- generated docs (single source: the catalog) -----------------------------

def render_docs(doc: dict) -> str:
    domain = doc.get("domain", "dockerfile-standard")
    lines = [
        DOCS_BEGIN, "",
        f"_Generated from `controls/{domain}.yaml` by `scripts/check-dockerfile-standard.py "
        "--write-docs` — do not edit by hand._", "",
        "| Control | Policy | Severity | Capability scope | Owner | Status |",
        "| ------- | ------ | -------- | ----------------- | ----- | ------ |",
    ]
    for c in doc["controls"]:
        policy = " ".join(str(c["policy"]).split())
        caps = ", ".join(c.get("applies_to_capabilities") or ["all"])
        lines.append(f"| {c['id']} | {policy} | {c['severity']} | {caps} | {c['owner']} | {c['status']} |")
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
        print(f"::error::{path}: markers not found. Add these two lines where the table "
              f"should go:\n  {DOCS_BEGIN}\n  {DOCS_END}")
        return 1
    new = text.split(DOCS_BEGIN, 1)[0] + block + text.split(DOCS_END, 1)[1]
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"dockerfile-standard: wrote generated control table into {path}")
    else:
        print(f"dockerfile-standard: {path} control table already up to date")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    current = _extract_block(text)
    if current is None:
        print(f"::error::{path}: generated-controls markers not found; run --write-docs")
        return 1
    if current.strip() != render_docs(doc).strip():
        print(f"::error::{path}: control table is out of sync with controls catalog; run: "
              "python3 scripts/check-dockerfile-standard.py --write-docs " + str(path))
        return 1
    print(f"dockerfile-standard: {path} control table is in sync with the catalog")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Enterprise Dockerfile Standard guard (ADR-0072).")
    ap.add_argument("dockerfile", nargs="?", default=DEFAULT_TARGET, help="Dockerfile to check")
    ap.add_argument("--repo-root", default=".", help="repo root (for go.mod/cmd/* structural checks)")
    ap.add_argument("--capabilities", default="", help="comma-separated declared capabilities")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    ap.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    ap.add_argument("--report", help="write the JSON report to this path")
    ap.add_argument("--write-docs", metavar="FILE", help="regenerate the control table in FILE and exit")
    ap.add_argument("--verify-docs", metavar="FILE", help="fail if FILE's control table drifted; then exit")
    args = ap.parse_args(argv[1:])

    controls_path = Path(args.controls)
    if not controls_path.is_file():
        print(f"::error::dockerfile-standard: control catalog not found: {controls_path}")
        return 1
    doc = load_controls(controls_path)

    if args.write_docs:
        return write_docs(doc, Path(args.write_docs))
    if args.verify_docs:
        return verify_docs(doc, Path(args.verify_docs))
    if args.format == "markdown":
        print(render_docs(doc))
        return 0

    target = Path(args.dockerfile)
    if not target.is_file():
        print(f"::error::dockerfile-standard: Dockerfile not found: {target}")
        return 1

    capabilities = {c.strip() for c in args.capabilities.split(",") if c.strip()}
    unknown = capabilities - KNOWN_CAPABILITIES
    if unknown:
        print(f"::error::dockerfile-standard: unknown capability/ies {sorted(unknown)}; "
              f"must be a subset of {sorted(KNOWN_CAPABILITIES)}")
        return 1
    if not capabilities:
        print("::warning::dockerfile-standard: --capabilities is empty; only capability-"
              "agnostic controls will be meaningfully evaluated")

    df = Dockerfile(target, Path(args.repo_root))
    results = evaluate(df, doc["controls"], capabilities)
    threshold = SEVERITY_ORDER[args.fail_on]
    report = build_report(df, results, doc.get("policy_ssot", []), capabilities, args.fail_on, threshold)

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        render_text(report, threshold)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
