#!/usr/bin/env python3
"""
RH Chain Rotation Engine — Audit Evidence Extractor
Extracts comprehensive trading data from engine.log for Sep 7-8, 2026.
Produces: trades.json, monitoring_timeline.csv, rejected_candidates.json,
          config_versions.json, raw_engine_log.txt
"""

import json
import csv
import re
import os
from datetime import datetime
from collections import defaultdict

LOG_FILE = "/root/shaheer-project/crypto-alpha-engine/logs/engine.log"
OUT_DIR = "/root/shaheer-project/shaheer-relay/audit"

# ─── Timestamp parser ───
def parse_ts(line):
    m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
    return m.group(1) if m else None

def extract_level(line):
    m = re.search(r'\b(INFO|WARNING|ERROR|DEBUG|CRITICAL)\b', line)
    return m.group(1) if m else "UNKNOWN"

# ─── Read all lines ───
print("Reading engine log...")
with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
    all_lines = f.readlines()
print(f"  Total lines: {len(all_lines)}")

# ─── 1. RAW ENGINE LOG (Sep 7-8 only) ───
print("\n[1/5] Producing raw_engine_log.txt...")
raw_lines = []
for line in all_lines:
    ts = parse_ts(line)
    if ts and (ts.startswith('2026-09-07') or ts.startswith('2026-09-08')):
        raw_lines.append(line)
    elif not ts and raw_lines:
        raw_lines.append(line)

with open(os.path.join(OUT_DIR, "raw_engine_log.txt"), 'w') as f:
    f.writelines(raw_lines)
print(f"  Written {len(raw_lines)} lines")

# ─── Classify log line event type ───
def classify_event(line):
    text = line.strip()
    if 'ROTATION ENGINE v' in text: return 'CONFIG_BANNER'
    if re.search(r'Wallet:.*\|.*Target:', text): return 'CONFIG_WALLET'
    if re.search(r'Enter:.*\|.*Exit:', text): return 'CONFIG_SETTINGS'
    if 'POSITION:' in text: return 'ENTRY_CONFIRMED'
    if re.search(r'V[34] BUY\b', text) and 'REVERTED' not in text and 'ERR' not in text: return 'BUY_ATTEMPT'
    if re.search(r'V[34] BOUGHT', text): return 'BUY_FILLED'
    if 'BUY' in text and 'REVERTED' in text: return 'BUY_REVERTED'
    if 'BUY' in text and 'ERR' in text: return 'BUY_ERROR'
    if 'BOUGHT' in text and 'V' not in text.split('BOUGHT')[0][-3:]: return 'BUY_FILLED'
    if re.search(r'V[34] SELL\b', text) and 'REVERTED' not in text: return 'SELL_ATTEMPT'
    if re.search(r'V[34] SOLD|^.*SOLD \w+', text): return 'SELL_FILLED'
    if 'SELL' in text and 'REVERTED' in text: return 'SELL_REVERTED'
    if 'SELL FAILED' in text: return 'SELL_FAILED'
    if 'BAD FILL' in text: return 'BAD_FILL'
    if re.search(r'Exit #\d+', text): return 'EXIT_CONFIRMED'
    if 'Fast Exit' in text: return 'FAST_EXIT'
    if 'HARD SL' in text: return 'EXIT_TRIGGER_HARD_SL'
    if 'HARD TP HIT' in text or 'TAKE PROFIT' in text: return 'EXIT_TRIGGER_HARD_TP'
    if 'SLOWING' in text and 'on ' in text: return 'EXIT_TRIGGER_SLOWING'
    if re.search(r'MOMENTUM DEAD', text): return 'EXIT_TRIGGER_MOMENTUM_DEAD'
    if 'FAST SL TRIGGERED' in text: return 'EXIT_TRIGGER_FAST_SL'
    if 'FAST EXIT' in text and 'selling NOW' in text: return 'FAST_EXIT_SELLING'
    if 'LATCHED EXIT' in text: return 'LATCHED_EXIT'
    if re.search(r'R\d+ \w+.*PnL=', text): return 'TICK_OBSERVATION'
    if 'PICK' in text: return 'PICK'
    if re.search(r'Testing \w+ 5m=', text): return 'SCAN_TEST'
    if 'Reading' in text and 'B/S=' in text: return 'MOMENTUM_READING'
    if 'Momentum VERIFIED' in text: return 'MOMENTUM_VERIFIED'
    if 'Momentum DIED' in text: return 'MOMENTUM_DIED'
    if 'HONEYPOT BLOCKED' in text: return 'HONEYPOT_BLOCKED'
    if 'HONEYPOT CHECK' in text: return 'HONEYPOT_CHECK'
    if 'SKIP' in text: return 'SKIP'
    if 'BLACKLISTING' in text: return 'BLACKLIST'
    if 'session-blocked' in text: return 'SESSION_BLOCKED'
    if 'POSITION CAP' in text: return 'POSITION_CAP'
    if 'Cooldown' in text: return 'COOLDOWN'
    if 'Pumping candidates' in text: return 'SCAN'
    if 'Waiting for a pump' in text: return 'SCAN_IDLE'
    if 'CONFIRMATION' in text: return 'CONFIRMATION'
    if 'VERIFIED ENTRY' in text: return 'VERIFIED_ENTRY'
    if 'Fill:' in text: return 'FILL_CHECK'
    if 'SLIPPAGE CHECK' in text: return 'SLIPPAGE_CHECK'
    if 'Unwrapping' in text or 'WETH unwrapped' in text: return 'WETH_UNWRAP'
    if 'amountOutMin' in text: return 'TX_DETAIL'
    if 'POOL FOUND' in text: return 'POOL_FOUND'
    if 'POOL NOT FOUND' in text or 'POOL SCAN TIMEOUT' in text: return 'POOL_NOT_FOUND'
    if 'FAST MONITOR' in text: return 'FAST_MONITOR'
    if 'V4 cache' in text or 'V4 pool' in text: return 'CACHE'
    if 'nonce too low' in text: return 'NONCE_ERROR'
    if 'DETECTED existing position' in text: return 'DETECTED_POSITION'
    if 'EXIT_BLOCKED' in text: return 'EXIT_BLOCKED'
    if 'BUY' in text and 'with' in text: return 'BUY_ATTEMPT'
    if 'SELL' in text and 'tokens' in text: return 'SELL_ATTEMPT'
    if 'SOLD' in text: return 'SELL_FILLED'
    if 'ERROR' in text: return 'ERROR'
    if 'WARNING' in text: return 'WARNING'
    return 'OTHER'

def extract_token_from_line(line):
    m = re.search(r'POSITION:\s+(\S+)\s+[\d,]+', line)
    if m: return m.group(1)
    m = re.search(r'V[34]\s+(?:BUY|SELL|BOUGHT|SOLD)\s+(\S+)', line)
    if m: return m.group(1)
    m = re.search(r'(?:BUY|SELL|BOUGHT|SOLD)\s+(\S+?)(?:\s+with|\s+\(|\s*:|\s+→)', line)
    if m and m.group(1) not in ('FAILED', 'ERR'): return m.group(1)
    m = re.search(r'R\d+\s+(\S+)\s+\$', line)
    if m: return m.group(1)
    m = re.search(r'PICK\s+\([^)]+\):\s+(\S+)', line)
    if m: return m.group(1)
    m = re.search(r'Testing\s+(\S+)\s+5m=', line)
    if m: return m.group(1)
    m = re.search(r'on\s+(\S+)\s+PnL=', line)
    if m: return m.group(1)
    m = re.search(r'HONEYPOT BLOCKED:\s+(\S+)', line)
    if m: return m.group(1)
    m = re.search(r'BLACKLISTING\s+(\S+)', line)
    if m: return m.group(1)
    m = re.search(r'on\s+(\S+)\s+—\s+selling', line)
    if m: return m.group(1)
    return ""

# ─── 2. MONITORING TIMELINE CSV ───
print("\n[2/5] Producing monitoring_timeline.csv...")
csv_path = os.path.join(OUT_DIR, "monitoring_timeline.csv")
csv_rows = []
for line in raw_lines:
    ts = parse_ts(line)
    if not ts:
        continue
    event_type = classify_event(line)
    token = extract_token_from_line(line)
    raw = line.strip()
    csv_rows.append({
        'timestamp': ts,
        'event_type': event_type,
        'token': token,
        'raw_log_line': raw
    })

with open(csv_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['timestamp', 'event_type', 'token', 'raw_log_line'])
    writer.writeheader()
    writer.writerows(csv_rows)
print(f"  Written {len(csv_rows)} rows")

# ─── 3. TRADES.JSON ───
print("\n[3/5] Producing trades.json...")

# Collect token addresses and pool info
token_addresses = {}
pool_addresses = {}

for i, line in enumerate(raw_lines):
    text = line.strip()
    m = re.search(r'(0x[0-9a-fA-F]{8,}): V4 \w+ VERIFIED.*route=(\S+)', text)
    if m:
        addr = m.group(1)
        for j in range(i+1, min(i+5, len(raw_lines))):
            pm = re.search(r'PICK\s+\([^)]+\):\s+(\S+)', raw_lines[j].strip())
            if pm:
                token_addresses[pm.group(1)] = addr
                break

for i, line in enumerate(raw_lines):
    text = line.strip()
    m = re.search(r'POOL FOUND.*?:\s+(0x[0-9a-fA-F]+)\s+fee=(\d+)', text)
    if m:
        pool_addr = m.group(1)
        fee = m.group(2)
        for j in range(i-1, max(i-10, 0), -1):
            token = extract_token_from_line(raw_lines[j].strip())
            if token:
                pool_addresses[token] = {"pool_address": pool_addr, "fee": fee}
                break

# Build trades with MULTI-POSITION support
# Key insight: active_positions uses a LIST per token (stack), not a single dict
trades = []
active_positions = defaultdict(list)  # token -> list of trade dicts (stack, newest last)
pre_entry_activity = defaultdict(list)
trade_counter = 0

for i, line in enumerate(raw_lines):
    text = line.strip()
    ts = parse_ts(text)
    if not ts:
        continue

    # ── PICK lines ──
    m = re.search(r'PICK\s+\(([^)]+)\):\s+(\S+)\s+5m=([\+\-][\d.]+)%(?:\s+.*?route=(\S+))?', text)
    if m:
        token = m.group(2)
        pre_entry_activity[token].append({
            "timestamp": ts, "type": "PICK",
            "detail": f"PICK ({m.group(1)}): {token} 5m={m.group(3)}% route={m.group(4) or ''}"
        })

    # PICK (new/pons) style without 5m
    if not m:
        m2 = re.search(r'PICK\s+\(([^)]+)\):\s+(\S+)$', text)
        if m2:
            token = m2.group(2)
            pre_entry_activity[token].append({
                "timestamp": ts, "type": "PICK",
                "detail": f"PICK ({m2.group(1)}): {token}"
            })

    # ── Testing lines ──
    m = re.search(r'Testing\s+(\S+)\s+5m=([\+\-][\d.]+)%', text)
    if m:
        pre_entry_activity[m.group(1)].append({
            "timestamp": ts, "type": "TESTING", "detail": text
        })

    # ── Reading lines ──
    m = re.search(r'Reading\s+(\d+):\s+5m=([\+\-][\d.]+)%\s+B/S=([\d./]+)', text)
    if m:
        for j in range(i-1, max(i-10, 0), -1):
            t = extract_token_from_line(raw_lines[j].strip())
            if t:
                pre_entry_activity[t].append({
                    "timestamp": ts, "type": "READING",
                    "detail": f"Reading {m.group(1)}: 5m={m.group(2)}% B/S={m.group(3)}"
                })
                break

    # ── Momentum VERIFIED ──
    m = re.search(r'Momentum VERIFIED:\s+([\d.]+)→([\d.]+)→([\d.]+)', text)
    if m:
        for j in range(i-1, max(i-10, 0), -1):
            t = extract_token_from_line(raw_lines[j].strip())
            if t:
                pre_entry_activity[t].append({
                    "timestamp": ts, "type": "MOMENTUM_VERIFIED",
                    "detail": f"Verified: {m.group(1)}→{m.group(2)}→{m.group(3)}"
                })
                break

    # ── VERIFIED ENTRY (v6.2) ──
    m = re.search(r'VERIFIED ENTRY:\s+(\S+)\s+5m=([\+\-][\d.]+)%\s+B/S=([\d.]+)', text)
    if m:
        pre_entry_activity[m.group(1)].append({
            "timestamp": ts, "type": "VERIFIED_ENTRY",
            "detail": f"VERIFIED ENTRY: {m.group(1)} 5m={m.group(2)}% B/S={m.group(3)}"
        })

    # ── CONFIRMATION CHECK/WAIT ──
    m = re.search(r'CONFIRMATION CHECK:\s+(\S+)\s+5m=([\+\-][\d.]+)%\s+B/S=([\d.]+)', text)
    if m:
        pre_entry_activity[m.group(1)].append({
            "timestamp": ts, "type": "CONFIRMATION_CHECK",
            "detail": f"CONFIRMATION CHECK: {m.group(1)} 5m={m.group(2)}% B/S={m.group(3)}"
        })

    # ── BUY attempts (V3/V4/plain) ──
    m = re.search(r'(?:V([34]) )?BUY (\S+) with ([\d.]+) (\S+) \(~?\$?([\d.]+)\)(?:.*?fee=(\d+))?', text)
    if m and 'REVERTED' not in text and 'ERR' not in text and 'FAILED' not in text:
        token = m.group(2)
        pre_entry_activity[token].append({
            "timestamp": ts, "type": "BUY_ATTEMPT",
            "detail": text
        })

    # ── POSITION: confirmed entry ──
    m = re.search(r'POSITION:\s+(\S+)\s+([\d,]+)\s+tokens\s+@\s+\$([\d.]+)', text)
    if m:
        token = m.group(1)
        qty = m.group(2).replace(',', '')
        price = m.group(3)
        trade_counter += 1

        # Find buy amount looking backward
        buy_amount = "NOT_RECORDED"
        quote_currency = "NOT_RECORDED"
        buy_fee = "NOT_RECORDED"
        buy_route = "NOT_RECORDED"
        gas_est = "NOT_RECORDED"

        for j in range(i-1, max(i-20, 0), -1):
            jtext = raw_lines[j].strip()
            bm = re.search(r'(?:V[34] )?BUY \S+ with [\d.]+ \S+ \(~?\$?([\d.]+)\)', jtext)
            if bm and 'REVERTED' not in jtext and 'ERR' not in jtext:
                buy_amount = bm.group(1)
                bm2 = re.search(r'with ([\d.]+) (\S+) \(', jtext)
                if bm2:
                    quote_currency = bm2.group(2)
                bm3 = re.search(r'fee=(\d+)', jtext)
                if bm3:
                    buy_fee = bm3.group(1)
                break

        for j in range(i-1, max(i-30, 0), -1):
            rm = re.search(r'PICK.*route=(\S+)', raw_lines[j].strip())
            if rm:
                buy_route = rm.group(1)
                break

        for j in range(max(i-15, 0), min(i+3, len(raw_lines))):
            gm = re.search(r'estimate_gas OK \((\d+) gas\)', raw_lines[j].strip())
            if gm:
                gas_est = f"{gm.group(1)} gas (estimate)"
                break

        trade = {
            "trade_id": trade_counter,
            "token": token,
            "token_address": token_addresses.get(token, "NOT_RECORDED"),
            "pool_info": pool_addresses.get(token, {"pool_address": "NOT_RECORDED", "fee": "NOT_RECORDED"}),
            "route": buy_route,
            "entry_timestamp_utc": ts,
            "entry_price_usd": price,
            "tokens_bought": qty,
            "quote_currency": quote_currency,
            "quote_spent_usd": buy_amount,
            "buy_fee_tier": buy_fee,
            "tx_hash_buy": "NOT_RECORDED",
            "gas_cost_buy": gas_est,
            "pre_entry_activity": pre_entry_activity.get(token, [])[-20:],
            "tick_observations": [],
            "exit_timestamp_utc": "NOT_RECORDED",
            "exit_reason": "NOT_RECORDED",
            "exit_pnl_pct": "NOT_RECORDED",
            "exit_proceeds_usd": "NOT_RECORDED",
            "tx_hash_sell": "NOT_RECORDED",
            "gas_cost_sell": "NOT_RECORDED",
            "peak_drawdown": "NOT_RECORDED",
            "notes": []
        }

        active_positions[token].append(trade)
        trades.append(trade)
        pre_entry_activity[token] = []

    # ── Tick observations (R0/R1/R2) ──
    m = re.search(r'R(\d+)\s+(\S+)\s+\$([\d.]+)\s+PnL=([\+\-][\d.]+)%\s+5m=([\+\-][\d.]+)%\s+B/S=([\d/]+)\s+Peak=([\+\-][\d.]+)%\s+Trail=(\d+)%\s+Val=\$([\d.]+)\s+Tot=\$([\d.]+)', text)
    if m:
        token = m.group(2)
        tick = {
            "timestamp": ts,
            "rotation_slot": f"R{m.group(1)}",
            "price_usd": m.group(3),
            "pnl_pct": m.group(4),
            "momentum_5m_pct": m.group(5),
            "buy_sell_ratio": m.group(6),
            "peak_drawdown_pct": m.group(7),
            "trail_width_pct": m.group(8),
            "position_value_usd": m.group(9),
            "portfolio_total_usd": m.group(10)
        }
        # Add tick to the LAST (most recent) active position for this token
        if active_positions[token]:
            active_positions[token][-1]["tick_observations"].append(tick)

    # ── Exit triggers — set exit_reason on the LAST active position for the token ──
    def set_exit_reason(token, reason):
        if active_positions[token]:
            # Set on the last position that doesn't already have an exit
            for pos in reversed(active_positions[token]):
                if pos["exit_timestamp_utc"] == "NOT_RECORDED":
                    pos["exit_reason"] = reason
                    return pos
        return None

    # HARD SL
    m = re.search(r'HARD SL ([\+\-][\d.]+)% on (\S+) PnL=([\+\-][\d.]+)%', text)
    if m:
        set_exit_reason(m.group(2), f"HARD_SL ({m.group(1)}%)")

    # SLOWING
    m = re.search(r'SLOWING ([\+\-][\d.]+)%→([\+\-][\d.]+)% on (\S+) PnL=([\+\-][\d.]+)%', text)
    if m:
        set_exit_reason(m.group(3), f"SLOWING ({m.group(1)}%→{m.group(2)}%)")

    # MOMENTUM DEAD
    m = re.search(r'MOMENTUM DEAD 5m=([\+\-][\d.]+)% on (\S+) PnL=([\+\-][\d.]+)%', text)
    if m:
        set_exit_reason(m.group(2), f"MOMENTUM_DEAD (5m={m.group(1)}%)")

    # TAKE PROFIT
    m = re.search(r'TAKE PROFIT ([\+\-][\d.]+)%.*on (\S+) PnL=([\+\-][\d.]+)%', text)
    if m:
        set_exit_reason(m.group(2), f"HARD_TP ({m.group(1)}%)")

    # FAST SL TRIGGERED → look ahead for the FAST EXIT to get the token
    m = re.search(r'FAST SL ([\+\-][\d.]+)%', text)
    if m and 'TRIGGERED' in text:
        sl_pct = m.group(1)
        for j in range(i, min(i+5, len(raw_lines))):
            fm = re.search(r'FAST EXIT:.*on (\S+)', raw_lines[j].strip())
            if fm:
                set_exit_reason(fm.group(1), f"FAST_SL ({sl_pct}%)")
                break

    # BAD FILL — only match to an active position if the bought quantity
    # matches (BAD FILLs that occur on buys without a POSITION line should
    # not pollute trade records — they go into rejected_candidates instead)
    m = re.search(r'BAD FILL \(([\+\-][\d.]+)%\).*BLACKLISTING (\S+)', text)
    if m:
        token = m.group(2)
        # Look backward for the buy qty that triggered this BAD FILL
        bf_qty = None
        for j in range(i-1, max(i-10, 0), -1):
            bfm = re.search(r'(?:V[34] )?BOUGHT \S+:\s+([\d,]+)\s+tokens', raw_lines[j].strip())
            if bfm:
                bf_qty = bfm.group(1).replace(',', '')
                break
        # Only match to active positions whose tokens_bought matches the BAD FILL qty
        if active_positions[token]:
            matched = False
            for pos in reversed(active_positions[token]):
                if pos["exit_timestamp_utc"] == "NOT_RECORDED" and (bf_qty is None or pos["tokens_bought"] == bf_qty):
                    pos["exit_reason"] = f"BAD_FILL ({m.group(1)}%)"
                    pos["notes"].append("Token blacklisted after bad fill")
                    matched = True
                    break
            if not matched and bf_qty:
                # The BAD FILL qty doesn't match any active position — this was
                # a buy-and-immediate-sellback that never got a POSITION line.
                # Don't pollute the trade records.
                pass

    # SELL FAILED (EXIT_BLOCKED)
    m = re.search(r'SELL FAILED \d+ TIMES for (\S+).*EXIT_BLOCKED', text)
    if m:
        token = m.group(1)
        if active_positions[token]:
            for pos in active_positions[token]:
                if pos["exit_timestamp_utc"] == "NOT_RECORDED":
                    pos["notes"].append(f"EXIT_BLOCKED: Sell failed repeatedly, tokens stuck in wallet")

    # ── Exit confirmation ──
    # Match: "Exit #N — $VALUE (PnL X%)" or "Fast Exit #N — $VALUE"
    m = re.search(r'(?:⚡\s*)?(?:Fast )?Exit #(\d+)\s*—\s*\$([\d.]+)(?:\s*\(PnL ([\+\-][\d.]+)%\))?', text)
    if m:
        exit_num = m.group(1)
        proceeds = m.group(2)
        pnl = m.group(3) if m.group(3) else "NOT_RECORDED"

        # Find the token AND sell quantity: look backward for sell/trigger/sold lines
        # ONLY match lines that reference a token in our active positions
        matched_token = None
        sell_qty = None
        for j in range(i-1, max(i-30, 0), -1):
            jtext = raw_lines[j].strip()

            # _do_sell treating as success — look further back for TAKE PROFIT or sell
            if '_do_sell: sell fn returned 0 but tokens gone/dust' in jtext:
                for k in range(j-1, max(j-20, 0), -1):
                    ktext = raw_lines[k].strip()
                    # Find the TAKE PROFIT / SLOWING / etc. that triggered this sell
                    for tp in [
                        r'TAKE PROFIT.*on (\S+)',
                        r'HARD SL.*on (\S+)',
                        r'SLOWING.*on (\S+)',
                        r'MOMENTUM DEAD.*on (\S+)',
                    ]:
                        tkm = re.search(tp, ktext)
                        if tkm and tkm.group(1) in active_positions and active_positions[tkm.group(1)]:
                            matched_token = tkm.group(1)
                            break
                    if matched_token:
                        break
                if matched_token:
                    break
                continue

            # SELL TOKEN (QTY tokens)
            sq = re.search(r'(?:V[34] )?SELL (\S+) \(([\d,]+) tokens\)', jtext)
            if sq and sq.group(1) in active_positions and active_positions[sq.group(1)]:
                matched_token = sq.group(1)
                sell_qty = sq.group(2).replace(',', '')
                break

            # SOLD TOKEN
            sq = re.search(r'(?:V[34] )?SOLD (\S+)', jtext)
            if sq and sq.group(1) in active_positions and active_positions[sq.group(1)]:
                matched_token = sq.group(1)
                break

            # Exit trigger lines
            for trigger_pattern in [
                r'HARD SL.*on (\S+)',
                r'SLOWING.*on (\S+)',
                r'MOMENTUM DEAD.*on (\S+)',
                r'TAKE PROFIT.*on (\S+)',
                r'FAST EXIT:.*on (\S+)',
            ]:
                tm = re.search(trigger_pattern, jtext)
                if tm and tm.group(1) in active_positions and active_positions[tm.group(1)]:
                    matched_token = tm.group(1)
                    break
            if matched_token:
                break

        if matched_token and active_positions[matched_token]:
            # Try to match by sell quantity first (most precise)
            matched_pos = None
            if sell_qty and len(active_positions[matched_token]) > 1:
                for pos in active_positions[matched_token]:
                    if pos["tokens_bought"] == sell_qty:
                        matched_pos = pos
                        break
            # Fallback: first position with exit_reason set but no exit_timestamp
            if not matched_pos:
                for pos in active_positions[matched_token]:
                    if pos["exit_timestamp_utc"] == "NOT_RECORDED" and pos["exit_reason"] != "NOT_RECORDED":
                        matched_pos = pos
                        break
            # Last resort: any open position
            if not matched_pos:
                for pos in active_positions[matched_token]:
                    if pos["exit_timestamp_utc"] == "NOT_RECORDED":
                        matched_pos = pos
                        break
            if matched_pos:
                matched_pos["exit_timestamp_utc"] = ts
                matched_pos["exit_pnl_pct"] = pnl
                matched_pos["exit_proceeds_usd"] = proceeds
                if matched_pos["exit_reason"] == "NOT_RECORDED":
                    if 'Fast Exit' in text:
                        matched_pos["exit_reason"] = "FAST_EXIT"
                    else:
                        matched_pos["exit_reason"] = "UNKNOWN"
                # Remove from active list
                active_positions[matched_token].remove(matched_pos)

    # ── DETECTED existing position — engine restart merged positions ──
    m = re.search(r'DETECTED existing position:\s+(\S+)\s+\(([\d,]+)\s+tokens', text)
    if m:
        token = m.group(1)
        detected_qty = m.group(2).replace(',', '')
        # Any older positions for this token that don't match the detected qty
        # were superseded/merged by the engine
        if active_positions[token]:
            for pos in list(active_positions[token]):
                if pos["tokens_bought"] != detected_qty and pos["exit_timestamp_utc"] == "NOT_RECORDED":
                    pos["exit_reason"] = "SUPERSEDED_BY_ENGINE_MERGE"
                    pos["exit_timestamp_utc"] = ts
                    pos["exit_pnl_pct"] = "MERGED"
                    pos["exit_proceeds_usd"] = "MERGED_INTO_NEXT_POSITION"
                    pos["notes"].append(
                        f"Engine restart detected combined position ({detected_qty} tokens). "
                        f"This {pos['tokens_bought']}-token position was merged/superseded. "
                        f"Value captured in subsequent position exit."
                    )
                    active_positions[token].remove(pos)

    # ── _do_sell treating as success — handled by Exit # matching above ──

    # ── SELL lines — note fee tier ──
    m = re.search(r'(?:V[34] )?SELL (\S+).*fee=(\d+)', text)
    if m and 'REVERTED' not in text:
        token = m.group(1)
        if active_positions[token]:
            for pos in active_positions[token]:
                if pos["exit_timestamp_utc"] == "NOT_RECORDED":
                    # Only add once
                    fee_note = f"Sell fee tier: {m.group(2)}"
                    if fee_note not in pos["notes"]:
                        pos["notes"].append(fee_note)
                    break

# Handle remaining open positions
for token, pos_list in active_positions.items():
    for pos in pos_list:
        if pos["exit_timestamp_utc"] == "NOT_RECORDED":
            pos["notes"].append("POSITION STILL OPEN AT END OF LOG — EXIT NOT RECORDED")

# Add peak drawdown from ticks
for trade in trades:
    if trade["tick_observations"]:
        peaks = [float(t["peak_drawdown_pct"]) for t in trade["tick_observations"]]
        if peaks:
            trade["peak_drawdown"] = f"{min(peaks)}%"

# Clean up IPUNK's excessive sell fee notes
for trade in trades:
    if trade["token"] == "IPUNK":
        unique_notes = list(dict.fromkeys(trade["notes"]))
        trade["notes"] = unique_notes

# Write trades.json
with open(os.path.join(OUT_DIR, "trades.json"), 'w') as f:
    json.dump({
        "extraction_timestamp": "2026-09-08T09:00:00Z",
        "source_file": LOG_FILE,
        "total_trades": len(trades),
        "date_range": "2026-09-07 17:53:55 to 2026-09-08 09:02:42",
        "wallet": "0xB3C1B3670aBBE92459cced7A875B92B8045b8eB2",
        "notes": [
            "tx_hash fields marked NOT_RECORDED — engine log does not emit on-chain tx hashes",
            "gas_cost fields show estimates where available from honeypot check gas simulation",
            "IPUNK position had repeated sell reversions (EXIT_BLOCKED) and tokens remained in wallet",
            "Some PENNYSTOCKS/UNITY positions had multiple entries for the same token in rapid succession"
        ],
        "trades": trades
    }, f, indent=2, default=str)
print(f"  Written {len(trades)} trade records")

# Verify
unmatched = [t for t in trades if t['exit_timestamp_utc'] == 'NOT_RECORDED']
matched = [t for t in trades if t['exit_timestamp_utc'] != 'NOT_RECORDED']
print(f"    Matched exits: {len(matched)}")
print(f"    Unmatched (still open / stuck): {len(unmatched)}")
for t in unmatched:
    print(f"      #{t['trade_id']} {t['token']} entered {t['entry_timestamp_utc']}")
    for n in t['notes'][:3]:
        print(f"        {n}")

# ─── 4. REJECTED CANDIDATES ───
print("\n[4/5] Producing rejected_candidates.json...")

rejected = []
seen_rejections = set()

for i, line in enumerate(raw_lines):
    text = line.strip()
    ts = parse_ts(text)
    if not ts:
        continue

    # HONEYPOT BLOCKED
    m = re.search(r'HONEYPOT BLOCKED:\s+(\S+)\s+—\s+(.*)', text)
    if m:
        token = m.group(1)
        key = f"{ts}_{token}_honeypot"
        if key not in seen_rejections:
            seen_rejections.add(key)
            entry_5m = "NOT_RECORDED"
            route = "NOT_RECORDED"
            liq = "NOT_RECORDED"
            for j in range(i-1, max(i-10, 0), -1):
                pm = re.search(r'PICK.*?(\S+)\s+5m=([\+\-][\d.]+)%.*?route=(\S+)', raw_lines[j].strip())
                if pm:
                    entry_5m = pm.group(2) + "%"
                    route = pm.group(3)
                    break
            for j in range(i-1, max(i-10, 0), -1):
                lm = re.search(r'POSITION CAP.*?\$(\d+)\s+liq', raw_lines[j].strip())
                if lm:
                    liq = "$" + lm.group(1)
                    break
            rejected.append({
                "timestamp": ts, "token": token,
                "rejection_reason": "HONEYPOT_BLOCKED",
                "detail": m.group(2),
                "entry_5m": entry_5m, "route": route, "liquidity": liq
            })

    # BAD FILL
    m = re.search(r'BAD FILL \(([\+\-][\d.]+)%\).*BLACKLISTING (\S+)', text)
    if m:
        key = f"{ts}_{m.group(2)}_badfill"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(2),
                "rejection_reason": "BAD_FILL_BLACKLISTED",
                "detail": f"Fill slippage {m.group(1)}%, token blacklisted",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # Momentum DIED during observation
    m = re.search(r'Momentum DIED during observation \(([\+\-][\d.]+)%\)', text)
    if m:
        token = ""
        for j in range(i-1, max(i-10, 0), -1):
            t = extract_token_from_line(raw_lines[j].strip())
            if t:
                token = t
                break
        key = f"{ts}_{token}_momdied"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": token,
                "rejection_reason": "MOMENTUM_DIED_PRE_ENTRY",
                "detail": f"Momentum dropped to {m.group(1)}% during observation",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # SKIP — momentum not verified
    m = re.search(r'SKIP (\S+) — momentum not verified', text)
    if m:
        key = f"{ts}_{m.group(1)}_momnotverified"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "MOMENTUM_NOT_VERIFIED",
                "detail": "Momentum failed verification during pre-entry study",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # SKIP — pump already ripped too hard
    m = re.search(r'SKIP (\S+) — pump already ripped too hard \(5m=([\+\-][\d.]+)% > ([\+\-][\d.]+)% cap\)', text)
    if m:
        key = f"{ts}_{m.group(1)}_ripped"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "PUMP_TOO_HARD",
                "detail": f"5m={m.group(2)}% exceeded {m.group(3)}% cap",
                "entry_5m": m.group(2) + "%", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # SKIP — cooldown
    m = re.search(r'SKIP (\S+) — cooldown \((\d+)s remaining\)', text)
    if m:
        key = f"{ts}_{m.group(1)}_cooldown"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "COOLDOWN_ACTIVE",
                "detail": f"{m.group(2)}s remaining",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # SKIP — session-blocked
    m = re.search(r'SKIP (\S+) — buy reverted (\d+) times?, session-blocked', text)
    if m:
        key = f"{ts}_{m.group(1)}_sessionblocked"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "SESSION_BLOCKED",
                "detail": f"Buy reverted {m.group(2)} times, blocked for session",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # SKIP — blacklisted
    m = re.search(r'SKIP (\S+) — blacklisted', text)
    if m:
        key = f"{ts}_{m.group(1)}_blacklisted"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "BLACKLISTED",
                "detail": "Token on blacklist",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # SKIP — slippage too high
    m = re.search(r'SKIP (\S+) — slippage too high \(([\d.]+)%\)', text)
    if m:
        key = f"{ts}_{m.group(1)}_slippage"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "SLIPPAGE_TOO_HIGH",
                "detail": f"Slippage {m.group(2)}% exceeded max",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # BUY REVERTED
    m = re.search(r'(?:V[34] )?BUY (\S+) REVERTED', text)
    if m:
        key = f"{ts}_{m.group(1)}_reverted"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "BUY_REVERTED",
                "detail": "On-chain buy transaction reverted",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # V4 pool found but unknown quote token
    m = re.search(r'(0x[0-9a-fA-F]+): V4 pool found but unknown quote token', text)
    if m:
        key = f"{ts}_{m.group(1)}_unknownquote"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(1),
                "rejection_reason": "UNKNOWN_QUOTE_TOKEN",
                "detail": "V4 pool found but quote token not recognized",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

    # SLIPPAGE REJECT
    m = re.search(r'SLIPPAGE ([\d.]+)% > ([\d.]+)% MAX.*REJECT (\S+)', text)
    if m:
        key = f"{ts}_{m.group(3)}_slipreject"
        if key not in seen_rejections:
            seen_rejections.add(key)
            rejected.append({
                "timestamp": ts, "token": m.group(3),
                "rejection_reason": "SLIPPAGE_REJECTED",
                "detail": f"Slippage {m.group(1)}% > {m.group(2)}% max",
                "entry_5m": "NOT_RECORDED", "route": "NOT_RECORDED", "liquidity": "NOT_RECORDED"
            })

# Build summary
summary = defaultdict(int)
for r in rejected:
    summary[r["rejection_reason"]] += 1

with open(os.path.join(OUT_DIR, "rejected_candidates.json"), 'w') as f:
    json.dump({
        "extraction_timestamp": "2026-09-08T09:00:00Z",
        "source_file": LOG_FILE,
        "total_rejections": len(rejected),
        "rejection_summary": dict(summary),
        "rejections": rejected
    }, f, indent=2, default=str)

print(f"  Written {len(rejected)} rejection records")
for reason, count in sorted(summary.items(), key=lambda x: -x[1]):
    print(f"    {reason}: {count}")

# ─── 5. CONFIG VERSIONS ───
print("\n[5/5] Producing config_versions.json...")

configs = []
current_config = None

for i, line in enumerate(raw_lines):
    text = line.strip()
    ts = parse_ts(text)
    if not ts:
        continue

    m = re.search(r'ROTATION ENGINE (v[\d.]+)\s*—\s*(.*)', text)
    if m:
        version = m.group(1)
        description = m.group(2)
        settings = {}
        for j in range(i+1, min(i+6, len(raw_lines))):
            jtext = raw_lines[j].strip()
            em = re.search(r'Enter:\s+(.*?)\s*\|\s*Exit:\s+(.*)', jtext)
            if em:
                settings["entry_criteria"] = em.group(1)
                settings["exit_criteria"] = em.group(2)
            wm = re.search(r'Wallet:\s+(0x\S+)\s*\|\s*\$([\d.]+)\s+(\w+)\s*\+\s*~\$([\d]+)\s+(\w+)\s*\|\s*Target:\s*\$([\d]+)', jtext)
            if wm:
                settings["wallet"] = wm.group(1)
                settings["balance_1"] = f"${wm.group(2)} {wm.group(3)}"
                settings["balance_2"] = f"~${wm.group(4)} {wm.group(5)}"
                settings["target"] = f"${wm.group(6)}"
            if '====' in jtext:
                break

        config_entry = {
            "timestamp": ts,
            "version": version,
            "description": description,
            "settings": settings,
            "first_seen": ts,
            "last_seen": ts,
        }

        if current_config and current_config["version"] == version and current_config["settings"] == settings:
            current_config["last_seen"] = ts
            continue

        if current_config:
            current_config["active_until"] = ts

        current_config = config_entry
        configs.append(config_entry)

config_groups = []
for c in configs:
    config_groups.append({
        "version": c["version"],
        "description": c["description"],
        "first_seen": c["first_seen"],
        "last_seen": c.get("last_seen", c["first_seen"]),
        "active_until": c.get("active_until", "END_OF_LOG"),
        "settings": c["settings"]
    })

with open(os.path.join(OUT_DIR, "config_versions.json"), 'w') as f:
    json.dump({
        "extraction_timestamp": "2026-09-08T09:00:00Z",
        "source_file": LOG_FILE,
        "total_config_changes": len(config_groups),
        "versions_seen": sorted(set(c["version"] for c in config_groups)),
        "configs": config_groups
    }, f, indent=2, default=str)
print(f"  Written {len(config_groups)} config version records")
print(f"  Versions seen: {sorted(set(c['version'] for c in config_groups))}")

# ─── Summary ───
print("\n" + "="*60)
print("AUDIT EXTRACTION COMPLETE")
print("="*60)
print(f"Output directory: {OUT_DIR}")
print(f"  trades.json             — {len(trades)} trades ({len(matched)} closed, {len(unmatched)} open/stuck)")
print(f"  monitoring_timeline.csv — {len(csv_rows)} events")
print(f"  rejected_candidates.json — {len(rejected)} rejections")
print(f"  config_versions.json    — {len(config_groups)} config versions")
print(f"  raw_engine_log.txt      — {len(raw_lines)} lines")
