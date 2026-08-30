# TITAN B5 Correction Report

**Date**: 2026-08-30  
**Verdict**: **NO-GO** — Correction Sections 1-7 complete, 8-9 pending  
**Commit**: 4e1731d (titan-repair branch)  
**Tests**: 744 passed, 0 failed  

---

## Why the Original GO Was Withdrawn

The original B5 GO verdict was **invalid** due to a critical error:

**MT5 margin mode mapping was inverted.**
- Had: `0 = HEDGING` (WRONG)
- Correct: `0 = NETTING, 1 = EXCHANGE, 2 = HEDGING`

This means every margin-mode check in the original RC1 was testing the wrong condition. A NETTING account would have been accepted as HEDGING — completely unsafe.

Additionally, the 4 new B5 modules (ownership_identity, integrity_verifier, migration_manager, replay_engine) were **never wired into the production entrypoint**. They existed as standalone files with tests but no code path in TitanIntegration actually called them.

Tag `titan-rc1` has been **deleted** and replaced with `titan-rc1-SUPERSEDED-UNTRUSTED`.

---

## Correction Sections Completed (7 of 9)

### Section 1: Margin Mode Fix
- `_MARGIN_MODE_MAP` corrected: `{0: NETTING, 1: EXCHANGE, 2: HEDGING}`
- `detect_margin_mode()` rejects bool, float, string, negative, future values
- `UNSUPPORTED_MARGIN_MODE` added to HaltReason enum
- All test DEMO_ACCOUNT dicts updated: `"margin_mode": 2`
- ReplayAuditBroker fixed from `margin_mode: 0` to `margin_mode: 2`

### Section 2: Module Wiring into Production Entrypoint
- All 4 B5 modules now imported and call-sited in `titan_integration.py`
- TitanIntegration wires **18 modules** (was 14)
- OwnershipVerifier, PendingOrderStore, MigrationManager created at init
- StartupIntegrityVerifier runs at init when manifest provided
- Gateway verifies margin mode on every mutation verb
- ShadowId rejected at all production/mutation interfaces
- 17 new tests prove import, call-site, and runtime-trace evidence

### Section 3: Reproducible Artifact (including MANIFEST.json)
- `SOURCE_DATE_EPOCH` (or git commit time) used for all timestamps
- File mtimes normalised to epoch before hashing
- MANIFEST.json is now **byte-identical** between builds
- Verified: two builds produce identical MANIFEST.json

### Section 4: Runtime SBOM
- CycloneDX 1.5 format SBOM.json included in artifact
- Lists: numpy 2.4.6, pandas 3.0.5, scikit-learn 1.9.0, scipy 1.18.0, joblib 1.5.3
- Lists: CPython 3.12.3, SQLite 3.45.1, Linux 6.8.0, x86_64
- Model/scaler inventory: NONE (no model files in artifact)

### Section 5: Packaged Entrypoint Execution
- Artifact contains all B5 modules (verified)
- TitanIntegration initialises from packaged source with FakeBroker
- Works from neutral (non-repo) working directory
- SBOM.json present and valid in artifact

### Section 6: Mutation-Producing Replay (All 5 Verbs)
All 5 mutation verbs exercised through the **full production path**:
1. **OPEN**: pre_execute_check → execute_order → arbiter → FakeBroker → record_fill → ledger
2. **MODIFY_SLTP**: position_state → request_sl_change → FakeBroker → confirm_sl_change
3. **REDUCE**: ownership_verifier.verify_for_mutation(verb=REDUCE)
4. **CLOSE**: ownership_verifier.verify_for_mutation(verb=CLOSE)
5. **CANCEL**: pending_order_store.register_order → request_cancel → confirm_cancel

Also tested: durable halt blocks all verbs, wrong account blocks cancel.

### Section 7: Migration Fault Certification
- **Crash between migrations**: Restartable — new manager picks up from where it stopped
- **Corrupt WAL**: Recovery via coherent SQLite backup API
- **Checksum mismatch**: Modified migration SQL after application detected and halted
- **Future schema version**: Unknown version 999 detected and halted
- **Serialization lock**: Concurrent migrations blocked until lock released
- **Fresh creation**: All tables created from empty DB
- **Idempotent**: Running migrations twice is safe

---

## Sections Pending

### Section 8: Real 24-Hour Soak
- Requires real wall-clock time (not simulated)
- Mutation-producing FakeBroker, hash-chained heartbeats, resource measurement
- Cannot be completed in a single session

### Section 9: Final Verdict
- Blocked on Section 8
- Will be either `SOAK_IN_PROGRESS` or `NO-GO`
- Never `GO` until 24h soak completes with zero violations

---

## Test Growth

| Phase | Tests | Delta |
|-------|-------|-------|
| A-D   | 138   | —     |
| B     | 222   | +84   |
| B3    | 300   | +78   |
| B4    | 405   | +105  |
| B5    | 657   | +252  |
| B5 Correction | **744** | **+87** |

## Files Changed (15 files, +1809/-75 lines)

- `ownership_identity.py` — Corrected margin mode mapping + detection
- `titan_integration.py` — Wired B5 modules (18 modules total)
- `execution_safety.py` — Added UNSUPPORTED_MARGIN_MODE halt reason
- `replay_engine.py` — Fixed margin_mode from 0 to 2
- `build_rc1.py` — SOURCE_DATE_EPOCH, SBOM, normalised mtimes
- `test_b5_verification.py` — 17 new wiring evidence tests
- `test_replay_soak.py` — 8 new mutation-producing replay tests
- `test_artifact_integrity.py` — 12 new entrypoint + migration fault tests
- `test_ownership_certification.py` — Rewritten margin mode tests
- 6 test files — Added `margin_mode: 2` to DEMO_ACCOUNT dicts
