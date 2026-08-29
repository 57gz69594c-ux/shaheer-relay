BEGIN CHATGPT TRADING ENGINE AUDIT PACKET

TRADING_ENGINE_AUDIT_PACKET_20260829T145654Z_8d46c96

================================================================
PUBLIC-SAFE SUMMARY
================================================================

This is the public relay pointer for a comprehensive forensic audit
of the trading engine. The full audit packet is stored locally due
to the PUBLIC visibility of this repository.

REPOSITORY VISIBILITY: PUBLIC
FULL PACKET STATUS: LOCAL ONLY (contains proprietary logic)
FULL PACKET LOCATION: [REDACTED — private server path]
FULL PACKET SHA-256: 00ae7a50d5739c33bf398eac6147b10146d4247afde7b29e043c1f98002d41b3

================================================================
COMPLETENESS CHECKLIST
================================================================

| Section                          | Status    |
|----------------------------------|-----------|
| Exact code snapshot              | COMPLETE  |
| Effective runtime configuration  | COMPLETE  |
| Signal and feature specification | COMPLETE  |
| Risk and sizing logic            | COMPLETE  |
| Exit-management state machine    | COMPLETE  |
| Market-data integrity            | PARTIAL   |
| MT5 execution behavior           | COMPLETE  |
| Runtime and recovery behavior    | COMPLETE  |
| Raw signals, including rejected  | PARTIAL   |
| Orders, deals, positions, trades | COMPLETE  |
| Backtest methodology             | PARTIAL   |
| Shadow results                   | COMPLETE  |
| Demo results                     | COMPLETE  |
| Live results                     | MISSING   |
| Broker symbol specifications     | COMPLETE  |
| Costs, spread, commission, swap  | PARTIAL   |
| Test inventory and results       | PARTIAL   |
| Incident and failure evidence    | COMPLETE  |

================================================================
KEY FINDINGS SUMMARY (public-safe)
================================================================

GROUND TRUTH:
- System is DEMO ONLY (multiple safety locks confirmed)
- 8 instruments (7 rule-based + 1 ML-based)
- M15 timeframe, 88 features (not 83 as previously reported)
- 9 execution gates, all fail-open on error
- 3-phase trailing stop system (breakeven/trailing/TP-extension)

CONFIRMED CONTRADICTIONS (6):
- Feature count mismatch (88 vs reported 83)
- Max TP extensions (1, not 5 as reported)
- Trailing system architecture (2-phase, not 8-tier as reported)
- Trail metric (SL fraction, not 18% MFE as reported)
- Confidence threshold inconsistency across files
- Score=0 in all order log entries

CRITICAL BUGS FOUND (5):
- HTF confirmation gate silently fails (TypeError, never executes)
- Signal dedup reset function defined but never called
- Demo account check bypassed by REST adapter layer
- Spread features receive zero values from adapter
- Spread double-counted in position sizing

PERFORMANCE (DEMO, 4 DAYS, 26 TRADES):
- Negative expectancy on valid trades
- P/L calculation bug produces impossible values
- Strategy changed mid-sample (SL multiplier fix)
- Sample too small for statistical significance

BLOCKERS TO PROFITABILITY CONCLUSION (10):
- Insufficient sample size
- P/L calculation bug
- Configuration changed mid-sample
- No untouched test set
- No cost modeling
- No baseline comparison
- External Edge pipeline empty
- Shadow Lab incomplete
- Inverse policy based on 8 trades
- Negative expectancy on demo

QUESTIONS FOR OWNER (6):
- Commission/swap rates for Dominion Markets
- Hedging vs netting mode
- Gold ML runner activity status
- score=0 logging issue source
- Backtest result provenance
- Trailing state persistence intent

================================================================
ACCESS
================================================================

The full audit packet with all evidence references, code citations,
worked examples, reconciliation tables, and detailed findings is
available locally. A private relay destination is required for the
complete packet to be published remotely.

To request the full packet, provide a private repository or
secure transfer mechanism.

================================================================
AUDIT METADATA
================================================================

Audit timestamp: 2026-08-29T14:56:54Z
Engine commit: 8d46c96ac726c38674f52f16a67a5beafb9c17ca
Engine branch: master
Worktree: dirty (4 modified, 20+ untracked)
Auditor: Claude Code (read-only, zero modifications to engine)
Engine modified: NO
Trading action taken: NO
Evidence references: 89 citations (E001-E089)

END CHATGPT TRADING ENGINE AUDIT PACKET
