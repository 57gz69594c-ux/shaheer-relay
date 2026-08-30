# ASCENSION Event Genesis — Provider Activation & 24H Certification

**Date**: 2026-08-30
**Commit**: ee91abd (crypto-alpha-engine)
**Continues from**: 6c85e10 (Event Genesis raw infrastructure)

## Verdict

**SECURE_CREDENTIAL_BLOCKED**

All infrastructure is built, tested, and deployed. Awaiting Helius API key installation.

## What Was Built

### Stage 1 — Secure Credential Bootstrap
- `/usr/local/sbin/ascension-set-helius-key` — Interactive installer (reads silently from /dev/tty)
  - Root-only, umask 077, atomic write via temp+mv, cleanup trap
  - Validates key format (20–128 chars, alphanumeric/dash/underscore)
  - Sets `root:root 0600` on `/etc/ascension/event-genesis.env`
- `/etc/ascension/` directory (mode 700, outside all repositories)
- systemd EnvironmentFile injection — no secrets in ExecStart or unit files

### Collector Service (src/ascension/collector_service.py — 1,103 lines)
- **SafeRPCClient** — Enforces frozen read-only RPC method allowlist (50+ methods)
  - Explicit denylist: `sendTransaction`, `simulateTransaction`, `requestAirdrop`
  - Any unlisted method raises `SecurityError`
- **Redaction layer** — `redact_url()`, `redact_env()`, `redact_exception()`, `RedactingFormatter`
  - Secrets never appear in logs, status output, artifacts, or exception traces
- **Provider Capability Gate** — Verifies:
  - Solana mainnet-beta genesis hash
  - HTTP connectivity and slot queries
  - Pump.fun + PumpSwap accessibility
  - Transaction data completeness (slot, blockTime, logs, innerInstructions, balances, tokenBalances)
  - Secret absence from process arguments
- **EventCollector** — Main polling loop with rate limiting (10 RPC/s)
  - Fetches signatures for P0 programs, retrieves full transactions
  - Watermark-based dedup and progress tracking
  - Health recording, manifest sealing
- **CertificationState** — 24h genuine elapsed time tracking
  - Lag percentiles (p50/p95/p99), coverage ratio, gap tracking
  - Restart recovery (persist/restore across process death)
  - Six directive-specified verdicts

### systemd Integration
- `ascension-event-genesis.service` — Main collector daemon
  - EnvironmentFile credential injection
  - Security hardening (NoNewPrivileges, ProtectSystem=strict, PrivateTmp, etc.)
  - Restart=on-failure with backoff
  - Memory/CPU limits
- `ascension-certification-check.timer` — 15-minute periodic status check
- `/usr/local/bin/ascension-collector-status` — CLI status (no secrets shown)

### Tests: 387 passed (294 existing + 93 new), 0 failed

93 new adversarial tests across 14 test classes:
- **Secret leakage** (14 tests): URL redaction, env redaction, exception redaction, log formatter, source code scan, activation file, cert state file, status output
- **RPC method enforcement** (12 tests): Denylist/allowlist integrity, SafeRPCClient blocking, no wallet imports
- **Restart recovery** (3 tests): Cert state round-trip, fresh start, activation immutability
- **Backfill exclusion** (4 tests): Data origin tags, canary ineligibility
- **Certification timing** (11 tests): 24h duration, verdicts at each threshold, progress capping
- **Reconnect behavior** (4 tests): 429s, failures, disconnects, reconnects tracked
- **Lag tracking** (5 tests): Percentiles, bounded samples, coverage ratio
- **Provider capability gate** (4 tests): All-pass, genesis mismatch, no-slot, secret in args
- **Credit usage** (3 tests): Rate limiter, fetch count, signature count
- **systemd validation** (7 tests): File existence, EnvironmentFile, no secrets in ExecStart, hardening
- **Secure installer** (10 tests): Existence, executable, tty read, root check, umask, atomic write, trap, validation, permissions, env dir
- **Data quality** (5 tests): Duplicates, malformed, failed, decode failures, gaps
- **Verdicts** (3 tests): All 6 states reachable, safety block complete

## Activation

To install the Helius key and start collection:

```
sudo /usr/local/sbin/ascension-set-helius-key
sudo systemctl start ascension-event-genesis
ascension-collector-status
```

## Safety

| Field | Value |
|-------|-------|
| MARKET_DATA_PROVIDER_CREDENTIAL_CONFIGURED | FALSE (awaiting installation) |
| TRADING_CREDENTIALS_CONFIGURED | FALSE |
| WALLET_KEYS_LOADED | 0 |
| TRANSACTIONS_SIGNED | 0 |
| TRANSACTIONS_SUBMITTED | 0 |
| LIVE_TRADING_ENABLED | FALSE |

## Verdicts (from directive)

| State | Meaning |
|-------|---------|
| **SECURE_CREDENTIAL_BLOCKED** ← CURRENT | No Helius key installed |
| PROVIDER_CAPABILITY_FAILED | Genesis/connectivity/data check failed |
| COLLECTION_ACTIVE_CERTIFICATION_RUNNING | Collecting, 24h timer not elapsed |
| COLLECTION_ACTIVE_COVERAGE_FAILED | 24h elapsed, unrepaired gaps |
| COLLECTION_ACTIVE_COVERAGE_PASSED_TEMPORAL_MATURITY_PENDING | 24h passed, no certified snapshots yet |
| COLLECTION_ACTIVE_TEMPORAL_PROOF_PASSED | Full certification complete |
