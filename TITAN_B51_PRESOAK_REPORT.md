# TITAN B5.1 Pre-Soak Closure Report

**Date**: 2026-08-30  
**Verdict**: **NO-GO — REPRODUCIBLE RC CERTIFICATION FAILED** (Sections 1-6 complete, 7-8 pending)  
**Commit**: f4579c5 (titan-repair branch)  
**Tests**: 783 passed, 0 failed (3 consecutive runs)  
**Archive SHA-256**: `56c2f4cb3feee0402a90cfee007db998defad3e3d538070435d774a4285a60e2`  

---

## Pre-Soak Sections Completed (6 of 8)

### Section 1: Tag Incident Preservation ✅
- `titan-rc1` (annotated, object `d523eab`) DELETED
- Replaced with `titan-rc1-SUPERSEDED-UNTRUSTED` (object `496d909`)
- Both peel to commit `7eb52e0`
- Original object unreachable but recoverable via `git cat-file -p d523eab...`
- No destructive recovery performed
- `TAG_INCIDENT_RECORD.md` committed
- Old RC1 identity permanently untrusted — future tag: `titan-rc2`

### Section 2: Archive-Level Reproducibility ✅
- Two tar.gz archives built from clean checkouts at different wall-clock times
- **Identical SHA-256**: `56c2f4cb3feee0402a90cfee007db998defad3e3d538070435d774a4285a60e2`
- **Identical byte size**: 334,331 bytes
- **File count**: 107 files
- **MANIFEST.json**: byte-identical between builds
- **SBOM.json**: byte-identical between builds
- Source commit: `b316c274066b`
- Source tree: `7961ce9d91a9`
- SOURCE_DATE_EPOCH: `1788088884`
- Build command: `python3 build_rc1.py`
- Deterministic gzip (mtime=0, no filename in header)
- Deterministic tar (sorted members, UID/GID=0, normalized mtimes)

### Section 3: Gold Model Resolution ✅
- **Gold is EXPLICITLY EXCLUDED** from the certified engine
- No `champion_model.pkl` or `champion_scaler.pkl` in artifact (0 model files)
- `gold_enabled = False` (hard-disabled in TitanIntegration)
- Status reports `gold_model_status = "EXCLUDED_NO_MODEL"`
- Gold uses `GoldShadowStateStore` (SHADOW_NO_SEND) — records intents only
- Multi-asset orchestrator excludes Gold from its universe
- Gold shadow mutations: exactly 0
- This artifact does NOT certify a complete eight-instrument engine — Gold is excluded

### Section 4: Packaged Entrypoint Evidence ✅
- Service template ExecStart: `/usr/bin/python3 -m trading_research.intraday.runner`
- All 18 modules produce trace evidence at init
- Runner and multi-asset orchestrator modules importable
- All 4 B5 modules present in artifact
- 783 tests run 3 consecutive times against packaged source — all pass

### Section 5: Five-Verb FakeBroker Transport ✅
All 5 mutation verbs exercised through production paths:

| Verb | Path | Evidence |
|------|------|----------|
| OPEN | pre_execute_check → execute_order → arbiter → FakeBroker → ledger | `ExecutionState.ACKNOWLEDGED`, broker_ticket > 0 |
| MODIFY_SLTP | position_state.register → modify_sl → send_fn → confirmed | `accepted=True`, `confirmed_sl` matches |
| CLOSE | create_position_identity → register_identity → verify_for_mutation(CLOSE) | ownership verified |
| REDUCE | create_position_identity → register_identity → verify_for_mutation(REDUCE) | ownership verified |
| CANCEL | register_order → request_cancel → confirm_cancel | order no longer active |

Safety verifications:
- ✅ Durable halt blocks OPEN at execute_order
- ✅ Wrong account hash blocks CANCEL
- ✅ ShadowId rejected at mutation boundary (before is_shadow_ticket)
- ✅ Gold shadow ticket rejected at gateway
- ✅ Gold shadow mutations: exactly 0
- ✅ Wrong margin mode (NETTING) causes durable halt

### Section 6: Extended Migration Fault Certification ✅
15 fault scenarios tested:

| Fault | Result |
|-------|--------|
| Process death before commit | No partial state, clean restart |
| Process death after partial migration | Restartable from v1 to v3 |
| Corrupt SQLite header | Detected, migration fails |
| Corrupt database page | Detected on integrity_check |
| Missing WAL | Graceful recovery, migration succeeds |
| Truncated WAL | Graceful handling, no crash |
| Corrupt WAL | Graceful handling, no crash |
| Backup while WAL active | SQLite backup API succeeds |
| Restore and migrate backup | Data preserved, migration continues |
| Restart after failure | Clean migration after bad SQL |
| Multiple processes (5 threads) | At least one succeeds, no crashes |
| Halt preservation | Halt records survive restart |
| Intent preservation | Pending orders survive restart |
| Gold shadow preservation | Shadow intents survive restart |
| Checksum mismatch | (from B5 Section 7) Detected and halted |

---

## Sections Pending

### Section 7: Real 24-Hour Soak
- Pin archive by SHA-256 in read-only content-addressed directory
- Separate DBs/logs/locks/PIDs/ports
- Network-denied environment with FakeBroker
- SOAK_LIMITS.json sealed before start
- Hash-chained heartbeats every minute
- Auto-invalidation on any violation
- Must cross 00:00 UTC
- Cannot be simulated — requires real wall-clock time

### Section 8: Verdict
- Blocked on Section 7
- Will be `SOAK_IN_PROGRESS` or `NO-GO`
- Never `GO` until 24h soak completes with zero violations

---

## Test Growth

| Phase | Tests | Delta |
|-------|-------|-------|
| A-D | 138 | — |
| B | 222 | +84 |
| B3 | 300 | +78 |
| B4 | 405 | +105 |
| B5 | 657 | +252 |
| B5 Correction | 744 | +87 |
| **B5.1 Pre-Soak** | **783** | **+39** |

## Files Changed

- `build_rc1.py` — Archive builder, gzip determinism, archive-level reproducibility verification
- `titan_integration.py` — Gold hard-disable (`gold_enabled=False`), ShadowId check reordered before is_shadow_ticket
- `tests/titan/test_b51_certification.py` — 39 new tests across 6 sections
- `TAG_INCIDENT_RECORD.md` — Tag deletion/replacement documentation
