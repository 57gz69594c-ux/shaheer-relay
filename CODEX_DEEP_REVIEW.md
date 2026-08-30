# ASCENSION Registry Reality Check + Free State Pilot — Deep Review

## Executive verdict

**Overall: BLOCKED — not ready for `DECODER_PRODUCTION_LOCK_PASSED`.**

The official PumpSwap program correction is directionally correct and the specified tests pass, but production certification is blocked by four critical issues:

1. Re-decoding is still destructive: `build_re_decode_utility()` updates `raw_events` in place.
2. `decoded_versions` exists only as optional schema code; neither current database contains the table, and no code writes version rows.
3. The collector’s watermark algorithm can repeatedly refetch transactions, miss high-volume intervals, and advance past unpersisted events.
4. Decoder coverage excludes inner CPI instructions, preventing complete Jupiter/underlying-venue reconstruction.

Verification performed:

```text
194 passed in 0.92s
```

This covered all three requested test modules. No wallets, signing, orders, trading, training, scoring, or outcome-distribution inspection occurred.

---

## Critical findings

### P0-1: Append-only lineage is not implemented end-to-end

The legacy re-decoder directly executes `UPDATE raw_events`, contradicting the append-only requirement: [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:393).

Meanwhile, `ensure_append_only_tables()` only creates `decoded_versions`; nothing inserts into it: [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:1359).

Live schema inspection found:

```text
data/ascension/event_genesis.db: decoded_versions absent
data/event_genesis.db: decoded_versions absent
raw_events.source_encoding: absent
```

Additional design gaps:

- No uniqueness constraint such as `(raw_record_id, decoder_version, registry_hash, code_commit)`.
- `supersedes_version` references a string rather than a row ID.
- No immutable link to `raw_payload_hash`.
- No selected/current-version view.
- No migration-run manifest, cursor, status, counts, or failure ledger.
- No transaction-safe resume semantics.
- `verify_raw_immutability()` hashes only concatenated payload hashes; it does not detect modification of signature, program ID, instruction indices, or other raw-envelope fields.

**Required change:** remove or permanently disable in-place re-decoding. Add an append-only migration runner that inserts one `decoded_versions` row per raw instruction and never modifies `raw_events`.

---

### P0-2: Collector watermark handling is unsafe

The RPC returns recent signatures newest-first. `_poll_program()` processes that order and updates the watermark after every signature: [collector_service.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/collector_service.py:772).

Consequences:

- The final watermark can become the oldest slot in the fetched page, causing repeated refetching.
- The fixed `limit=25` has no pagination; more than 25 new signatures between polls can be silently missed.
- The watermark advances even when `process_transaction()` returns no events.
- It also advances after individual ingestion errors.
- Empty decoder output can therefore become permanent data loss.
- No gap detection or repair is called from the live collector despite resilience classes existing.

**Required change:**

- Paginate with `before`/`until` until reaching the persisted cursor.
- Sort fetched signatures oldest-first before processing.
- Store signature plus slot, not slot alone.
- Commit raw payloads, ingestion results, and watermark atomically.
- Advance only after every targeted instruction is either durably stored or durably quarantined.
- Record fetch/decode failures in a retry queue.
- Add burst tests with more than 25 signatures, same-slot signatures, partial failures, and restart recovery.

---

### P0-3: CPI/inner-instruction decoding is missing

`process_transaction()` only iterates outer message instructions: [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:2215).

It stores `innerInstructions` as JSON but never decodes them. This creates major blind spots:

- Jupiter route legs cannot be reconstructed.
- DEX calls invoked through aggregators or arbitrage routers are missed.
- `inner_instruction_index` is always `-1`.
- Jupiter double-counting analysis cannot reliably detect inner-venue overlap.
- The claimed canonical identity `signature:outer_ix:inner_ix` is not actually generated.

**Required change:** normalize outer and inner instructions into one instruction-envelope iterator containing:

```text
signature
outer_instruction_index
inner_instruction_index
stack_height
program_id
accounts
data
source_encoding
```

Decode each recognized inner CPI separately. Treat Jupiter routes as router envelopes and underlying CPIs as economic swap legs.

---

### P0-4: Temporal snapshot proof is currently vacuous

`CausalSnapshotBuilder.build_snapshot()` queries historical events and then sets the frozen cursor to the maximum ID it just selected: [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:2731).

Therefore:

- `event_id <= frozen_cursor` is true by construction.
- `snapshot_commit_time = time.time()` allows late/backfilled records received before the builder runs, even if they were unavailable at the intended decision time.
- The test called “Earlier event learned after cutoff rejected” does not assert that the late event is excluded.
- `store_snapshot()` uses `INSERT OR REPLACE`, contradicting immutable snapshots: [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:2874).

**Required change:** capture `decision_cursor` and `collector_first_seen_at_cutoff` prospectively when each decision age becomes due. Query using both immutable bounds:

```sql
id <= :decision_cursor
AND provider_receipt_time <= :decision_cutoff
AND chain_slot_time <= :chain_cutoff
```

Use plain `INSERT`, versioned correction rows, and a unique immutable snapshot identity.

---

## High-severity findings

### P1-1: Encoding provenance is not persisted

`RawEvent.source_encoding` exists in memory, but `raw_events` has no corresponding column and `EventIngester` does not insert it: [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:832).

The report says “per-field provenance,” but implementation records only one request-level value on the transient object. Inner instructions may have separate provenance and are not represented.

**Fix:** add encoding provenance to immutable instruction envelopes and `decoded_versions`. Backfill legacy rows as `ENCODING_PROVENANCE_UNKNOWN` unless the stored RPC envelope proves the encoding.

---

### P1-2: Decoder version pinning is incorrect

Every event produced by `process_transaction()` is assigned `event_genesis_v1`, including PumpSwap, whose registry declares `event_genesis_v2_registry_fix`: [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:2270).

The test explicitly requires the old version for every event, so it preserves the bug: [test_decoder_production.py](/root/shaheer-project/crypto-alpha-engine/tests/test_decoder_production.py:552).

**Fix:** obtain the decoder version from the matched registry entry and persist registry hash, decoder hash, code commit, and IDL hash with every decoded version.

---

### P1-3: Versioned transactions with ALTs are incompletely resolved

Loaded addresses are appended only when `message.accountKeys` is empty. For typical v0 transactions with static keys present, loaded addresses are not appended: [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:2176).

This can make program and account indices unresolved. Existing ALT tests assert only “does not crash,” not correct decoding.

**Fix:** always normalize static keys plus `loadedAddresses.writable` plus `loadedAddresses.readonly` in canonical Solana ordering. Add real ALT fixtures asserting program ID, pool, mints, and account indices.

---

### P1-4: Decoder semantic coverage is too shallow

Golden-fixture results show:

- PumpSwap buy/sell: pool present, token mint absent.
- All P1 swaps: token mint absent.
- Jupiter: no pool or mint.
- Some Raydium/Orca fixture events lack direction or pool.
- Failed fixture decodes as `UNKNOWN`, not `FAILED_TRANSACTION`.
- PumpSwap events carry `event_genesis_v1`.

The failed-transaction test even allows `SWAP`, which contradicts its own safety comment: [test_decoder_production.py](/root/shaheer-project/crypto-alpha-engine/tests/test_decoder_production.py:371).

**Fix:** golden fixtures must assert exact instruction identity and mandatory semantic fields, not merely venue/event type. Failed targeted instructions must always produce a quarantined failed-event record and never an economic event.

---

### P1-5: Coverage denominators are not true end-to-end denominators

`measure_coverage_denominators()` derives everything from rows already inserted into `raw_events`: [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:979).

It cannot measure:

- Notifications never fetched.
- Transactions fetch failures.
- Target instructions dropped before insertion.
- Unsupported transaction versions.
- CPI instructions never enumerated.
- Account-resolution failures.
- Provider coverage gaps.

Most `NonDecodedReason` values are never populated.

**Fix:** create an append-only acquisition ledger for notification → signature → transaction fetch → instruction envelope → decode → reconciliation, with exactly one terminal status per stage.

---

### P1-6: Registry audit is metadata, not an audit

The official correction to `pAMMBay...` is present and `PSwapMd...` is excluded from active collection. However, `audit_program_registry()` performs no RPC/on-chain verification: [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:1277).

Problems:

- Only PumpSwap contains embedded on-chain audit metadata.
- Upgrade authority and program-data identity are not checked.
- Historical PSwapMd count is hardcoded to zero.
- Registry hash includes only keys and program IDs, excluding status, versions, decoder hashes, and IDL hashes.
- PumpSwap becomes “canary activated” from any raw row, not a successfully decoded/reconciled event.
- P0 decoder boundaries are not marked because boundary logic checks only `P1_DECODER_REGISTRY`.

**Conclusion:** registry correction is structurally present but certification is incomplete.

---

### P1-7: Raw collector can pass without proving coverage

`determine_verdict()` promotes the raw collector after 24 hours and one event, regardless of provider qualification, program coverage, gaps, restarts, decode failures, or manifests: [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:1616).

`CertificationState.verdict()` similarly uses elapsed time, unrepaired-gap count, and snapshot count, but the collector never runs gap detection.

**Fix:** define a frozen pass predicate requiring complete acquisition evidence, finalized reconciliation, acceptable lag, zero unexplained gaps, and per-program coverage.

---

## Medium-severity findings

- `tokens` and `pools` registries are never populated by ingestion; collector metrics for these remain misleading.
- Certification restart recovery restores only start time/slot and restart count; all other counters reset.
- Gap detection treats every slot without a tracked-program event as missing, which is invalid for a filtered program ledger.
- Daily manifests call coverage `COMPLETE` whenever no gaps were recorded, even though gaps may never have been measured.
- Watermark updates use delete-plus-insert rather than monotonic compare-and-set, permitting regression.
- `PrimaryLabelSpec.freeze()` is not actually irreversible; fields remain mutable and `freeze()` can be called repeatedly.
- Training authorization ignores `prospective_shadow_required`, holdout-day requirements, and rare-label thresholds. Training remains hardcoded unauthorized in current verdicts, but the gate itself is unsafe.
- “Implements 10 stages” is inconsistent with stages numbered 0 through 10—eleven stages—and older stage headings remain interleaved.
- Provider pricing in the memo is hardcoded and unverified; it should be marked dated/unverified or removed until a purchase decision is requested.

---

## Stage 7 free observed-state pilot

Stage 7 is currently only a report plan; no capture code, schema, subscription manager, coherence certificate, or tests exist.

### Minimum implementation

Add an append-only observed-state ledger with:

```text
account_pubkey
owner_program
slot
write_version_if_available
subscription_type
subscription_id
provider_receipt_time
ingestion_time
context_slot
raw_account_data
raw_data_hash
encoding
reconnect_epoch
is_baseline
is_update
```

Implement:

1. Baseline `getMultipleAccounts` capture for required venue accounts.
2. `programSubscribe` for stable program-owned state.
3. `accountSubscribe` for discovered vaults, tick arrays, bin arrays, oracles, bitmaps, and open-orders accounts.
4. Reconnect with baseline refresh and explicit gap epochs.
5. Slot-coherence certificates listing every required account and its observed slot.
6. Fail-closed reconstruction when required state is missing, stale, cross-slot ambiguous, or acquired after the decision cutoff.
7. Traffic and credit measurement only—no label outcomes or token scoring.

### Recommended first venue sequence

1. Pump.fun bonding curve.
2. PumpSwap constant-product pool and vaults.
3. Raydium CPMM.
4. Raydium V4 with AMM state, vaults, and OpenBook/open-orders dependencies.
5. Only then attempt CLMM/DLMM/Whirlpool, whose dynamic state sets are substantially harder.

The pilot must remain classified `OBSERVED_ACCOUNT_SNAPSHOT`; it must not claim exact intra-slot account-change reconstruction.

---

## Path to `DECODER_PRODUCTION_LOCK_PASSED`

A credible pass requires all of the following:

- Registry entries independently verified and hash-pinned.
- Raw instruction envelopes immutable and provenance-complete.
- Append-only re-decode migration completed and reproducible.
- CPI and ALT decoding supported.
- Decoder version and registry hash correct per row.
- Exact fixture assertions for all supported variants.
- Acquisition denominators include pre-insertion losses.
- Failed/malformed/unsupported items durably quarantined.
- Per-program activation and certification boundaries recorded without backdating.
- Independent replay reproduces identical decoded rows.
- No raw-row mutation before and after migration.
- PumpSwap canary promoted only after sufficient successful, finalized semantic reconciliation.
- Current hardcoded `DecoderVerdict.BLOCKED` replaced with a computed frozen gate.

Until then, the correct state remains:

```text
PROGRAM_REGISTRY_CORRECTED
PUMPSWAP_CANARY_ACTIVATED
DECODER_PRODUCTION_LOCK_BLOCKED
OBSERVED_STATE_FREE_PILOT_NOT_STARTED
PRIMARY_LABEL_DRAFTED_NOT_FROZEN
TRAINING_UNAUTHORIZED
```

---

## Prioritized action plan

### P0 — immediate

1. Delete/disable in-place re-decoding and implement append-only `decoded_versions`.
2. Fix collector pagination, ordering, atomic watermark advancement, retry ledger, and restart state.
3. Normalize and decode inner CPI instructions.
4. Repair prospective snapshot cursor/time semantics and remove `INSERT OR REPLACE`.
5. Persist source encoding and immutable instruction identity.

### P1 — certification

6. Normalize ALT-loaded addresses.
7. Add strict golden fixtures for exact accounts, mints, pools, direction, failure state, CPI path, Token-2022, malformed data, and unsupported versions.
8. Build genuine acquisition and coverage denominators.
9. Implement real per-program registry verification and activation records.
10. Compute the decoder verdict from frozen pass criteria instead of hardcoding it.

### P2 — free pilot

11. Implement observed-account schema and subscription service.
12. Pilot Pump.fun, PumpSwap, and Raydium CPMM reconstruction.
13. Produce coherence, gap, traffic, and cost certificates.
14. Keep labels unfrozen, training unauthorized, and outcome access sealed.

## Safety assessment

The active collector has a strong read-only RPC allowlist and explicit denial of transaction submission. No wallet or signing APIs appear in the reviewed path.

Remaining hardening:

- Move the allowlist into the base RPC transport so no production code can instantiate an unrestricted `SolanaRPCClient`.
- Replace safety `assert` statements with unconditional runtime guards because Python optimization can remove assertions.
- Add CI scans for wallet/keypair/signing/submission imports and RPC methods.
- Preserve the enforced state: **0 orders, 0 signing, 0 wallets, trading disabled.**