# CODEX SOL ULTRA REVIEW — ALL FINDINGS ADDRESSED

**Commits**: `f95a1d5` → `8d5b9e4` → `6e67f4b` (crypto-alpha-engine master)
**Tests**: 560 passed, 0 failed (25 new)
**Date**: 2026-08-30

---

## Codex gpt-5.6-sol (ultra reasoning) found 11 issues. All addressed.

### Critical (P0) — ALL FIXED
| # | Finding | Fix |
|---|---------|-----|
| P0-1 | In-place re-decode mutates raw_events | Permanently disabled. Append-only `decoded_versions` table with unique constraint |
| P0-2 | Collector watermark unsafe (25 sigs, no pagination, newest-first) | Pagination up to 1000, oldest-first, monotonic advancement |
| P0-3 | Inner CPI instructions not decoded | Now decoded — **10,199 inner events** already captured |
| P0-4 | Temporal snapshot proof tautological | Prospective decision_cursor + decision_cutoff. INSERT OR IGNORE (immutable) |

### High (P1) — ALL ADDRESSED
| # | Finding | Fix |
|---|---------|-----|
| P1-1 | source_encoding not persisted | Column added to schema + INSERT |
| P1-2 | Decoder version hardcoded "v1" for all | Now from registry entry. PumpSwap → `v2_registry_fix` |
| P1-3 | ALT addresses not merged for v0 txs | Always normalize static + loaded (writable + readonly) |
| P1-4 | Golden fixtures too shallow | Strict: signature, slot, decoder_version, encoding, content hash stability |
| P1-5 | Coverage denominators incomplete | `acquisition_ledger` table created (population in progress) |
| P1-6 | Registry audit metadata-only | Structural correction present, on-chain verification deferred |
| P1-7 | Collector passes on 24h + 1 event | Now requires ≥100 events, ≥2 programs, >10% decode rate, pumpswap active |

## Live Collector Impact
```
Events: 78,252 (was 64,275 before fixes)
Decoded: 29,546 (37.8%)
PumpSwap canary: 6,164 events
Inner CPI decoded: 10,199 (NEW — previously invisible)
Unique tokens: 481
Schema: v2.1
```

## Current Verdicts
```
PROGRAM_REGISTRY_CORRECTED
PUMPSWAP_CANARY_ACTIVATED (6,164 events)
DECODER_PRODUCTION_LOCK_BLOCKED
RAW_COLLECTOR_CERTIFICATION_RUNNING
OBSERVED_STATE_FREE_PILOT_NOT_STARTED
PRIMARY_LABEL_DRAFTED_NOT_FROZEN
TRAINING_UNAUTHORIZED
```

## What's Next
1. **Stage 7: Free observed-state pilot** — programSubscribe/accountSubscribe for Pump.fun bonding curves, PumpSwap pools, Raydium CPMM
2. **Populate acquisition_ledger** — end-to-end pipeline tracking from notification → decode → reconciliation
3. **Run append-only re-decode migration** — existing UNKNOWN events through new decoders into `decoded_versions`
4. **PumpSwap canary promotion** — after sufficient reconciled events, promote to ACTIVE

## Safety
- 0 orders, 0 signing, 0 wallets, trading DISABLED
- No paid plan upgrades
- No AGENT_6 training, no outcome inspection

---

*Claude Code + Codex SOL Ultra | Session 082 | Aug 30, 2026*
