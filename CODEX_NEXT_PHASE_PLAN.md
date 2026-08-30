## Executive recommendation

Implement items 1–2 as two explicit, resumable maintenance commands:

1. `redecode-unknowns`: reads immutable `raw_events`, reconstructs each exact instruction, and appends one `decoded_versions` row for every selected raw row—including rows that remain `UNKNOWN`.
2. `backfill-acquisition-ledger`: derives the strongest defensible historical pipeline evidence from `ingestion_log` and `raw_events`, clearly labeling inferred records. Then instrument the live collector so future ledger entries are exact.

Do not run either command until dry-run checks, a SQLite backup, raw-ledger hash capture, and collector-write coordination are complete.

The existing `build_re_decode_utility()` is a useful skeleton, but should not be run as-is. Its main deficiencies are:

- It matches only `program_id`, so multiple instructions for one program in one transaction can be associated with the wrong raw row.
- It skips any raw row having any active decoded version, even when the current `(decoder_version, registry_hash, code_commit)` has not been run.
- It appends nothing for a still-unknown result, losing evidence that the attempt occurred.
- `INSERT OR IGNORE` increments `appended` even when nothing was inserted.
- It accepts `"unknown"` provenance defaults.
- It commits one large migration with no checkpoint or bounded batches.

Relevant code is in [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:393), the authoritative schema is in [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:1283), transaction reconstruction is in [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:2268), and the live collector pipeline is in [collector_service.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/collector_service.py:827).

# 1. Append-only re-decode migration

## 1.1 Functions to create or change

### In `src/ascension/label_genesis.py`

Replace/refactor `build_re_decode_utility()` into these focused functions:

```python
def canonical_registry_payload() -> bytes: ...
def compute_registry_hash() -> str: ...
def resolve_code_commit(project_root: Path) -> str: ...
def select_redecode_candidates(
    conn, decoder_versions_by_program, registry_hash, code_commit,
    after_id=0, limit=1000
) -> list[sqlite3.Row]: ...
def match_redecoded_event(raw_row, decoded_events) -> RawEvent | None: ...
def serialize_decoded_fields(event, outcome, reason=None) -> str: ...
def append_decoded_version(conn, raw_row, event, provenance) -> str: ...
def run_append_only_redecode(
    conn,
    code_commit: str,
    registry_hash: str,
    batch_size: int = 500,
    dry_run: bool = True,
    max_rows: int | None = None,
) -> ReDecodeReport: ...
```

Keep `build_re_decode_utility()` temporarily as a compatibility wrapper, but require explicit non-placeholder provenance:

```python
def build_re_decode_utility(conn, code_commit, registry_hash):
    if code_commit in ("", "unknown") or registry_hash in ("", "unknown"):
        raise ValueError(...)
    return run_append_only_redecode(..., dry_run=False)
```

Use `ensure_append_only_tables()` rather than maintaining a second inline table definition. That function already models the intended FK and unique constraint at [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:1428).

### New command module

Create `src/ascension/redecode_migration.py` with a CLI:

```text
python -m src.ascension.redecode_migration \
  --db data/ascension/event_genesis.db \
  --dry-run

python -m src.ascension.redecode_migration \
  --db ... \
  --execute \
  --expected-raw-count 78252 \
  --expected-unknown-count 48706 \
  --expected-ledger-hash <preflight hash>
```

Options should include `--after-id`, `--max-rows`, `--batch-size`, `--code-commit`, and `--registry-hash`. `--execute` must be explicit; default is dry-run.

## 1.2 Registry hash

Hash the decoder registry semantically, not via `repr()` and not merely by hashing the source file.

Build a canonical object from `VERIFIED_PROGRAMS`, ordered by registry key and recursively serialized with:

```python
json.dumps(
    normalized_registry,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
```

Then:

```python
registry_hash = hashlib.sha256(canonical_bytes).hexdigest()
```

Include every decoder-relevant field:

- registry key
- `program_id`
- venue
- program/version
- decoder version
- status
- supported event types
- discriminator source
- any IDL/SDK identifier or hash
- registry classification such as `RECLASSIFIED`

Exclude operational timestamps that do not affect decoding, such as an audit time, unless they intentionally define the registry version.

To prevent silent changes, add:

```python
def registry_hash_manifest() -> dict:
    return {
        "algorithm": "sha256",
        "canonicalization": "json-sort-keys-compact-v1",
        "registry_hash": ...,
        "program_count": ...,
    }
```

The registry currently lives at [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:623).

## 1.3 Code commit

Resolve once at command startup:

1. Prefer an explicit `--code-commit`.
2. Otherwise execute `git rev-parse HEAD`.
3. Reject an empty result, `"unknown"`, or anything not matching `^[0-9a-f]{40}$`.

The inspected commit is `6e67f4bcdd114930d87e26f1115812db741bf521`, but it must be resolved again at actual migration time.

Because the working tree is currently dirty, record that fact in the migration manifest. A bare commit does not uniquely identify uncommitted decoder code. Safest policy:

- Production migration: require a clean worktree.
- Emergency override: compute `code_commit` as `<HEAD>+dirty.<sha256(diff)>`, but this would require relaxing the current field format and is less desirable.
- Recommended: commit the decoder/migration implementation and run using that clean commit.

`code_commit` identifies code; `registry_hash` identifies registry configuration. Do not substitute one for the other.

## 1.4 Candidate selection SQL

Select only original raw rows classified as unknown/incomplete, and exclude only an identical decode run:

```sql
SELECT
    r.id,
    r.signature,
    r.slot,
    r.program_id,
    r.instruction_index,
    COALESCE(r.inner_instruction_index, -1) AS inner_instruction_index,
    r.source_encoding,
    r.raw_payload,
    r.raw_payload_hash,
    r.data_origin,
    r.event_type,
    r.venue
FROM raw_events AS r
WHERE r.id > :after_id
  AND (
      r.event_type IS NULL
      OR r.event_type = 'UNKNOWN'
      OR r.venue IS NULL
      OR r.venue = ''
  )
  AND NOT EXISTS (
      SELECT 1
      FROM decoded_versions AS d
      WHERE d.raw_record_id = r.id
        AND d.decoder_version = :target_decoder_version
        AND d.registry_hash = :registry_hash
        AND d.code_commit = :code_commit
  )
ORDER BY r.id
LIMIT :batch_size;
```

Because decoder versions vary by program, either:

- join candidates to an in-memory `program_id → decoder_version` map and apply the `NOT EXISTS` check per row; or
- load active registry values into a temporary table and join it.

Do not use the current `invalidated = 0 LIMIT 1` test. An older version is not evidence that the current version was run.

Preflight counts:

```sql
SELECT COUNT(*) FROM raw_events;

SELECT COUNT(*)
FROM raw_events
WHERE event_type IS NULL
   OR event_type = 'UNKNOWN'
   OR venue IS NULL
   OR venue = '';

SELECT program_id, COUNT(*)
FROM raw_events
WHERE event_type IS NULL
   OR event_type = 'UNKNOWN'
   OR venue IS NULL
   OR venue = ''
GROUP BY program_id
ORDER BY COUNT(*) DESC;
```

## 1.5 Exact instruction matching

After `process_transaction()` reconstructs the transaction, match on the complete raw identity:

```python
(
    signature,
    program_id,
    instruction_index,
    inner_instruction_index or -1,
)
```

The current code matches only `program_id` and nonempty venue. That is unsafe for repeated swaps and inner CPI events.

Use:

```python
matches = [
    event for event in decoded_events
    if event.signature == row["signature"]
    and event.program_id == row["program_id"]
    and event.instruction_index == row["instruction_index"]
    and normalized_inner(event.inner_instruction_index)
        == normalized_inner(row["inner_instruction_index"])
]
```

Require exactly one match:

- Zero matches → append `UNKNOWN` with `outcome="NO_MATCHING_RECONSTRUCTED_INSTRUCTION"`.
- More than one → append `UNKNOWN` with `outcome="AMBIGUOUS_RECONSTRUCTION"`.
- Exactly one → append that result, even if its event type is `UNKNOWN`.

This is especially important because inner CPIs are constructed separately at [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:2426).

Pass encoding provenance deliberately. Historical rows contain `source_encoding`; `process_transaction()` currently derives encoding from `rpc_encoding`, defaulting to `"json"`. Add either:

```python
process_transaction(..., instruction_source_encoding=row["source_encoding"])
```

or a dedicated reconstruction function that honors the stored encoding. Never guess if it is `ENCODING_PROVENANCE_UNKNOWN`.

## 1.6 Insert semantics

Every selected raw record gets a terminal append-only row.

```sql
INSERT INTO decoded_versions (
    raw_record_id,
    decoder_version,
    source_encoding,
    code_commit,
    registry_hash,
    idl_sdk_hash,
    decoded_event_type,
    decoded_venue,
    decoded_fields,
    decoded_at,
    supersedes_version_id,
    invalidated,
    invalidation_reason
)
VALUES (
    :raw_id,
    :decoder_version,
    :source_encoding,
    :code_commit,
    :registry_hash,
    :idl_sdk_hash,
    :event_type,
    :venue,
    :decoded_fields,
    :decoded_at,
    :supersedes_id,
    0,
    NULL
)
ON CONFLICT(raw_record_id, decoder_version, registry_hash, code_commit)
DO NOTHING;
```

Check `cursor.rowcount`; only then increment `appended`.

`supersedes_version_id` should be the latest non-invalidated prior interpretation for that raw row:

```sql
SELECT id
FROM decoded_versions
WHERE raw_record_id = :raw_id
  AND invalidated = 0
ORDER BY decoded_at DESC, id DESC
LIMIT 1;
```

This creates lineage without invalidating earlier rows. Prior versions remain valid historical interpretations unless a separate reviewed invalidation process marks them otherwise.

`decoded_fields` should be canonical JSON and include:

```json
{
  "schema": "decoded_fields_v1",
  "outcome": "DECODED",
  "event_type": "SWAP",
  "venue": "pumpswap",
  "pool_address": "...",
  "token_mint": "...",
  "base_mint": "...",
  "quote_mint": "...",
  "swap_direction": "...",
  "base_amount": null,
  "quote_amount": null,
  "price": null,
  "instruction_index": 2,
  "inner_instruction_index": 4
}
```

Do not turn unknown numeric values into zero.

## 1.7 Events that remain `UNKNOWN`

Append an ordinary `decoded_versions` row:

- `decoded_event_type = 'UNKNOWN'`
- `decoded_venue` = registry venue when the program is known; otherwise `''` or a clearly standardized `UNKNOWN`
- `decoded_fields.outcome = 'STILL_UNKNOWN'`
- `decoded_fields.reason` from a closed enum:
  - `UNKNOWN_DISCRIMINATOR`
  - `UNSUPPORTED_PROGRAM_STATUS`
  - `ENCODING_PROVENANCE_UNKNOWN`
  - `MALFORMED_RAW_PAYLOAD`
  - `NO_MATCHING_RECONSTRUCTED_INSTRUCTION`
  - `AMBIGUOUS_RECONSTRUCTION`
  - `DECODER_EXCEPTION`

A decoder exception should normally be recorded as an `UNKNOWN` attempt and counted in `errors`; otherwise reruns cannot distinguish “not attempted” from “attempted and failed.” The only rows that should not receive a version are catastrophic transaction failures such as a database write failure that rolls back the batch.

## 1.8 Transactions and resumption

Process in ID-ordered batches of 250–1,000:

```sql
BEGIN IMMEDIATE;
-- insert decoded_versions rows for one batch
-- insert/update migration progress metadata
COMMIT;
```

Recommended new table:

```sql
CREATE TABLE IF NOT EXISTS migration_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_name TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    registry_hash TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL,
    start_raw_ledger_hash TEXT NOT NULL,
    end_raw_ledger_hash TEXT,
    last_raw_record_id INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0,
    appended INTEGER NOT NULL DEFAULT 0,
    still_unknown INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    detail TEXT
);
```

This is operational metadata, not decoded data. If schema expansion is deferred, write the equivalent JSON manifest atomically outside the DB.

## 1.9 Verification queries

```sql
SELECT COUNT(*) FROM decoded_versions;

SELECT decoded_event_type, decoded_venue, COUNT(*)
FROM decoded_versions
WHERE code_commit = :commit
  AND registry_hash = :registry_hash
GROUP BY decoded_event_type, decoded_venue
ORDER BY COUNT(*) DESC;

SELECT COUNT(*)
FROM decoded_versions
WHERE code_commit = :commit
  AND registry_hash = :registry_hash
  AND decoded_event_type = 'UNKNOWN';

SELECT raw_record_id, COUNT(*)
FROM decoded_versions
WHERE decoder_version = :decoder_version
  AND code_commit = :commit
  AND registry_hash = :registry_hash
GROUP BY raw_record_id
HAVING COUNT(*) > 1;
```

Before and after, verify:

```sql
SELECT COUNT(*), MIN(id), MAX(id),
       SUM(LENGTH(raw_payload)), SUM(LENGTH(raw_payload_hash))
FROM raw_events;
```

Also use the existing `compute_ledger_hash()` at [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:1466). The before/after hash and row count must be identical.

# 2. Acquisition ledger population

## 2.1 Historical limitations

`ingestion_log` contains batch totals and timestamps but no signature or raw-event foreign key. Therefore it cannot retrospectively prove an exact notification-to-signature-to-fetch chain.

Historical population must separate:

- Exact evidence derived from `raw_events`.
- Batch evidence derived from `ingestion_log`.
- Explicitly inferred stages where no direct evidence exists.

Never manufacture exact notification timestamps or claim individual fetch failures from aggregate counters.

## 2.2 Schema hardening

The current ledger at [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:1303) has no deduplication key or batch link. Extend schema v2.2 with:

```sql
ALTER TABLE acquisition_ledger ADD COLUMN ingestion_log_id INTEGER;
ALTER TABLE acquisition_ledger ADD COLUMN raw_record_id INTEGER;
ALTER TABLE acquisition_ledger ADD COLUMN evidence_kind TEXT
    NOT NULL DEFAULT 'DIRECT';
ALTER TABLE acquisition_ledger ADD COLUMN event_key TEXT;
```

SQLite migration code must use the same guarded `PRAGMA table_info` pattern as `migrate_schema_v2_1()`.

Indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_acq_batch
ON acquisition_ledger(ingestion_log_id);

CREATE INDEX IF NOT EXISTS idx_acq_raw_record
ON acquisition_ledger(raw_record_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_acq_event_key
ON acquisition_ledger(event_key)
WHERE event_key IS NOT NULL;
```

`event_key` should be a SHA-256 of canonical fields:

```text
ledger-v1 | source-kind | source-id | stage | signature |
program-id | instruction-index | inner-index | terminal-status
```

This makes the backfill and live instrumentation safely rerunnable.

Define enums/constants:

```python
class AcquisitionStage(str, Enum):
    NOTIFICATION = "NOTIFICATION"
    SIGNATURE_DISCOVERED = "SIGNATURE_DISCOVERED"
    TRANSACTION_FETCH = "TRANSACTION_FETCH"
    DECODE = "DECODE"
    RAW_PERSIST = "RAW_PERSIST"
    BATCH_COMPLETE = "BATCH_COMPLETE"

class EvidenceKind(str, Enum):
    DIRECT = "DIRECT"
    DERIVED = "DERIVED"
    AGGREGATE = "AGGREGATE"
    INFERRED = "INFERRED"
```

## 2.3 Functions to create

In `event_genesis.py`, or preferably a new `src/ascension/acquisition_ledger.py`:

```python
def make_acquisition_event_key(...) -> str: ...
def record_acquisition_stage(conn, *, stage, terminal_status, ...) -> bool: ...
def backfill_batch_ledger(conn, after_ingestion_id=0, limit=1000) -> dict: ...
def backfill_raw_event_ledger(conn, after_raw_id=0, limit=5000) -> dict: ...
def audit_acquisition_pipeline(conn) -> dict: ...
```

Do not put commits inside `record_acquisition_stage()`. The caller should atomically commit ledger records with the associated batch/event operation.

## 2.4 Backfill from `ingestion_log`

One exact aggregate row per historical ingestion batch:

```sql
SELECT
    id, source, batch_id, provider_name,
    started_at, completed_at,
    events_received, events_inserted, events_duplicate,
    events_late_arriving, events_malformed,
    events_unknown_program, decoder_failures,
    status, error, data_origin
FROM ingestion_log
WHERE id > :after_id
ORDER BY id
LIMIT :limit;
```

Insert `BATCH_COMPLETE`:

```sql
INSERT INTO acquisition_ledger (
    stage,
    terminal_status,
    detail,
    recorded_at,
    ingestion_log_id,
    evidence_kind,
    event_key
)
VALUES (
    'BATCH_COMPLETE',
    :status,
    :canonical_detail_json,
    COALESCE(:completed_at, :started_at),
    :ingestion_id,
    'AGGREGATE',
    :event_key
)
ON CONFLICT(event_key) DO NOTHING;
```

`detail` should preserve all counters, source, provider, origin, batch ID, error, and an explicit warning:

```json
{
  "schema": "acquisition_detail_v1",
  "historical_backfill": true,
  "granularity": "BATCH",
  "signature_attribution_available": false,
  "events_received": 12,
  "events_inserted": 10,
  "events_duplicate": 2
}
```

Where `events_received > 0`, an optional aggregate `SIGNATURE_DISCOVERED` row may be added, but its status must be `AGGREGATE_OBSERVED`, not a per-signature success.

Do not spread aggregate failures across arbitrary signatures.

## 2.5 Backfill exact stages from `raw_events`

For every raw event, derive the stages supported by its contents:

```sql
SELECT
    id, program_id, signature, slot,
    instruction_index,
    COALESCE(inner_instruction_index, -1),
    provider_receipt_time,
    ingestion_time,
    event_type,
    venue,
    err,
    raw_payload_hash,
    data_origin
FROM raw_events
WHERE id > :after_id
ORDER BY id
LIMIT :limit;
```

Append:

1. `SIGNATURE_DISCOVERED`
   - status `OBSERVED`
   - evidence `DERIVED`
   - recorded time `provider_receipt_time`
   - detail says it is derived from persisted raw evidence.

2. `TRANSACTION_FETCH`
   - status `FETCHED`
   - evidence `DERIVED`
   - the non-null raw payload proves a transaction was available.
   - recorded time `provider_receipt_time`.

3. `DECODE`
   - status `DECODED` if event type is not `UNKNOWN`
   - status `UNKNOWN` otherwise
   - recorded time `ingestion_time`
   - include venue and original decoder version.

4. `RAW_PERSIST`
   - status `INSERTED`
   - evidence `DIRECT`
   - recorded time `ingestion_time`
   - include `raw_record_id`, `raw_payload_hash`, and origin.

Deduplicate signature/fetch stages at signature level, while decode/persist stages remain instruction-level. Use two keys:

```text
signature-stage: signature + stage
instruction-stage: signature + program + outer_ix + inner_ix + stage
```

A historical `NOTIFICATION` row should only be generated if the source proves a notification mechanism. Existing polling via `getSignaturesForAddress` should use `SIGNATURE_DISCOVERED`, not `NOTIFICATION`.

## 2.6 Live instrumentation

Modify `_poll_program()` in [collector_service.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/collector_service.py:827):

- After each signature is admitted from pagination:
  - `SIGNATURE_DISCOVERED / OBSERVED`
- Before/after `get_transaction()`:
  - `TRANSACTION_FETCH / FETCHED`
  - `TRANSACTION_FETCH / NOT_FOUND`
  - `TRANSACTION_FETCH / RPC_ERROR`
- After `process_transaction()`:
  - one `DECODE / DECODED` or `DECODE / UNKNOWN` per exact instruction
  - if no events: one signature-level `DECODE / NO_REGISTERED_INSTRUCTIONS`
- After ingestion:
  - `RAW_PERSIST / INSERTED`
  - `RAW_PERSIST / DUPLICATE`
  - `RAW_PERSIST / ERROR`

Also modify `EventIngester.begin_batch()` and `complete_batch()` at [event_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/event_genesis.py:1468) to expose the ingestion-log ID and write `BATCH_COMPLETE`.

The comment at collector line 844 already says a failed fetch should be recorded, but no implementation exists.

Important atomicity rule: write `RAW_PERSIST` in the same transaction as the `raw_events` insert. Currently `ingest_event()` commits internally. Refactor to support:

```python
ingest_event(event, commit=False)
record_acquisition_stage(..., commit=False)
conn.commit()
```

For duplicate detection, find the existing raw ID using the same uniqueness identity so the ledger can reference it.

For later WebSocket collection, add `NOTIFICATION` only at the actual callback boundary.

# 3. Free observed-state pilot — design only

The repository’s current Stage 7 outline is at [label_genesis.py](/root/shaheer-project/crypto-alpha-engine/src/ascension/label_genesis.py:1887). The RPC allowlist already includes `programSubscribe`, but `accountSubscribe` is missing and must be added before implementation.

## Objective

Measure whether free Solana account subscriptions provide sufficiently coherent observed state for selected simple venues. Outputs remain `OBSERVED_ACCOUNT_SNAPSHOT`; they must never be represented as exact pre/post transaction state.

## Pilot scope

Start with:

1. Pump.fun bonding curves.
2. PumpSwap pools and SPL vaults.
3. Raydium CPMM only after the first two stabilize.

Exclude CLMM/DLMM/Whirlpool certification because tick/bin/bitmap and dynamic fee state require broader, order-sensitive account-write coverage.

## Proposed components

```text
ObservedStateCollector
├── BaselineLoader        getMultipleAccounts at finalized slot
├── ProgramSubscriber     programSubscribe for owned state accounts
├── AccountSubscriber     accountSubscribe for vault/mint accounts
├── SubscriptionRegistry  account → venue/pool/role
├── StateJournal          append-only raw account notifications
├── CoherenceTracker      slot/root/reconnect/gap metrics
└── VenueReconstructor    venue-specific quote/state reconstruction
```

## Proposed table

```sql
CREATE TABLE observed_account_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_pubkey TEXT NOT NULL,
    owner_program TEXT,
    venue TEXT,
    pool_address TEXT,
    account_role TEXT NOT NULL,
    slot INTEGER NOT NULL,
    commitment TEXT NOT NULL,
    subscription_id INTEGER,
    received_at REAL NOT NULL,
    monotonic_receipt_ns INTEGER NOT NULL,
    lamports INTEGER,
    data_encoding TEXT NOT NULL,
    raw_data TEXT NOT NULL,
    raw_data_hash TEXT NOT NULL,
    reconnect_epoch INTEGER NOT NULL,
    source_method TEXT NOT NULL,
    classification TEXT NOT NULL
        DEFAULT 'OBSERVED_ACCOUNT_SNAPSHOT',
    UNIQUE(account_pubkey, slot, raw_data_hash)
);
```

Never update old account versions.

## Startup sequence

1. Verify genesis hash.
2. Capture finalized/root slot.
3. Load coherent baselines with `getMultipleAccounts`.
4. Register `programSubscribe` with venue-specific data-size/memcmp filters.
5. Discover vault/mint/dynamic accounts from decoded pool state.
6. Register `accountSubscribe` for those accounts.
7. Buffer notifications received during baseline establishment.
8. Apply buffered observations in `(slot, monotonic_receipt_ns)` order.
9. Track root notifications and reconnect epochs.

Because standard subscriptions do not expose a write version, same-slot ordering remains ambiguous and must be measured, not inferred.

## Measurements

Per venue and account role:

- notifications/hour and bytes/hour
- unique accounts
- duplicate notification rate
- reconnect count and duration
- missing-slot intervals
- same-account/same-slot multiple-value rate
- cross-account slot skew for a pool
- baseline-to-first-notification gap
- transaction-to-observed-state latency
- reconstruction success rate
- quote disagreement against deterministic fixture/reference calculations

## Pilot acceptance

A venue may pass “observed-state usable” only if:

- all required account roles are discovered and subscribed;
- reconnect recovery has a deterministic rebaseline procedure;
- no unresolved gaps exist for evaluated windows;
- ambiguous same-slot updates are excluded;
- reconstruction is bit-exact on fixtures;
- live quote comparisons meet a predeclared tolerance;
- measured traffic remains within the free-provider budget.

Passing does not authorize exact-state labels, training, or trading. It only authorizes a venue-specific `OBSERVED_ACCOUNT_SNAPSHOT` input track.

# 4. PumpSwap canary promotion criteria

Although implementation was requested only for items 1–2, the migration should emit the evidence needed for later PumpSwap promotion.

Promote `pumpswap_amm_v1` from `CANARY` to `ACTIVE` only after:

- Official program ID remains `pAMMBay...`; no `PSwapMd...` contamination.
- Minimum observation duration, preferably seven consecutive days.
- Minimum volume, with 6,164 current events satisfying the initial count floor but not duration/quality alone.
- Golden fixtures cover pool creation, both swap directions, deposit, withdraw, failed, malformed, outer, inner CPI, v0/ALT, WSOL, and Token-2022 cases.
- Re-decode and live decode agree for identical raw instructions.
- Unknown rate is below a declared threshold overall and per discriminator.
- No ambiguous instruction matching.
- Account-derived pool/mint identities agree with transaction balances.
- Duplicate rate and reconnect/gap metrics are within collector limits.
- Registry hash, decoder commit, activation slot/time, and ledger cursor are frozen in a promotion artifact.
- Manual review samples transactions across slots, outer instructions, and CPI paths.
- Raw immutability proof passes before and after all certification analysis.

Promotion changes only registry/certification status. It does not authorize model training or trading.

# 5. Required tests

## Re-decode unit tests

Add to `tests/test_label_genesis.py`:

- Outer instruction exact-path matching.
- Inner CPI exact-path matching.
- Two same-program instructions in one transaction do not cross-match.
- Still-unknown creates a version row.
- Malformed JSON creates terminal `UNKNOWN/DECODER_EXCEPTION` evidence without changing raw data.
- Unknown encoding remains quarantined.
- Identical run is idempotent.
- New commit creates a new version.
- New registry hash creates a new version.
- Older version sets `supersedes_version_id`.
- Invalidated old version remains present.
- `cursor.rowcount` controls appended count.
- Canonical registry hash is stable across dictionary insertion order.
- A registry-relevant change changes the hash.
- Placeholder provenance is rejected.
- Batch rollback leaves neither partial versions nor progress advancement.
- Resume from `last_raw_record_id` produces the same final result.
- Before/after raw ledger hash and count are identical.

## Acquisition-ledger tests

Add to `tests/test_event_genesis.py` and `tests/test_collector_service.py`:

- Schema migration is idempotent.
- Backfill rerun inserts no duplicates.
- Historical batch counters are preserved exactly.
- Aggregate evidence is not attributed to an invented signature.
- One signature with multiple instructions has one signature/fetch stage and multiple decode/persist stages.
- Outer and inner instruction keys differ.
- UNKNOWN decode gets terminal `UNKNOWN`.
- `getTransaction -> None` records `NOT_FOUND`.
- RPC exception records `RPC_ERROR`.
- Empty decoder output records `NO_REGISTERED_INSTRUCTIONS`.
- Duplicate raw event records `RAW_PERSIST/DUPLICATE`.
- Raw insert and ledger persist roll back together.
- No `NOTIFICATION` is generated for polling-only history.
- Canonical detail JSON and event-key generation are deterministic.

## Safety regression tests

Every maintenance entry point must assert:

```python
assert LIVE_TRADING_ENABLED is False
assert REAL_ORDERS_SUBMITTED == 0
assert WALLET_TRANSACTIONS_SIGNED == 0
assert WITHDRAWAL_CAPABILITY is False
```

Additionally:

- Monkeypatch/deny `UPDATE raw_events` and `DELETE FROM raw_events`.
- Verify no training module is imported or invoked.
- Verify collector RPC methods remain read-only.
- Verify migration commands contain no wallet loading, signing, or submission path.
- Run the full existing 560-test suite plus new tests.

# 6. Recommended rollout order

1. Commit the implementation so `code_commit` is trustworthy.
2. Stop or coordinate the collector briefly for a clean DB backup and preflight snapshot.
3. Add provenance helpers, exact matching, tests, and dry-run reporting.
4. Dry-run against a copied database.
5. Run a small execute canary, e.g. 100–500 UNKNOWN rows.
6. Verify decoded counts, UNKNOWN reasons, instruction matching, and raw hash.
7. Resume through all 48,706 candidates in bounded transactions.
8. Add acquisition-ledger schema hardening and historical backfill.
9. Instrument the live collector for exact future stages.
10. Run reconciliation reports and PumpSwap-specific certification queries.
11. Design-review Stage 7; do not implement or activate it in this phase.

At every stage: `raw_events` remains immutable; no model training is authorized; no wallet is loaded; no transaction is signed; no order is submitted.