#!/usr/bin/env bash
# MarketScalper CI gate (roadmap P0.3; hardened per verified defect F3).
# The single quality gate: run before every merge/deploy. Exits on first failure.
#
# Usage: bash scripts/ci.sh   (from anywhere; script cd's to repo root)
#
# The pytest step hosts the mandatory gates:
#   - determinism harness (P0.26/P1.21, Architecture §10 — non-negotiable)
#   - FeedProvider conformance suite + import-boundary check (P0.19)
#   - DB schema / append-only tests (P0.8)
# Most of these require MARKETSCALPER_DB_DSN. Without it they would be
# silently SKIPPED while pytest still exits 0 (verified defect F3) — so
# this gate refuses to report success when the environment cannot run them.

set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${MARKETSCALPER_DB_DSN:-}" ]; then
    echo "[ci] FAIL: MARKETSCALPER_DB_DSN is not set." >&2
    echo "[ci]       The mandatory determinism/conformance/DB gates cannot run" >&2
    echo "[ci]       without a database — refusing to report a vacuous green." >&2
    exit 1
fi

echo "[ci] step 1: pytest suite (incl. determinism + conformance + boundary gates)"
python -m pytest

echo "[ci] all gates passed"
