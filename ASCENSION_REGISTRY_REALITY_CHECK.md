# ASCENSION REGISTRY REALITY CHECK + FREE STATE PILOT

**Commit**: `a48a597` (crypto-alpha-engine master)
**Tests**: 535 passed, 0 failed
**Date**: 2026-08-30

---

## CRITICAL REGISTRY CORRECTION (Stage 1)

`PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP` is **NOT** official PumpSwap.
It is a separate deployed BPF program used as inner CPI venue by arb routers.
Discriminator `38fc74089edfcd5f` does not match any official pump.fun method.
**0 historical events affected** (PSwapMd always had 0 events).

**Official pump.fun Pump AMM**: `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`
- On-chain verified: BPFLoaderUpgradeab1e, executable=True
- IDL discriminators match: buy=`66063d1201daebea`, sell=`33e685a4017f83ad`
- Golden fixtures captured: `pumpswap_buy.json` + `pumpswap_sell.json`
- Status: **CANARY** (collecting, own certification clock, not backdated)

---

## CORRECTED SUBSYSTEM VERDICTS (Stage 0)

| Subsystem | Verdict |
|-----------|---------|
| Program Registry | `PROGRAM_REGISTRY_CORRECTED` |
| PumpSwap Activation | `PUMPSWAP_ACTIVATION_UNVERIFIED` (canary just started) |
| Raw Collector | `RAW_COLLECTOR_CERTIFICATION_RUNNING` (~36%) |
| Decoder Production Lock | `DECODER_PRODUCTION_LOCK_BLOCKED` |
| State Capture | `EXACT_STATE_CAPTURE_NOT_STARTED` |
| Executable Label | `EXECUTABLE_LABEL_COHORT_NOT_STARTED` |
| Training | `TRAINING_UNAUTHORIZED` |
| Paid Exact State | `PAID_EXACT_STATE_APPROVAL_NOT_YET_REQUESTED` |

---

## 10-STAGE IMPLEMENTATION

### Stage 1: Program Registry Reality Check
- All 10 program IDs verified on-chain (BPFLoaderUpgradeab1e, executable)
- PSwapMd reclassified under `pswapmd_unidentified` (RECLASSIFIED)
- pAMMBay added as `pumpswap_amm_v1` (CANARY)
- Registry hash computed for provenance

### Stage 2: Append-Only Lineage
- `decoded_versions` table created (append-only, keyed by raw_record_id)
- Raw immutability verification via SHA-256 over raw_payload_hash
- No more in-place overwrites of decoded records

### Stage 3: Field-Level Encoding Contract
- Per-field provenance, not request-level inference
- PROVENANCE_UNKNOWN -> quarantine/fail-closed
- Tests for both JSON instruction strings and serialized envelopes

### Stage 4: Decoder Certification (expanded)
- 11 golden fixtures (9 original + pumpswap_buy + pumpswap_sell)
- Fixture matrix requires: buy/sell, init/migration, v0+ALT, CPI, Token-2022, WSOL, failed, malformed, unsupported

### Stage 5: Measured Coverage
- Per-program/version n/N funnels (notification -> decoded -> reconciled)
- NonDecodedReason classification for every remainder
- Global 38.9% is NOT decoder coverage -- per-program breakdown required

### Stage 6: Activation vs Certification
- Per-program boundaries: raw_capture, decoder, observed_state, exact_state, frozen_label
- `activated_at` separated from `certified_at` -- never backdated

### Stage 7: Free Observed-State Pilot (designed, not started)
- Standard programSubscribe/accountSubscribe -- no plan upgrade needed
- Venue-specific reconstruction verdicts:
  - Pump.fun / PumpSwap / CPMM: **PENDING** (may pass with observed state)
  - CLMM / DLMM / Whirlpool: **BLOCKED** (need tick/bin state, fee oracle)

### Stage 8: Causal Label Track (draft)
- `CAPTURED_STATE_QUOTE_RETURN_V1`
- State-conditional quote opportunity, NOT actual fill
- Missed fleeting states -> conservative false negatives
- Training/scoring NOT allowed from this track yet

### Stage 9: Primary Label Draft
- Versioned hash, NOT frozen, outcome access sealed
- Freeze only after exact-state replay passes

### Stage 10: Provider Decision Memo (no purchase)
- Triton Fumarole/Riptide: ~$199-499/mo
- Helius LaserStream Business: ~$499/mo
- QuickNode Solana gRPC: ~$299-799/mo
- Return: `PAID_EXACT_STATE_APPROVAL_NOT_YET_REQUESTED`

---

## COLLECTOR STATUS

- Events: ~49,800 and climbing
- Tokens: 387 unique
- Hours: 8.6 of 24h (35.7%)
- Decode rate: 39.0%
- pAMMBay events: 0 (canary just activated)
- Restarts: 3 (latest: registry correction)
- 429s: 0 | Failures: 0

---

## SAFETY

- 0 orders, 0 signing, 0 wallets, trading DISABLED
- No paid plan upgrades without explicit approval
- No AGENT_6 training, no outcome inspection, no coin scores

---

*Claude Code | Session 081 | Aug 30, 2026*
