#!/usr/bin/env bash
# =============================================================================
# Central schema-compatibility + canonical-outbox-conformance logic (SSOT).
#
# Single source of truth consumed by
#   .github/workflows/schema-compatibility.yml
# and therefore, via a thin caller, by every *-core-postgres repo on the
# platform. Change the gate HERE and roll it out by moving the @v1 tag — never
# by editing ~27 service repos (mirrors scripts/check-seed-contract.py).
#
# Contract with the workflow (all via environment):
#   TEST_DATABASE_URL     (required) DSN of an ephemeral, empty postgres.
#   DATABASE_URL          (optional) defaults to TEST_DATABASE_URL.
#   OUTBOX_TABLE          (optional) if set, the canonical outbox verifier runs
#                                    against this table; if empty, only the
#                                    migration apply/validate runs.
#   OUTBOX_VERIFY_VERSION (optional) platform-shared-go version providing
#                                    cmd/outbox-verify (the contract). Pinned
#                                    centrally so the contract is bumped
#                                    fleet-wide from ONE place, with zero
#                                    per-repo go.mod churn.
#   MIGRATE_CMD           (optional) explicit override of the migrate step for
#                                    a repo that does not fit the auto-ladder.
#
# The caller repo is checked out at $PWD (this script runs from its root).
#
# Migration ladder (auto-detected; first match wins). The platform's canonical,
# authoritative migration source is the goose chain applied through the shared
# dbmigrate gate (RFC-0027); the embedded schema.Migrate() baseline is a
# squashed snapshot some repos still apply at boot. We therefore PREFER the
# goose chain and fall back to the baseline:
#   1. MIGRATE_CMD override, if provided.
#   2. `go test ./schema -run TestGooseMigrationsRoundTrip` when that test
#      exists (adds down-migration coverage; auth-style).
#   3. schema.GooseUpDSN(ctx, dsn)  — the authoritative goose chain. Present in
#      every *-core-postgres repo. If it fails AND an embedded baseline exists,
#      fall back to (4) rather than failing the gate.
#   4. schema.Migrate(ctx, pool)    — embedded baseline (run twice = idempotency).
#   5. static SQL validation        — no executable migrator present.
#
# Whichever path runs must leave the DB migrated to HEAD; the outbox verifier
# then introspects the live table and fails closed on ANY semantic drift from
# the canonical contract (RFC-0032 / ADR-0069).
#
# Schema-path/layout guardrail (Schema Migration Standard §1/§12): every
# *-core-postgres repo's migration SQL MUST live at the exact path
# schema/migrations/ (sqlc query files under sql/queries/ are the one other
# allowed .sql location — not a migration). A repo that owns any DDL at all
# must ship AT LEAST the paired schema/migrations/000001_init.up.sql +
# 000001_init.down.sql baseline (every migration is a split .up.sql/.down.sql
# pair — no bare/combined .sql files). Additional forward migrations
# (000002_*, 000003_*, ...) are explicitly ALLOWED and expected to accumulate
# over time — this guardrail only fixes the PATH and the INIT PAIR, it never
# caps the migration count. A repo with zero .sql files anywhere is exempt
# (a legitimate no-DDL module, e.g. a storage adapter with no table DDL of its
# own). This check runs first and fails fast, before any DB is touched.
# =============================================================================
set -euo pipefail

: "${TEST_DATABASE_URL:?schema-compat: TEST_DATABASE_URL must be set}"
export DATABASE_URL="${DATABASE_URL:-${TEST_DATABASE_URL}}"
TABLE="${OUTBOX_TABLE:-}"
# v1.10.0: canonical outbox identity contract — event_id is a fresh
# per-occurrence uuidv7() (DB default) and idempotency_key is the
# producer-minted DETERMINISTIC idempotency key (NOT NULL, no default)
# (RFC-0032 / ADR-0071). v1.9.1 briefly named this column dedup_key; v1.10.0
# renamed it back to idempotency_key to align with the terminology already
# used across every other *-core-postgres outbox table on the platform.
# Bumped from the pre-split v1.7.0 (which forbade an event_id default and had
# no idempotency_key/dedup_key column at all).
VERIFY_VERSION="${OUTBOX_VERIFY_VERSION:-v1.10.0}"
MIGRATE_CMD_OVERRIDE="${MIGRATE_CMD:-}"

GENDIR=".schema-compat-gen"
trap 'rm -rf "${GENDIR}"' EXIT

log() { printf '>> %s\n' "$*"; }

# has_symbol <grep-pattern> — true if a Go symbol matching the pattern exists in
# the repo's schema package.
has_symbol() { grep -Rslq -- "$1" schema 2>/dev/null; }

# gen_and_run <import-lines> <body> — write a tiny main INSIDE the caller module
# (so the "<module>/schema" import resolves from its own go.mod) and run it.
gen_and_run() {
  local imports="$1" body="$2" module
  module="$(GOWORK=off go list -m)"
  rm -rf "${GENDIR}"
  mkdir -p "${GENDIR}"
  cat > "${GENDIR}/main.go" <<GO
package main

import (
	"context"
	"log"
	"os"
	"time"
${imports}
	schemapkg "${module}/schema"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()
	dsn := os.Getenv("TEST_DATABASE_URL")
	_ = dsn
${body}
}
GO
  GOWORK=off go run "./${GENDIR}"
}

run_gooseupdsn() {
  gen_and_run \
    "" \
    $'\tif err := schemapkg.GooseUpDSN(ctx, dsn); err != nil {\n\t\tlog.Fatal(err)\n\t}'
}

run_migrate() {
  gen_and_run \
    $'\t"github.com/jackc/pgx/v5/pgxpool"' \
    $'\tpool, err := pgxpool.New(ctx, dsn)\n\tif err != nil {\n\t\tlog.Fatal(err)\n\t}\n\tdefer pool.Close()\n\tif err := schemapkg.Migrate(ctx, pool); err != nil {\n\t\tlog.Fatal(err)\n\t}\n\tif err := schemapkg.Migrate(ctx, pool); err != nil {\n\t\tlog.Fatal(err)\n\t}'
}

static_lint() {
  local found=0 f
  while IFS= read -r -d '' f; do
    found=1
    [[ -s "${f}" ]] || { echo "::error file=${f}::migration SQL file is empty"; exit 1; }
  done < <(find . -type f -name '*.sql' \( -path '*/migrations/*' -o -path '*/schema/*' \) -print0)
  [[ "${found}" -eq 1 ]] || log "no migrations found; schema compatibility not required for this repo"
}

# check_schema_layout — fleet-wide path + naming consistency guardrail.
# Fails closed (before touching any DB) on:
#   1. any *.sql file living anywhere other than the allowed locations:
#        - schema/migrations/**  (the migration chain)
#        - sql/queries/**        (sqlc query source)
#        - schema/seed*.sql      (dev/role seed data — a distinct, sanctioned
#                                 concern from the migration chain, e.g.
#                                 seed_dev.sql / seed_roles.sql; NOT a
#                                 migration and never applies schema DDL)
#        - seed/**               (the platform seeding-standard tree)
#        - testdata/**           (idiomatic Go test fixtures, e.g. a
#                                 generated sqlc test snapshot)
#      Anything else — e.g. a stray flat schema/001_init.sql or schema/schema.sql
#      full-dump left over from before a squash, a service-local migrations/
#      copy, etc. — is a violation: it is either an actively-diverging second
#      SSOT or dead legacy cruft, and either way must not exist.
#   2. schema/migrations/ existing without the required baseline pair
#      000001_init.up.sql + 000001_init.down.sql.
#   3. any file under schema/migrations/ that isn't a well-formed
#      NNNNNN_name.up.sql / NNNNNN_name.down.sql, or an .up.sql with no
#      matching .down.sql (and vice versa).
# Additional forward migrations beyond 000001 are explicitly welcome — this
# never caps how many migrations a repo may accumulate.
check_schema_layout() {
  log "checking schema/migrations path + init-pair layout"

  local stray=() f
  while IFS= read -r -d '' f; do
    stray+=("${f}")
  done < <(find . -type f -name '*.sql' \
    -not -path './schema/migrations/*' \
    -not -path './sql/queries/*' \
    -not -path './schema/seed*.sql' \
    -not -path './seed/*' \
    -not -path './testdata/*' \
    -not -path '*/testdata/*' \
    -not -path './.schema-compat-gen/*' \
    -not -path './.git/*' \
    -print0)

  if [[ "${#stray[@]}" -gt 0 ]]; then
    echo "::error::stray .sql file(s) outside the canonical schema/migrations/ (migration chain) and sql/queries/ (sqlc queries) paths — every *-core-postgres repo's schema lives at EXACTLY schema/migrations/:"
    printf '  %s\n' "${stray[@]}"
    exit 1
  fi

  if [[ ! -d schema/migrations ]]; then
    log "no schema/migrations/ directory and no stray .sql found; treating as a legitimate no-DDL module (exempt)"
    return
  fi

  if [[ ! -f schema/migrations/000001_init.up.sql ]]; then
    echo "::error::schema/migrations/ exists but is missing the required baseline schema/migrations/000001_init.up.sql"
    exit 1
  fi
  if [[ ! -f schema/migrations/000001_init.down.sql ]]; then
    echo "::error::schema/migrations/ exists but is missing schema/migrations/000001_init.down.sql — every migration ships as a split .up.sql/.down.sql pair, never a lone .up.sql"
    exit 1
  fi

  local bad=0
  while IFS= read -r -d '' f; do
    local base name
    name="$(basename "${f}")"
    if [[ "${name}" == *.up.sql ]]; then
      base="${f%.up.sql}"
      if [[ ! -f "${base}.down.sql" ]]; then
        echo "::error file=${f}::.up.sql has no matching .down.sql"
        bad=1
      fi
    elif [[ "${name}" == *.down.sql ]]; then
      base="${f%.down.sql}"
      if [[ ! -f "${base}.up.sql" ]]; then
        echo "::error file=${f}::.down.sql has no matching .up.sql"
        bad=1
      fi
    else
      echo "::error file=${f}::does not match the NNNNNN_name.up.sql / NNNNNN_name.down.sql convention"
      bad=1
    fi
  done < <(find schema/migrations -maxdepth 1 -type f -name '*.sql' -print0)

  [[ "${bad}" -eq 0 ]] || exit 1
  log "schema/migrations layout OK"
}

apply_migrations() {
  if [[ -n "${MIGRATE_CMD_OVERRIDE}" ]]; then
    log "migrate via caller-provided MIGRATE_CMD override"
    bash -c "${MIGRATE_CMD_OVERRIDE}"
    return
  fi

  if has_symbol "func TestGooseMigrationsRoundTrip"; then
    log "migrate via goose round-trip test (up -> downTo -> up)"
    GOWORK=off go test ./schema -run TestGooseMigrationsRoundTrip -count=1
    return
  fi

  if has_symbol "func GooseUpDSN"; then
    log "migrate via schema.GooseUpDSN (authoritative goose chain)"
    if run_gooseupdsn; then
      return
    fi
    if has_symbol "func Migrate("; then
      log "GooseUpDSN failed; falling back to embedded schema.Migrate baseline"
      run_migrate
      return
    fi
    echo "::error::schema.GooseUpDSN failed and no embedded baseline to fall back to"
    return 1
  fi

  if has_symbol "func Migrate("; then
    log "migrate via embedded schema.Migrate baseline (x2 idempotency)"
    run_migrate
    return
  fi

  log "no executable migrator detected; running static SQL validation"
  static_lint
}

verify_outbox() {
  if [[ -z "${TABLE}" ]]; then
    log "OUTBOX_TABLE not set; skipping canonical outbox conformance"
    return
  fi
  log "verifying ${TABLE} against canonical outbox contract (verifier@${VERIFY_VERSION})"
  # Run from an empty dir so the caller's go.mod never influences the pinned
  # verifier version. The verifier depends only on outboxverify + pgx.
  local wd
  wd="$(mktemp -d)"
  (
    cd "${wd}"
    GOWORK=off go run \
      "github.com/coderaxis/platform-shared-go/cmd/outbox-verify@${VERIFY_VERSION}" \
      -table "${TABLE}" -dsn "${TEST_DATABASE_URL}"
  )
}

check_schema_layout
apply_migrations
verify_outbox
log "schema-compatibility OK"
