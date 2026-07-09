#!/usr/bin/env python3
"""Seed-contract guard (CI). Run from a service repo root.

Language-agnostic enforcement of the enterprise seeding standard
(docs/core-docs/standards/seeding/README.md). Fails (exit 1) when a stateful
service violates the contract. Stateless services (no cmd/seed and no seed data
tree) are skipped with exit 0 so this can run fleet-wide.

Governance model (federated ownership, central governance — ADR-0059):
  The service OWNS its seed layout, code, data, and tests. The platform gate
  enforces OUTCOMES/INVARIANTS, not a specific file layout. So this checker fails
  only on things that are unsafe or non-deployable, and merely *nudges* toward the
  recommended canonical tree.

Service classes (auto-detected):
  * stateless          — no cmd/seed and no seed tree            -> skip (exit 0)
  * file-based seeder   — ships a <...>/seed/data tree           -> invariants
  * delegated seeder    — cmd/seed pulls data from an external
                          *-core-postgres/seed module (SSOT in
                          that repo; enforced there)             -> marker only
  * code-only seeder    — cmd/seed seeds programmatically
                          (SQL/generated), ships no JSON tree     -> marker only

Enforced invariants (file-based seeders — HARD failures):
  1. Dockerfile carries the marker  # seed binary path: /app/<binary>
     (the seed binary must be built + shipped for the Argo PreSync hook)
  2. Dockerfile copies the seed data tree into the runtime image
  3. NO REAL DATA in qualified environments — every seed file that targets
     staging / preprod / prod is a placeholder (a JSON array whose every object
     has only the key "comment"). This is checked for BOTH layouts:
        canonical:  <data>/staging|preprod|prod/*.json
        flat:       <data>/*.staging.json  *.preprod.json  *.prod.json
  4. deploy-reusable.yml (if present) has no  SEED_COMMAND=""  override

Recommended (SOFT — informational ::notice::, never fails):
  * canonical subdir layout  system/ dev/ staging/ preprod/ prod/
    Services on the flat *.common.json (SSOT) + *.local.json (fixtures) layout
    are compliant; convergence to canonical is encouraged, not mandated.

Marker-only contract (delegated / code-only seeders): the seed binary must be
built and shipped, so the Dockerfile marker (invariant 1) is still required —
the file/data checks do not apply because the data SSOT is not in this repo.

This is the single-repo enforcement twin of the seeding standard's §6b.

SSOT: this file lives in coderaxis/github-actions and is invoked by the central
reusable workflow .github/workflows/seed-contract-check.yml. Service repos carry
only a thin caller; they do NOT vendor this script.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CANONICAL_SUBDIRS = ("system", "dev", "staging", "preprod", "prod")
QUALIFIED_ENVS = ("staging", "preprod", "prod")
SEED_MARKER = re.compile(r"#\s*seed binary path:\s*/app/\S+")


def find_seed_data_dir(root: Path) -> Path | None:
    """Locate the canonical <...>/seed/data directory under internal/."""
    internal = root / "internal"
    search_root = internal if internal.is_dir() else root
    for dirpath, dirnames, _ in os.walk(search_root):
        p = Path(dirpath)
        if p.name == "data" and p.parent.name == "seed":
            return p
        # prune vcs/vendor noise
        dirnames[:] = [d for d in dirnames if d not in {".git", "vendor", "node_modules"}]
    return None


def is_stateful(root: Path, data_dir: Path | None) -> bool:
    return (root / "cmd" / "seed").is_dir() or data_dir is not None


def cmd_seed_delegates(root: Path) -> bool:
    """True if cmd/seed imports an external *-core-postgres/seed package.

    Those services keep their seed data SSOT in the shared core-postgres module
    (enforced by the seed-contract check running in *that* repo), so this repo
    legitimately has no local seed/data tree.
    """
    seed_dir = root / "cmd" / "seed"
    if not seed_dir.is_dir():
        return False
    for gf in seed_dir.glob("*.go"):
        txt = gf.read_text(encoding="utf-8", errors="replace")
        if re.search(r'"[^"]*-core-postgres/seed"', txt):
            return True
    return False


def check_marker_only(root: Path, errors: list[str]) -> None:
    """For delegated / code-only seeders: only require the built+shipped binary."""
    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        errors.append("Dockerfile: missing (a stateful service must ship one)")
        return
    text = dockerfile.read_text(encoding="utf-8", errors="replace")
    if not SEED_MARKER.search(text):
        errors.append(
            "Dockerfile: missing marker '# seed binary path: /app/<binary>' "
            "(the seed binary must be built and shipped to run as an Argo PreSync hook)"
        )


def check_dockerfile(root: Path, data_dir: Path | None, errors: list[str]) -> None:
    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        errors.append("Dockerfile: missing (a stateful service must ship one)")
        return
    text = dockerfile.read_text(encoding="utf-8", errors="replace")
    if not SEED_MARKER.search(text):
        errors.append(
            "Dockerfile: missing marker '# seed binary path: /app/<binary>'"
        )
    if data_dir is not None:
        # The Dockerfile must COPY the seed data tree. Accept any COPY line that
        # references the seed data path segment.
        if "seed/data" not in text and "seed\\data" not in text:
            errors.append(
                "Dockerfile: does not copy the seed data tree "
                "(no reference to 'seed/data')"
            )


def is_canonical_layout(data_dir: Path) -> bool:
    """True if the service uses the recommended system/dev/staging/preprod/prod tree."""
    return all((data_dir / sub).is_dir() for sub in CANONICAL_SUBDIRS)


def qualified_env_files(data_dir: Path) -> list[Path]:
    """Every seed JSON that targets a qualified env, in BOTH layouts.

    canonical:  <data>/staging|preprod|prod/*.json
    flat:       <data>/*.staging.json  *.preprod.json  *.prod.json
    """
    files: list[Path] = []
    for env in QUALIFIED_ENVS:
        d = data_dir / env
        if d.is_dir():
            files.extend(sorted(d.glob("*.json")))
        files.extend(sorted(data_dir.glob(f"*.{env}.json")))
    return files


def check_placeholders(data_dir: Path | None, errors: list[str]) -> None:
    """INVARIANT: no real data may target staging/preprod/prod (both layouts)."""
    if data_dir is None:
        return
    for jf in qualified_env_files(data_dir):
        try:
            arr = json.loads(jf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{jf.relative_to(data_dir)}: invalid JSON (expected placeholder array): {exc}")
            continue
        if not isinstance(arr, list):
            errors.append(f"{jf.relative_to(data_dir)}: placeholder must be a JSON array")
            continue
        for i, obj in enumerate(arr):
            if not isinstance(obj, dict):
                errors.append(f"{jf.relative_to(data_dir)}[{i}]: placeholder entries must be objects")
                continue
            extra = [k for k in obj if k != "comment"]
            if extra:
                errors.append(
                    f"{jf.relative_to(data_dir)}[{i}]: real data field(s) {extra} in a "
                    "qualified-env seed file; staging/preprod/prod must be placeholder-only "
                    "(objects with just 'comment'). Move reference data to system/ or "
                    "*.common.json (loaded in every env)."
                )


def check_no_seed_command_override(root: Path, errors: list[str]) -> None:
    wf = root / ".github" / "workflows" / "deploy-reusable.yml"
    if not wf.is_file():
        return
    text = wf.read_text(encoding="utf-8", errors="replace")
    empty_assign = re.search(r'SEED_COMMAND\s*=\s*(""|\'\')', text)
    if not empty_assign:
        return
    # An empty assignment is the legitimate else-branch fallback ONLY when the
    # workflow actually computes SEED_BINARY. A hardcoded empty with no binary
    # logic is a real "seeding disabled" override and is not permitted.
    if "SEED_BINARY" in text:
        return
    errors.append(
        ".github/workflows/deploy-reusable.yml: hardcodes SEED_COMMAND=\"\" "
        "with no SEED_BINARY logic (seeding disabled); this is not permitted"
    )


def main() -> int:
    root = Path(os.getcwd())
    data_dir = find_seed_data_dir(root)

    if not is_stateful(root, data_dir):
        print("seed-contract: stateless service (no cmd/seed, no seed tree); skipping.")
        return 0

    errors: list[str] = []

    if data_dir is None:
        # No local file-based seed data tree, but cmd/seed exists. Either the data
        # SSOT lives in an external core-postgres module (delegated) or the seeder
        # is programmatic (code-only). Only the built+shipped binary is enforced
        # here; the file-tree contract does not apply.
        kind = (
            "delegated (external *-core-postgres/seed module owns the data SSOT)"
            if cmd_seed_delegates(root)
            else "code-only (programmatic seeder; no file-based seed data)"
        )
        check_marker_only(root, errors)
        if errors:
            print("::group::seed-contract violations")
            for e in errors:
                print(f"::error::{e}")
            print("::endgroup::")
            print(f"seed-contract: FAILED with {len(errors)} violation(s). [{kind}]")
            return 1
        print(f"seed-contract: OK ({kind}; enforced seed-binary marker only).")
        return 0

    # File-based seeder: enforce invariants (marker + data copy + no real data in
    # qualified envs + no SEED_COMMAND override). Layout is the service's own
    # choice — canonical is recommended (soft notice), not required.
    check_dockerfile(root, data_dir, errors)
    check_placeholders(data_dir, errors)
    check_no_seed_command_override(root, errors)

    if errors:
        print("::group::seed-contract violations")
        for e in errors:
            print(f"::error::{e}")
        print("::endgroup::")
        print(f"seed-contract: FAILED with {len(errors)} violation(s).")
        return 1

    rel = data_dir.relative_to(root)
    if is_canonical_layout(data_dir):
        print(f"seed-contract: OK (file-based, canonical layout; data tree at {rel}).")
    else:
        missing = [s for s in CANONICAL_SUBDIRS if not (data_dir / s).is_dir()]
        print(
            f"::notice::seed-contract: {rel} uses a flat/service-owned layout; "
            f"canonical subdirs {missing} are recommended (not required)."
        )
        print(f"seed-contract: OK (file-based, flat/service-owned layout; data tree at {rel}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
