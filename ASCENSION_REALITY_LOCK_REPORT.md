# ASCENSION REALITY LOCK — Complete Report

## Codeword: ASCENSION REALITY LOCK

## FINAL VERDICT: TEMPORAL_LEAKAGE_CONFIRMED_RESULT_INVALIDATED

---

## 1. LEGACY RESULT PRESERVED

Season 1 artifacts were NOT overwritten. The FROZEN marker is intact.
All 13 Season 1 files remain in `artifacts/ascension/seasons/season_1/`.
The legacy result is renamed: **LEGACY_SEASON_1_RAW_WICK_RESULT**.

## 2. TEMPORAL FEATURE VERDICT

**TEMPORAL_LEAKAGE_CONFIRMED**

All 5 AGENT_6 features and the label come from the same DexScreener API snapshot. The label (`raw_max_multiple_24h`) is algebraically derived from `price_change_24h` — a trailing 24h aggregate from the same snapshot that produced the features. 100% match rate (2,416/2,416). No temporal separation exists between feature observation and outcome determination.

## 3. EVENT-LEVEL EVIDENCE

| Metric | Value |
|--------|-------|
| Features audited | 5 |
| Temporal leakage confirmed | 2 (txns_buys_24h, volume_24h) |
| Feature definition invalid | 1 (appreciation_from_first) |
| Unverifiable | 2 (organic_volume_probability, independent_buyer_estimate) |
| Raw on-chain events available | 0 |
| Data source | DexScreener API aggregates (single 88-minute snapshot) |
| Tokens with actual first_trade_ts | 0 |

## 4. CLEAN DECISION SNAPSHOTS

**Status**: FRAMEWORK_READY_AWAITING_RAW_EVENTS

12 snapshot ages defined: T+30s, T+1m, T+2m, T+5m, T+10m, T+15m, T+30m, T+1h, T+3h, T+6h, T+12h, T+24h

Temporal rule: `latest_contributing_event_time <= decision_time`

**Blocked by**: No raw on-chain event data exists. Cannot construct point-in-time features.

## 5. EXECUTABLE LABEL RESULTS

**Status**: NOT_AVAILABLE

Legacy `raw_max_multiple_24h` preserved but NOT used as primary target.

| Label Type | Status |
|-----------|--------|
| legacy_raw_max_multiple_24h | PRESERVED (DexScreener trailing price_change) |
| net_executable_2x_after_decision | NOT_COMPUTABLE (no raw events) |
| net_executable_5x+ | NOT_COMPUTABLE |
| Executable milestones (all) | NOT_COMPUTABLE |

Position sizes defined: $50, $100, $250, $500, $1K, $2.5K, $5K, $10K
Horizons defined: 24h, 3d, 7d, 14d, 30d, 60d, 90d, 180d

## 6. CENSORING RECONCILIATION

**The 545 vs 183 discrepancy explained:**

- **545** = tokens in `c_raw_universe` without `price_change_24h` data (no label)
- **183** = tokens classified as `RIGHT_CENSORED` in universe `data_completeness`
- **Difference (362)** = tokens classified as SCAM_OR_RUG, PRICE_SOURCE_FAILED, NEVER_ECONOMICALLY_EXECUTABLE, or UNKNOWN_OUTCOME — they lack 24h data for reasons other than censoring

**Critical note**: Since all data comes from a single API snapshot, "right-censored" means the API returned no price data — NOT that the observation window is still open.

## 7. PLATFORM-CONDITIONED RESULTS

**Classification**: PLATFORM_AND_TEMPORAL_ARTIFACT

| Metric | Value |
|--------|-------|
| PumpSwap candidates | See platform table |
| PumpSwap 2x rate (raw wick) | ~14-20% depending on which pump* platforms included |
| AGENT_6 PumpSwap selections | 111/115 (96.5%) |

AGENT_6's performance cannot be classified as alpha because:
1. Features and labels come from the same snapshot — no temporal separation
2. 96.5% of selections were PumpSwap tokens
3. The "precision" reflects contemporaneous classification, not prediction

Until clean prospective data with temporal separation is available, the signal cannot be classified as COIN_ALPHA, PLATFORM_DETECTOR, or PUMPSWAP_CONDITIONAL_SIGNAL.

## 8. EXECUTABLE-ONLY ALPHA RESULTS

**NOT_ESTABLISHED**

No executable labels exist. The correct alpha denominator cannot be computed because:
1. Gate A (executability) cannot be assessed without raw on-chain events
2. Gate B (alpha) requires Gate A to define the eligible population
3. Lift calculations require executable-only denominators

## 9. MULTI-DAY UNIVERSE

| Property | Value |
|----------|-------|
| Universe name | UNIVERSE_90MIN_V1 |
| Calendar days | 1 (partial) |
| Collection window | 88 minutes |
| Total tokens | 2,961 |
| Multi-regime | NO |
| Multi-day | NO |
| Expansion status | AWAITING_PROSPECTIVE_COLLECTION |

The existing dataset MUST NOT be described as a multi-regime historical universe.

## 10. INDEPENDENT SEASONS

**Verdict**: INSUFFICIENT_INDEPENDENT_HISTORICAL_SEASONS

All 2,961 tokens were collected in a single 88-minute window. Previous "seasons" were time-slice partitions of this same window — NOT independent market regimes.

Requirements for independence:
- Token-disjoint sets from different calendar days
- Separate collection runs
- Different market conditions
- Outcomes frozen before access

**Season overlap matrix**: Not applicable — all tokens from same collection window.

## 11. CLEAN AGENT RESULTS

### AGENT_6_LEGACY_V1 (formerly AGENT_6_LOGISTIC_REACHED_2X)

| Property | Value |
|----------|-------|
| Status | RESEARCH_CANDIDATE |
| Temporal verdict | TEMPORAL_LEAKAGE_CONFIRMED |
| Season 1 precision | 53.04% (61/115) — NOT predictive |
| Season 1 lift | 2.36× — NOT predictive |
| Label | LEGACY_SEASON_1_RAW_WICK_RESULT |

### AGENT_6_CLEAN_V2

| Property | Value |
|----------|-------|
| Status | BLOCKED_AWAITING_RAW_EVENT_DATA |
| Requirements | Corrected snapshots, executable labels, eligible alpha population |
| Pre-registered ablations | 13 defined |

## 12. ABLATIONS

**Status**: BLOCKED_AWAITING_CLEAN_DATA

13 pre-registered ablations defined:
- without_txns_buys, without_volume, without_organic_volume_probability
- without_appreciation_from_first, without_independent_buyer_estimate
- buyer_features_only, volume_features_only, path_features_only
- top_one_feature, top_two_features, top_three_features
- venue_plus_time_only, executability_variables_only

Cannot execute until clean temporally valid data exists.

## 13. PORTFOLIO RESULTS

| Metric | Value |
|--------|-------|
| Total selections | 115 |
| Selections accounted | 115 |
| Unaccounted | 0 |
| Reason code | DATA_MISSING (all 115) |
| Gross return | UNAVAILABLE (temporal leakage) |
| Net simplified return | UNAVAILABLE (temporal leakage) |
| Net executable return | UNAVAILABLE (no raw events) |

Prior simulation note: Previous simulation had 89/115 trades — 26 unaccounted. Prior max drawdown of 0.93 (if 93%) would fail promotion. All prior simulation results are invalid due to temporal leakage.

## 14. MILESTONE PREDICTION RESULTS

**Status**: BLOCKED_AWAITING_RAW_EVENT_DATA

Previous cascade reported historical transition frequencies. These are NOT model performance. A predictive milestone system requires:
- Point-in-time decision snapshot at each milestone crossing
- Features using ONLY information available at that checkpoint
- Prediction frozen before viewing later outcomes
- Target measured AFTER checkpoint

All stages (AT_2X through AT_100X) are HYPOTHESIS_ONLY until raw events exist.

## 15. PROSPECTIVE SHADOW STATUS

| Property | Value |
|----------|-------|
| Pipeline state | COLLECTION_ONLY |
| Collection active | Yes (framework ready) |
| Prediction commitment | Not active |
| Shadow season | Not active |

**Blocked by**: No raw event collection pipeline, no temporal reconstruction, no clean snapshots.

## 16. MULTIPLE-TESTING STATUS

| Metric | Value |
|--------|-------|
| Model fits | 10 |
| Feature combinations | 1 |
| Total effective tests | 10 |
| Bonferroni factor | 10 |
| PBO | NOT_COMPUTABLE |
| Backtest selection risk | UNRESOLVED |

PBO requires independent season structure with CSCV. The current dataset has only one 88-minute collection window. No independent seasons exist.

## 17. TEST RESULTS

| Category | Count |
|----------|-------|
| PASSED | 224 |
| FAILED | 0 |
| SKIPPED | 0 |
| EXPECTED_FAILURES | 0 |

Breakdown: 183 existing (65 Arena + 65 Gauntlet + 53 Oracle) + 41 Reality Lock = 224 total

## 18. FAILED AND UNEVALUATED GATES

| Gate | Status | Reason |
|------|--------|--------|
| Temporal integrity | **FAILED** | Features and labels co-temporal |
| Executable labels | UNEVALUATED | No raw on-chain events |
| Platform generalization | UNEVALUATED | Confounded by PumpSwap concentration |
| Multi-day generalization | UNEVALUATED | Single 88-minute snapshot |
| Independent seasons | UNEVALUATED | Insufficient data |
| Clean model rebuild | BLOCKED | Awaiting raw events |
| Feature ablations | BLOCKED | Awaiting clean data |
| Calibration | **FAILED** + BLOCKED | ECE=0.146 + temporal leakage |
| Portfolio edge | UNEVALUATED | No executable returns |
| Milestone prediction | BLOCKED | No event-level data |
| Prospective shadow | COLLECTION_ONLY | No event pipeline |

## 19. BINANCE SAFETY

```
REAL_BINANCE_ORDERS_SUBMITTED = 0
LIVE_TRADING_ENABLED = FALSE
PRODUCTION_API_CREDENTIALS_USED = FALSE
WITHDRAWAL_CAPABILITY = FALSE
MODEL_DIRECTED_LIVE_TRADING = LOCKED
PRODUCTION_TRADING = LOCKED
WITHDRAWALS = DISABLED
```

## 20. FINAL VERDICT

### TEMPORAL_LEAKAGE_CONFIRMED_RESULT_INVALIDATED

The legacy Season 1 result is invalid as predictive evidence because features and labels come from the same DexScreener API snapshot with no temporal separation. The model learned to classify already-completed outcomes, not predict future ones. All preceding gates (executable signal, platform generalization, multi-day, independent seasons) cannot pass because the foundational temporal requirement is not met.

**The Arena architecture is preserved.** The ASCENSION framework is sound — it correctly identified this result through the Gauntlet's temporal audit framework. The framework needs new data, not a new architecture.

**What must happen:**
1. Collect raw on-chain events via Solana RPC or Helius
2. Build point-in-time features from raw events
3. Create executable labels with temporal separation
4. Rebuild on genuinely independent calendar-day data
5. Prospective shadow evaluation before any claims

---

## Artifacts Created

| Artifact | Path |
|----------|------|
| Reality Lock module | `src/ascension/reality_lock.py` |
| Test suite | `tests/test_reality_lock.py` (41 tests) |
| Governance state | `artifacts/ascension/reality_lock/governance_state.json` |
| Legacy manifest | `artifacts/ascension/reality_lock/legacy_result_manifest.json` |
| Feature verdicts | `artifacts/ascension/reality_lock/feature_temporal_verdicts.json` |
| Temporal violations | `artifacts/ascension/reality_lock/temporal_violations.json` |
| Censoring reconciliation | `artifacts/ascension/reality_lock/censoring_reconciliation.json` |
| Platform controls | `artifacts/ascension/reality_lock/platform_conditioned_results.json` |
| Season overlap | `artifacts/ascension/reality_lock/season_overlap_matrix.json` |
| Universe coverage | `artifacts/ascension/reality_lock/universe_coverage.json` |
| Agent 6 ablations | `artifacts/ascension/reality_lock/agent_6_ablations.json` |
| Milestone predictions | `artifacts/ascension/reality_lock/milestone_prediction_results.json` |
| Experiment count | `artifacts/ascension/reality_lock/experiment_count.json` |
| Prospective shadow | `artifacts/ascension/reality_lock/prospective_shadow_status.json` |
| Master results | `artifacts/ascension/reality_lock/reality_lock_master_results.json` |
| File hashes | `artifacts/ascension/reality_lock/file_hashes.json` |
| Temporal audit doc | `docs/ASCENSION_TEMPORAL_FORENSIC_AUDIT.md` |
| This report | `docs/ASCENSION_REALITY_LOCK_REPORT.md` |

---

**Safety**: 0 real orders, 0 credentials, trading DISABLED
**Tests**: 224 passed (183 existing + 41 new), 0 failed
**Season 1**: PRESERVED (FROZEN marker intact)
**Generated**: 2026-08-29
