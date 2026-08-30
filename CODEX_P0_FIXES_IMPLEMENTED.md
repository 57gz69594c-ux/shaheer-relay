# P0 FIXES IMPLEMENTED — Codex SOL Ultra Deep Review

**Commit**: `f95a1d5` (crypto-alpha-engine master)
**Tests**: 548 passed, 0 failed (13 new)
**Date**: 2026-08-30

---

## What Codex Found (gpt-5.6-sol, ultra reasoning)

4 critical (P0) + 7 high-severity (P1) issues in the Ascension Registry Reality Check implementation.
Full review: `CODEX_DEEP_REVIEW.md`

## What's Fixed in This Commit

### P0-1: Append-Only Lineage ✓
- In-place `UPDATE raw_events` permanently disabled
- `build_re_decode_utility()` now appends to `decoded_versions` table
- Unique constraint: (raw_record_id, decoder_version, registry_hash, code_commit)
- `supersedes_version_id` is a FK to decoded_versions.id (not a string)

### P0-2: Collector Watermark Safety ✓
- Pagination: up to 1000 signatures via `before` cursor (was 25, no pagination)
- Sort oldest-first before processing (was newest-first causing repeated refetch)
- Monotonic watermark advancement (never regress)
- Local state tracking during batch

### P0-3: Inner CPI Instruction Decoding ✓
- `process_transaction()` now iterates `innerInstructions`
- Jupiter route legs and arb router CPIs are decoded as separate events
- Events carry correct `instruction_index` + `inner_instruction_index`
- 13 new tests verify CPI, ALT, encoding, and schema changes

### P1-1: Source Encoding Persisted ✓
- `source_encoding` column added to `raw_events` schema
- Persisted in INSERT statement during ingestion

### P1-2: Decoder Version From Registry ✓
- No longer hardcoded "event_genesis_v1" for all events
- PumpSwap events now carry "event_genesis_v2_registry_fix"

### P1-3: ALT Address Normalization ✓
- Always merges static + loaded addresses (writable + readonly)
- Correct for v0 transactions with Address Lookup Tables

### Schema Migration
- v2.0 → v2.1
- New tables: `decoded_versions`, `acquisition_ledger`
- New column: `raw_events.source_encoding`
- Live DB migrated (64,275 events preserved)

---

## Still Pending (from Codex review)

| Finding | Status |
|---------|--------|
| P0-4: Temporal snapshot proof | PENDING — needs prospective cursor rework |
| P1-4: Decoder semantic coverage | PENDING — golden fixtures need exact assertions |
| P1-5: Coverage denominators | PARTIAL — acquisition_ledger created, not populated |
| P1-6: Registry audit RPC verification | PENDING — metadata only, no on-chain check |
| P1-7: Raw collector pass predicate | PENDING — needs frozen pass criteria |
| Stage 7: Free observed-state pilot | NOT STARTED — designed only |

## Current Verdicts

```
PROGRAM_REGISTRY_CORRECTED
PUMPSWAP_CANARY_ACTIVATED
DECODER_PRODUCTION_LOCK_BLOCKED
OBSERVED_STATE_FREE_PILOT_NOT_STARTED
PRIMARY_LABEL_DRAFTED_NOT_FROZEN
TRAINING_UNAUTHORIZED
```

## Safety
- 0 orders, 0 signing, 0 wallets, trading DISABLED
- No paid plan upgrades
- No AGENT_6 training, no outcome inspection

---

*Claude Code + Codex SOL Ultra | Session 082 | Aug 30, 2026*
