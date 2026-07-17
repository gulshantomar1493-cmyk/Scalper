#!/usr/bin/env bash
# MarketScalper CI gate — skeleton (roadmap P0.3).
# The single quality gate: run before every merge/deploy. Exits on first failure.
#
# Usage: bash scripts/ci.sh   (from anywhere; script cd's to repo root)

set -euo pipefail
cd "$(dirname "$0")/.."

echo "[ci] step 1: pytest suite"
python -m pytest

# TODO (P0.19): provider import-boundary check — fail the build if any
#               engine/strategy/planner/journal module imports a concrete
#               provider module; plus the FeedProvider conformance suite.

# TODO (P0.26, extended P1.21 / P2.23 / P3.20): determinism gate — double
#               replay over the same candle range must produce byte-identical
#               outputs (candles -> structure objects -> all objects ->
#               signals + recommendations). Non-negotiable, Architecture §10.

echo "[ci] all gates passed"
