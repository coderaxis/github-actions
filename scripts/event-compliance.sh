#!/usr/bin/env bash
# =============================================================================
# Central event-handling compliance gate (SSOT).
#
# Single source of truth consumed by
#   .github/workflows/event-handling-compliance.yml
# and therefore, via a thin caller, by every service repo on the platform.
# Change the gate HERE and roll it out by moving the @v1 tag — never by
# editing ~40 service repos (mirrors scripts/schema-compat.sh and
# scripts/check-seed-contract.py).
#
# Policy SSOT: services/ENTERPRISE_NOTIFICATION_PATTERN.md §7 (per-family
# ownership matrix + mechanical decision rule) and §8 (anti-patterns). This
# script is the EXECUTABLE form of that policy — read it for the full
# rationale. Summary of the mechanical rule this script enforces:
#
#   Has the service its own Postgres (a *-core-postgres dependency)?
#     Yes -> role P (or H, the notification hub): MUST publish domain events
#            EXCLUSIVELY via the transactional outbox
#            (github.com/coderaxis/platform-shared-go/outbox) + Debezium CDC.
#            Constructing a raw Kafka producer anywhere in this repo is a
#            hard violation — no exceptions — UNLESS role is the narrower
#            "Hybrid" carve-out (outbox for lifecycle, ONE named direct-Kafka
#            stream topic for a latency-critical frame stream, e.g. chat
#            messages / voice call media).
#     No  -> role DK (produces events; no Postgres) or E (true exception, no
#            events produced at all). A DK producer is allowed, but MUST use
#            the canonical envelope/topic registry in
#            github.com/coderaxis/platform-shared-go/messaging/kafka —
#            never an ad-hoc hardcoded topic string (anti-pattern #6/#9).
#   role Bridge (event-publisher-core only): the sanctioned CDC/polling
#            canonicalizer — exempt from the producer restriction entirely,
#            that IS its job.
#
# Contract with the workflow (all via environment):
#   ROLE            (required) one of: P | H | DK | Hybrid | E | Bridge
#                   (matches services/ENTERPRISE_NOTIFICATION_PATTERN.md §7's
#                   "Role" column for this repo's family. Declared per-repo by
#                   the thin caller — reviewable in that repo's own workflow
#                   YAML, exactly like schema-compatibility.yml's `table:`).
#   ALLOWED_TOPICS  (required when ROLE is DK or Hybrid; comma-separated) the
#                   ONLY Kafka topic string literals this repo's direct
#                   producer(s) may publish to, e.g. "inboxxhq.chat.messages".
#                   Must match a github.com/coderaxis/platform-shared-go/
#                   messaging/kafka topics.go constant VALUE.
#
# The caller repo is checked out at $PWD (this script runs from its root).
# =============================================================================
set -euo pipefail

: "${ROLE:?event-compliance: ROLE must be set (P|H|DK|Hybrid|E|Bridge)}"
ALLOWED_TOPICS_RAW="${ALLOWED_TOPICS:-}"

log() { printf '>> %s\n' "$*"; }
fail() { echo "::error::$*"; FAILED=1; }
FAILED=0

# find_go_files <pattern...> — grep -rn for pattern(s) across the repo's own
# Go source, excluding vendor, .git, generated dirs, and tests (tests may
# legitimately construct a fake/mock producer that isn't a real runtime
# concern).
grep_go() {
  grep -rn "$@" \
    --include='*.go' \
    --exclude='*_test.go' \
    --exclude-dir='.git' \
    --exclude-dir='vendor' \
    --exclude-dir='.schema-compat-gen' \
    . 2>/dev/null || true
}

PRODUCER_PATTERN='sarama\.NewSyncProducer\(|sarama\.NewAsyncProducer\('
CONSUMER_PATTERN='sarama\.NewConsumerGroup\('
ENTERPRISE_CONSUMER_PATTERN='events\.NewEnterpriseConsumer\('
CANONICAL_ENVELOPE_IMPORT='platform-shared-go/messaging/kafka'
OUTBOX_CALLSITE_PATTERN='\.(Insert|Create)[A-Za-z0-9_]*Outbox[A-Za-z0-9_]*\(|INSERT[[:space:]]+INTO[[:space:]]+[A-Za-z0-9_]*_outbox'
SHARED_OUTBOX_IMPORT='platform-shared-go/outbox'

# check_outbox_write_path — HARD gate, applies regardless of ROLE (any repo,
# core-postgres or service, could in principle hand-roll an outbox insert).
# If this repo's own Go source constructs/inserts an outbox row anywhere (a
# sqlc-generated Insert*Outbox*/Create*Outbox* call site, or a raw
# "INSERT INTO ..._outbox" string) it MUST also reference the canonical
# github.com/coderaxis/platform-shared-go/outbox package SOMEWHERE in this
# repo. A repo with NO local outbox-insert call site at all is exempt (it
# either doesn't own outbox writes yet, or writes exclusively through the
# shared publisher's own generic INSERT with no per-repo sqlc query — nothing
# to check either way). This is the enforcement for
# ENTERPRISE_NOTIFICATION_PATTERN.md §3 ("Canonical Go publisher") — the
# shared library centrally handles table-name validation, payload marshaling,
# correlation/causation-id capture, and the idempotent
# INSERT ... ON CONFLICT (event_id) DO NOTHING; a local reimplementation loses
# all of that silently. The doc's "thin per-family adapter" carve-out (post,
# chat, stripe-adapter, device) still must reference the shared package from
# within that adapter — it adapts to the same contract, it doesn't bypass it.
check_outbox_write_path() {
  log "checking outbox write-path compliance (must use github.com/coderaxis/platform-shared-go/outbox)"
  local hits
  hits="$(grep_go -iE "${OUTBOX_CALLSITE_PATTERN}" | { grep -v '/internal/db/\|/sqlc/' || true; })"
  if [[ -z "${hits}" ]]; then
    log "no local outbox-row-insert call site found — nothing to check (this repo may not own outbox writes, or writes exclusively via the shared publisher's generic INSERT with no per-repo sqlc query)"
    return
  fi
  if grep_go -F "${SHARED_OUTBOX_IMPORT}" | grep -q .; then
    log "outbox write-path references the canonical shared library — OK"
    return
  fi
  fail "this repo constructs/inserts an outbox row via a hand-rolled call site, but never references the canonical github.com/coderaxis/platform-shared-go/outbox package anywhere. Outbox writes must go through outbox.NewPublisher (directly, or via a thin per-family adapter that itself imports/wraps it) — never a full local reimplementation of provenance/marshaling/idempotency (ENTERPRISE_NOTIFICATION_PATTERN.md §3, §7 'Outbox publisher' row; anti-pattern #7). Found:"
  echo "${hits}" | sed 's/^/    /'
}

check_producer_compliance() {
  log "checking direct-Kafka producer compliance for role=${ROLE}"

  local hits
  hits="$(grep_go -E "${PRODUCER_PATTERN}")"

  case "${ROLE}" in
    P|H)
      if [[ -n "${hits}" ]]; then
        fail "role=${ROLE} services must publish domain events EXCLUSIVELY via the transactional outbox (github.com/coderaxis/platform-shared-go/outbox) + Debezium CDC — no direct Kafka producer is allowed (ENTERPRISE_NOTIFICATION_PATTERN.md §8 anti-pattern #1). Found direct producer construction:"
        echo "${hits}" | sed 's/^/    /'
      else
        log "role=${ROLE}: no direct producer found — OK"
      fi
      ;;
    DK|Hybrid)
      if [[ -z "${hits}" ]]; then
        log "role=${ROLE}: no direct producer found in this repo (fine if this repo doesn't own the producer side)"
        return
      fi
      log "role=${ROLE}: direct producer found (expected) — validating topic allowlist (hard) + canonical envelope usage (advisory)"

      # ADVISORY ONLY (never fails the build): adopting the shared envelope
      # struct/constants (platform-shared-go/messaging/kafka) instead of an
      # ad-hoc local envelope is real, but separate, hardening — a
      # value-preserving refactor with zero dual-produce/dual-consume risk
      # (ENTERPRISE_NOTIFICATION_PATTERN.md anti-pattern #4/#6/#9). The HARD
      # gate below (topic allowlist) is what actually catches an undocumented
      # or drifted topic — that's the real architectural risk this check
      # exists to prevent.
      if ! grep_go -F "${CANONICAL_ENVELOPE_IMPORT}" | grep -q .; then
        echo "::warning::role=${ROLE} direct-Kafka producer(s) found, but this repo never references the canonical github.com/coderaxis/platform-shared-go/messaging/kafka envelope/topics package (ENTERPRISE_NOTIFICATION_PATTERN.md anti-pattern #4/#6/#9 — hardcoding topic/envelope shape instead of the shared registry). ADVISORY ONLY — not build-blocking today. Recommend migrating:"
        echo "${hits}" | sed 's/^/    /'
      fi

      if [[ -z "${ALLOWED_TOPICS_RAW}" ]]; then
        fail "role=${ROLE} requires ALLOWED_TOPICS to be set (the exact topic string(s) this repo's direct producer(s) may publish to, per ENTERPRISE_NOTIFICATION_PATTERN.md §7)."
        return
      fi

      # Every hardcoded "inboxxhq.<...>" topic-looking string literal found
      # anywhere in Go source must be in the declared allowlist. This is a
      # deliberately simple lexical check (not a full data-flow trace of what
      # string ends up in a sarama.ProducerMessage.Topic) — it catches the
      # real-world failure mode of an ad-hoc/undocumented topic string, at the
      # cost of also matching topic literals used only in comments/consumer
      # subscriptions. False positives are rare and reviewable; false
      # negatives (a topic string built dynamically/concatenated) are a
      # known limitation — flag such code for manual review separately.
      # inboxxhq.events (the canonical Debezium-CDC outbox sink, OutboxTopic in
      # messaging/kafka/topics.go) is ALWAYS implicitly allowed here regardless
      # of role/ALLOWED_TOPICS — a DK/Hybrid repo consuming from the canonical
      # bus (e.g. chat/notification subscribing to cross-service domain
      # events) is correct, sanctioned usage, not a direct-Kafka violation.
      local found_topics off_topics topic
      found_topics="$(grep_go -oE '"inboxxhq\.[a-z0-9_.]+"' | sed -E 's/^[^:]+:[0-9]+:"//; s/"$//' | sort -u)"
      IFS=',' read -ra allowed <<< "${ALLOWED_TOPICS_RAW},inboxxhq.events"
      off_topics=""
      while IFS= read -r topic; do
        [[ -z "${topic}" ]] && continue
        local ok=0 a
        for a in "${allowed[@]}"; do
          [[ "${topic}" == "$(echo "${a}" | xargs)" ]] && ok=1 && break
        done
        [[ "${ok}" -eq 1 ]] || off_topics+="${topic}"$'\n'
      done <<< "${found_topics}"

      if [[ -n "${off_topics}" ]]; then
        fail "role=${ROLE} found Kafka topic string literal(s) NOT in the declared ALLOWED_TOPICS (${ALLOWED_TOPICS_RAW}) — every direct-Kafka topic this repo touches must be a registered github.com/coderaxis/platform-shared-go/messaging/kafka constant and declared in this repo's thin-caller workflow input:"
        echo "${off_topics}" | sed '/^$/d;s/^/    /'
      fi
      ;;
    E|Bridge)
      log "role=${ROLE}: no producer restriction enforced by this gate (true exception / sanctioned canonicalizer)"
      if [[ -n "${hits}" ]]; then
        log "note: direct producer construction found anyway — not failing, but re-verify this repo's role classification is still accurate:"
        echo "${hits}" | sed 's/^/    /'
      fi
      ;;
    *)
      fail "unknown ROLE=${ROLE} (expected one of P|H|DK|Hybrid|E|Bridge)"
      ;;
  esac
}

# check_consumer_style — ADVISORY ONLY (never fails the build). Reports raw
# sarama.NewConsumerGroup construction that isn't wrapped by the shared
# events.EnterpriseConsumer (retry/backoff/DLQ/correlation-id/tracing/health —
# see platform-shared-go/events/consumer.go). This is a fleet-wide, near-
# universal gap today; it is intentionally non-blocking until a follow-up
# decision is made to enforce it, so it never fails CI on its own.
check_consumer_style() {
  local hits wrapped
  hits="$(grep_go -E "${CONSUMER_PATTERN}")"
  [[ -z "${hits}" ]] && return
  wrapped="$(grep_go -F "${ENTERPRISE_CONSUMER_PATTERN}")"
  if [[ -z "${wrapped}" ]]; then
    echo "::warning::this repo constructs a raw sarama.NewConsumerGroup consumer instead of the shared events.EnterpriseConsumer (github.com/coderaxis/platform-shared-go/events — retry/backoff, DLQ routing, correlation-id propagation, tracing, liveness health). ADVISORY ONLY — not build-blocking today. Recommend migrating:"
    echo "${hits}" | sed 's/^/    /'
  fi
}

check_producer_compliance
check_outbox_write_path
check_consumer_style

if [[ "${FAILED}" -ne 0 ]]; then
  echo "::error::event-handling compliance FAILED — see errors above."
  exit 1
fi
log "event-handling compliance OK"
