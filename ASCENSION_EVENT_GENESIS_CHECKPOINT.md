# ASCENSION Event Genesis — Checkpoint Report

## Verdict: RAW_PROVIDER_BLOCKED

Pipeline infrastructure complete. Awaiting qualifying Solana RPC provider.

## What Was Built

### Stage 0 — Reality Lock Corrigendum (8 corrections)
1. Legacy target renamed: `LEGACY_DEXSCREENER_TRAILING_24H_PRICE_RATIO`
2. Operational status: `FRAMEWORK_ONLY` / `COLLECTOR_NOT_OPERATIONAL`
3. AGENT_6_LEGACY_V1 = `QUARANTINED_LEGACY_CONTROL` (was RESEARCH_CANDIDATE)
4. Platform result: `TEMPORAL_ARTIFACT` / `PLATFORM_CONFOUNDED`
5. RIGHT_CENSORED replaced with `MISSING_LEGACY_OUTCOME` (exact counts published)
6. ECE, lift, p-values, portfolio: `INVALIDATED_LEGACY_DIAGNOSTICS`
7. Multiple-testing count: `UNKNOWN_PENDING_COMPLETE_REGISTRY`
8. Arena reusable; provenance gates failed; framework tests are not alpha evidence

### Stage 2 — Provider-Neutral Solana RPC
- Public mainnet RPC verified (genesis hash confirmed: Solana mainnet-beta)
- Provider-neutral adapter supports Helius, custom RPC, or public endpoint
- Credentials never appear in logs or artifacts
- Read-only operations ONLY — no signing, no wallet loading

### Stage 3 — Raw Append-Only Event Ledger
- 12-table schema (v2.0) with 62-column raw event storage
- P0 decoders: Pump.fun (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`) + PumpSwap (`PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP`)
- P1 registered (not active): Raydium, Meteora, Orca, Jupiter
- Three-clock model: chain slot time, provider receipt time, ingestion time
- Append-only, hash-manifested, idempotent, restart-safe
- Token-2022 extensions, transfer fees, freeze hooks tracked

### Stage 4 — Resilience and Gap Recovery
- Separate processed/confirmed/finalized watermarks
- Gap detection distinguishes skipped Solana slots from missing data
- Immutable daily UTC manifests
- Unrepaired gaps → COVERAGE_DEGRADED

### Stage 5 — Causal Snapshot Certification
- 12 frozen ages: T+30s through T+24h
- Dual temporal eligibility: cursor AND receipt-time
- Backfill events blocked from prospective snapshots
- Pool lifecycle tracked: initialization → activation → first swap

### Stage 7 — Bounded Canary
- 5 Pump.fun signatures fetched from Solana mainnet
- 4 events extracted and ingested (tagged `NON_QUALIFYING_CANARY`)
- Genesis hash verified, health confirmed "ok"

## Tests: 294 passed (224 existing + 70 new), 0 failed

### Stage 6 Adversarial Tests (20 directive-required)
Future event rejected | Backfill receipt-time blocks entry | Duplicate delivery | Payload before cursor | Restart safety | Gap detection | RPC timeout/error | Clock skew/null | Same-slot ordering | Fork reversal (new record) | Malformed payload | Unknown program quarantined | Failed transactions | Migration/multiple pools | Token-2022 extensions | Unrepaired gap fails closed | Genesis mismatch | API keys absent | Zero signing | Corrigendum verified

## Remaining Blocker

**Set `HELIUS_API_KEY` environment variable** (free tier at helius.dev) to activate qualifying collection.

Alternative: Set `SOLANA_RPC_URL` to a private Solana HTTP RPC endpoint.

## Safety

```
LIVE_TRADING_ENABLED = FALSE
REAL_ORDERS_SUBMITTED = 0
WALLET_TRANSACTIONS_SIGNED = 0
PRODUCTION_API_CREDENTIALS_USED = FALSE
WITHDRAWAL_CAPABILITY = FALSE
```

Zero signing. Zero transaction submission. DexScreener aggregates prohibited from qualifying data.

## Commit: 6c85e10

---
*Claude Code | Session 074 | Aug 29, 2026*
