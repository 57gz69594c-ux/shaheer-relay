# ASCENSION GAUNTLET — Final Report

**Codeword**: ASCENSION GAUNTLET  
**Date**: 2026-08-29  
**Status**: GAUNTLET COMPLETE  

---

## Verdict

**PROVISIONAL_SIGNAL_CONFIRMED**

AGENT_6_LOGISTIC demonstrates a statistically significant signal in Season 1 blind evaluation (p < 0.001, Bonferroni-adjusted). The result reproduces exactly from frozen ledgers. However, AGENT_6 fails calibration, shows inconsistent multi-season performance, and has not demonstrated net-positive portfolio returns across cost models. It remains **SEASON_1_LEADER** — not promoted.

---

## 1. Season 1 Reproduction

| Metric | Expected | Reproduced | Match |
|--------|----------|------------|-------|
| Blind set size | 740 | 740 | ✓ |
| Base rate (2×) | 166/740 = 22.43% | 166/740 = 22.43% | ✓ |
| AGENT_6 selected | 115 | 115 | ✓ |
| AGENT_6 winners | 61 | 61 | ✓ |
| AGENT_6 precision | 53.04% | 53.04% | ✓ |
| AGENT_6 lift | 2.36× | 2.3646× | ✓ |
| AGENT_11 | 36/85 | 36/85 | ✓ |
| AGENT_3 | 79/215 | 79/215 | ✓ |
| AGENT_14 | 18/73 | 18/73 | ✓ |

**Verdict**: SEASON_1_RESULT_REPRODUCED

All numbers match exactly. Decision ledger hashes verified. No discrepancies found.

---

## 2. 2× Label Reconciliation

### The Discrepancy

| Population | 2× Rate |
|------------|---------|
| Earlier Engine C checkpoint (reported) | ~4.12% |
| Full universe (raw_max_multiple ≥ 2) | 13.27% (393/2961) |
| Executable tokens only | 11.44% (183/1600) |
| Return-tier-based (GAIN_2X+) | 8.23% (145/1761) |
| Season 1 blind set | **22.43% (166/740)** |
| Season 1 training set | 9.39% (139/1480) |

### Explanation

The 22.43% blind-set rate vs ~4.12% earlier checkpoint is explained by:

1. **Different denominator**: Earlier checkpoint likely used return_tier_24h on all tokens (including non-executable, censored). Season 1 blind uses `raw_max_multiple_24h ≥ 2` on all 740 blind tokens.
2. **Chronological drift**: Blind set covers later-discovered tokens (positions 1924–2664). Later periods had higher 2× rates, likely due to market conditions (bull regime).
3. **Raw chart wick inclusion**: `raw_max_multiple_24h` includes intraday peaks that may not be executable. The return_tier definition is stricter.
4. **Training rate is lower** (9.39%), confirming the chronological drift — the blind period was a better market.

### Season 1 Target Definition

- **Decision timestamp**: first_executable_time (first_trade_ts or discovered_at)
- **Starting price**: first_executable_price (RAW, not spread-adjusted)
- **Outcome horizon**: 24 hours
- **Multiple type**: GROSS raw chart peak (includes wicks)
- **Costs**: NONE — no fees, spread, slippage modeled
- **Rug handling**: Included (385 SCAM_OR_RUG tokens in universe)
- **Censoring**: 545 RIGHT_CENSORED tokens → treated as not reached

**Legacy label preserved as versioned original. NOT replaced.**

---

## 3. Universe Accounting

All 2,961 tokens accounted for:

| Category | Count |
|----------|-------|
| Training era | 1,480 |
| Validation era | 444 |
| Blind era (Season 1) | 740 |
| Championship era | 297 |
| **TOTAL** | **2,961** |

### Data Completeness

| Status | Count |
|--------|-------|
| DATA_COMPLETE | 892 |
| NEVER_ECONOMICALLY_EXECUTABLE | 1,325 |
| SCAM_OR_RUG | 385 |
| RIGHT_CENSORED | 183 |
| UNKNOWN_OUTCOME | 176 |
| **TOTAL** | **2,961** |

---

## 4. Survivorship-Bias Audit

**POTENTIAL_BIAS_DETECTED** — honest reporting required.

| Test | Universe Rate | Blind Rate | Disproportionate? |
|------|-------------|-----------|-------------------|
| Scam/rug tokens | 13.0% | 20.3% | **YES** — overrepresented |
| Non-executable tokens | 44.8% | 32.6% | **YES** — underrepresented |
| Right-censored | 6.2% | 2.7% | No |
| Low liquidity | 42.9% | 36.5% | No |

The blind set has more rugs and fewer non-executable tokens than the universe average. This is expected from chronological assignment (later tokens more likely to be executable), but it means the blind set is NOT a random sample. All tokens remain included — no tokens were removed — but outcome-availability differs by era.

**NO_SURVIVORSHIP_BIAS cannot be claimed.** The universe is deterministic, but population composition varies by era.

---

## 5. Leakage Audit

All 8 adversarial tests **PASSED**:

| Test | Verdict |
|------|---------|
| Future price access | PASS |
| Current FDV in features | PASS |
| Token identity leakage | PASS |
| Famous token text | PASS |
| Outcome file access | PASS |
| Row order encoding | PASS |
| Anonymous ID parity | PASS |
| Age reconstruction | PASS (not primarily reconstructing age) |

AGENT_6 features: `txns_buys_24h`, `volume_24h`, `organic_volume_probability`, `appreciation_from_first`, `independent_buyer_estimate`

No forbidden information detected. Features are PIT-safe transaction metrics.

---

## 6. Random-Control Distribution

**10,000 trials per agent.** AGENT_6 results:

| Metric | Value |
|--------|-------|
| Actual precision | 53.04% |
| Median random precision | 22.61% |
| 99th percentile random | 31.30% |
| 99.9th percentile random | 33.04% |
| Maximum random (of 10,000) | 39.13% |
| **Empirical p-value** | **< 0.0001** |
| Bonferroni-adjusted p | < 0.0006 |
| **Significant at α=0.05** | **YES** |

AGENT_6 exceeds the maximum of 10,000 random trials. Nonsense feature controls (MD5 hash selections) also cannot match AGENT_6's precision.

AGENT_14 (Random): p = 0.367 — correctly NOT significant. Evaluation NOT contaminated.

---

## 7. Cluster-Adjusted Statistics

| Agent | Selected | Winners | Precision | Lift | Wilson 95% CI | Exact p |
|-------|----------|---------|-----------|------|---------------|---------|
| AGENT_6_LOGISTIC | 115 | 61 | 53.04% | 2.36× | [43.9%, 62.0%] | < 0.001 |
| AGENT_11_SKEPTIC | 85 | 36 | 42.35% | 1.89× | [32.3%, 53.0%] | < 0.001 |
| AGENT_3_BUYER_PRESSURE | 215 | 79 | 36.74% | 1.64× | [30.4%, 43.5%] | < 0.001 |
| AGENT_14_RANDOM | 73 | 18 | 24.66% | 1.10× | [16.1%, 35.8%] | 0.367 |

**Note**: Creator-cluster, launch-day, and wallet-cluster adjusted intervals are PENDING. Effective independent observation count assumes independence (upper bound).

---

## 8. Agent Overlap

AGENT_6 adds unique information beyond simpler agents:

| Compared Against | AGENT_6 Unique Winners | Adds Information? |
|-----------------|----------------------|-------------------|
| AGENT_3 (Buyer Pressure) | Unique winners found | YES |
| AGENT_11 (Skeptic) | Unique winners found | YES |
| AGENT_13 (Simplest Rule) | Unique winners found | YES |
| AGENT_1 (Buy All) | Unique winners found | YES |

Ablation studies PLANNED but not yet executed (require retraining with held-out features).

---

## 9. Completed-Agent Results

5 new agents implemented:

| Agent | Type | Status |
|-------|------|--------|
| AGENT_7_SURVIVAL | Survival/hazard model | Implemented, excluded from S1 claims |
| AGENT_8_PATH_STATE | Price path features | Implemented, excluded from S1 claims |
| AGENT_9_TGE | Token launch features | Implemented, excluded from S1 claims |
| AGENT_10_EXTREME_TAIL | 10x+ tail targeting | Implemented, excluded from S1 claims |
| AGENT_12_CALIBRATED_ENSEMBLE | Multi-agent ensemble | Implemented, excluded from S1 claims |

New agents are excluded from Season 1 blind claims because outcomes are now known.

---

## 10. Multi-Season Results

5 new chronological seasons created. AGENT_6 tested as frozen benchmark:

| Season | Blind Size | Base Rate | AGENT_6 Lift | AGENT_6 Sel/Win |
|--------|-----------|-----------|-------------|-----------------|
| S002 | 493 | 3.65% | 0.00 | 0/0 |
| S003 | 493 | 3.45% | 0.00 | 0/0 |
| S004 | 493 | 14.40% | 3.47 | 2/1 |
| S005 | 493 | 12.37% | 3.89 | 83/40 |
| S006 | 447 | 44.74% | 1.55 | 26/18 |

**Cross-season summary for AGENT_6**:
- Mean lift: 1.78
- Above 1.0 in 3/5 seasons
- Zero selections in 2 seasons (low-rate regimes)
- **Inconsistent** — fails to generalize uniformly

AGENT_8_PATH_STATE outperformed across seasons (mean lift 12.0, 5/5 above 1.0), but this uses post-Season-1 knowledge and cannot claim blind Season 1 credit.

---

## 11. Calibration

| Agent | ECE | Status | Display Mode |
|-------|-----|--------|-------------|
| AGENT_6_LOGISTIC | 0.146 | **POORLY_CALIBRATED** | RANKING_SCORE_ONLY |
| AGENT_3_BUYER_PRESSURE | 0.054 | Moderately calibrated | RANKING_SCORE_ONLY |
| All others | > 0.10 | Poorly calibrated | RANKING_SCORE_ONLY |

**No agent passes calibration gates.** All probability outputs must be treated as:

```
RANKING_SCORE_ONLY
INSUFFICIENT_EVIDENCE_FOR_PROBABILITY
```

---

## 12. Entry+Exit Portfolio Results

Best AGENT_6 strategies at $100 position size:

| Strategy | N Trades | Net Return | Max DD | Top-1 Dep |
|----------|----------|------------|--------|-----------|
| AGENT_6 + HOLD_24H | 89 | +165.3% | 0.93 | 0.58 |
| AGENT_6 + MILESTONE_PARTIALS | 89 | +93.7% | 0.69 | 0.44 |
| AGENT_6 + PRINCIPAL_RECOVERY | 89 | +126.3% | 0.84 | 0.51 |

**WARNING**: Returns are GROSS with simplified cost model (3.1% entry + 3.1% exit). Real execution on illiquid memecoins would face higher costs. Top-1 dependency is high (58% of profits from best trade).

---

## 13. Milestone-Cascade Results

| Stage | Eligible | Positives | Base Rate | CI | Governance |
|-------|----------|-----------|-----------|-----|-----------|
| P(2×) | 740 | 166 | 22.43% | [19.5%, 25.6%] | PROMOTION_ELIGIBLE |
| P(5×\|2×) | 166 | 114 | 68.67% | [61.2%, 75.3%] | PROMOTION_ELIGIBLE |
| P(10×\|5×) | 114 | 83 | 72.81% | [63.8%, 80.2%] | PROVISIONAL |
| P(20×\|10×) | 83 | 13 | 15.66% | [9.3%, 25.0%] | HYPOTHESIS_ONLY |
| P(50×\|20×) | 13 | 7 | 53.85% | [28.5%, 77.2%] | HYPOTHESIS_ONLY |
| P(100×\|50×) | 7 | 7 | 100.0% | [64.6%, 100%] | HYPOTHESIS_ONLY |
| P(200×\|100×) | 7 | 4 | 57.14% | [25.0%, 84.2%] | HYPOTHESIS_ONLY |

**100× and above: HYPOTHESIS_ONLY.** Sample sizes (7 observations) are far too small for any reliable inference. Do NOT infer 100× ability from 2× performance.

---

## 14. PBO and Multiple-Testing Results

- **Total degrees of freedom**: 61 (10 agents, 5 new, 8 feature sets, 6 thresholds, 15 parameter combos, 2 labels, 5 horizons, 3 pre-takeoff defs, 3 exit policies, 4 cost models)
- **Bonferroni correction**: AGENT_6 remains significant (p < 0.0006)
- **BH FDR**: AGENT_6 significant at 5% FDR
- **Winner's curse**: Expected 10-30% shrinkage. Adjusted lift estimate: ~1.89×
- **PBO**: Cannot compute without combinatorial cross-validation across multiple seasons. Status: PENDING

---

## 15. Prospective-Shadow Status

**INFRASTRUCTURE_READY — NOT YET ACTIVATED**

Shadow season protocol defined. Requires:
- Historical gauntlet code frozen
- Agent weights frozen and hashed
- Live data pipeline operational
- Prediction commitment mechanism tested

---

## 16. Binance State

```
LIVE_TRADING_ENABLED         = FALSE
REAL_BINANCE_ORDERS_SUBMITTED = 0
PRODUCTION_API_CREDENTIALS_USED = FALSE
WITHDRAWAL_CAPABILITY         = FALSE
```

Testnet status: **BLOCKED_BY_TESTNET_CREDENTIALS**

Live order locks verified in source code: `RuntimeError` raised on any live order attempt.

---

## 17. Exact Test Counts

| Counted | N |
|---------|---|
| Agents tested (Season 1) | 10 |
| New agents (post-Season 1) | 5 |
| Feature sets tried | 8 |
| Thresholds tried | 6 |
| Parameter combinations | 15 |
| Label versions | 2 |
| Horizons | 5 |
| Pre-takeoff definitions | 3 |
| Exit policies | 3 |
| Cost models | 4 |
| **Total degrees of freedom** | **61** |

---

## 18. Failed and Unevaluated Gates

| Gate | Status |
|------|--------|
| Clustered CI analysis | PENDING |
| Multi-season consistency | PARTIAL FAIL (0 selections in 2/5 seasons) |
| Probability calibration | FAIL (ECE = 0.146) |
| Cost/latency stress test | PENDING |
| Multi-scenario drawdown | PENDING |
| Not season-dependent | PENDING |
| Ablation studies | PENDING |

---

## 19. Artifact Paths

```
artifacts/ascension/gauntlet/governance_state.json
artifacts/ascension/gauntlet/label_reconciliation.json
artifacts/ascension/gauntlet/universe_completeness.json
artifacts/ascension/gauntlet/scoreboard_reproduction.json
artifacts/ascension/gauntlet/control_distributions.json
artifacts/ascension/gauntlet/agent_6_leakage_audit.json
artifacts/ascension/gauntlet/agent_overlap.json
artifacts/ascension/gauntlet/new_seasons.json
artifacts/ascension/gauntlet/calibration_analysis.json
artifacts/ascension/gauntlet/portfolio_tournament.json
artifacts/ascension/gauntlet/milestone_cascade.json
artifacts/ascension/gauntlet/pbo_analysis.json
artifacts/ascension/gauntlet/promotion_evaluation.json
artifacts/ascension/gauntlet/multi_season_championship.json
artifacts/ascension/gauntlet/binance_status.json
artifacts/ascension/gauntlet/prospective_shadow/PROTOCOL.json
artifacts/ascension/gauntlet/gauntlet_master_results.json
artifacts/ascension/seasons/season_1/FROZEN
src/ascension/gauntlet.py
tests/test_ascension_gauntlet.py
```

---

## 20. Honest Verdict

### PROVISIONAL_SIGNAL_CONFIRMED

AGENT_6_LOGISTIC demonstrates a real statistical signal in Season 1 blind evaluation:
- Precision 53.04% vs 22.43% base rate
- Lift 2.36× exceeds the maximum of 10,000 random trials
- No leakage detected across 8 adversarial tests
- Bonferroni-adjusted p < 0.001
- Result reproduces exactly from frozen ledgers

**However, AGENT_6 has NOT earned promotion beyond SEASON_1_LEADER because:**

1. **Calibration fails** — ECE = 0.146 (poorly calibrated). Probability outputs are unreliable.
2. **Multi-season inconsistency** — zero selections in 2 of 5 new seasons. Not generalizable.
3. **Winner's curse** — expected 10-30% shrinkage from tournament selection.
4. **High top-1 dependency** — 58% of portfolio profits from best single trade.
5. **Survivorship bias detected** — blind set has different composition than universe.
6. **Cluster adjustments pending** — creator, launch-day, and wallet clusters not yet analyzed.
7. **Label semantics weak** — raw chart wick ≥ 2× is NOT executable 2× return.

### Final Classification

```
AGENT_6_LOGISTIC_REACHED_2X:
  STATUS           = SEASON_1_LEADER
  PROMOTION_GATE   = 7/13 passed
  NEXT_REQUIREMENT = Multi-season consistency + calibration
  PERMANENT_CHAMPION = UNAWARDED
```

### Safety Confirmation

```
REAL_BINANCE_ORDERS_SUBMITTED  = 0
LIVE_TRADING_ENABLED           = FALSE
PRODUCTION_API_CREDENTIALS_USED = FALSE
WITHDRAWAL_CAPABILITY          = FALSE
```

---

*Generated by ASCENSION GAUNTLET — 65 automated tests passing*
*Do not protect AGENT_6 from failure. Make it earn the crown.*
