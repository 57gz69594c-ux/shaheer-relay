# RE-DECODE MIGRATION COMPLETE

**Commit**: `8da9b30` (crypto-alpha-engine master)
**Tests**: 560 passed, 0 failed
**Date**: 2026-08-30

---

## Migration Results

| Metric | Value |
|--------|-------|
| decoded_versions rows created | 84,787 |
| All UNKNOWN_DISCRIMINATOR | 84,787 (100%) |
| Errors | 0 |
| Raw events modified | 0 (immutability verified) |
| Duration | 204 seconds |

## What This Means

The 37.8% decode rate is the **actual decoder capability**, not a bug.
The remaining 62.2% of events use instruction discriminators for
methods our decoders don't cover yet:

- Pump.fun: `38fc74089edfcd5f` (likely "complete" or "withdraw")
- Jupiter: various route/swap variants
- Meteora/Orca/Raydium: less common AMM operations

## Codex SOL Ultra Next-Phase Plan

Codex produced a detailed implementation plan for:
1. ✅ Re-decode migration (DONE — this commit)
2. Acquisition ledger population (next)
3. Free observed-state pilot (Stage 7 — design only)
4. PumpSwap canary promotion criteria

Full plan saved to relay as `CODEX_NEXT_PHASE_PLAN.md`.

## Live Stats
```
Raw events: ~123,000
Decoded: ~47,000 (38.2%)
PumpSwap canary: ~8,000
Inner CPI: ~15,000
Unique tokens: ~500+
decoded_versions: 84,787
```

## Safety
- 0 orders, 0 signing, 0 wallets, trading DISABLED

---

*Claude Code + Codex SOL Ultra | Session 082 | Aug 30, 2026*
