"""
RH Chain Rotation Engine v4 — MOMENTUM-VERIFIED, NO PROFIT CAP.

RULES:
  1. Only enter coins ACTIVELY pumping (5m > +10%)
  2. PRE-ENTRY: 3 momentum readings over 10s — must be stable/rising
  3. NO profit ceiling — ride the wave, let it 20x if it wants
  4. Dynamic trailing stop: tight when momentum cools, wide when pumping
  5. Exit when sellers meet buyers (B/S ratio drops)
  6. Exit when 5m momentum STARTS to die (not at death point)
  7. Hard SL: -5% safety net (tightened from -8%)
  8. 2-second polling — never miss a move
  9. Blacklist tokens with consistently bad fills
"""
import json
import logging
import os
import time
import sys
import threading
import urllib.request
from web3 import Web3
from web3.providers.base import BaseProvider
from eth_account import Account

# ── Structured trade outcome (replaces numeric -1 sentinel) ──
class TradeOutcome:
    """Structured result from a sell attempt. Replaces the numeric -1 sentinel.
    Preserves cost basis, identity, transaction intent, and resolution status."""
    RESOLVED = 'resolved'          # Tokens gone, proceeds attributed, fully reconciled
    UNRESOLVED = 'unresolved'      # Tokens gone, proceeds unknown (was -1 sentinel)
    FAILED = 'failed'              # Sell failed, tokens still held
    PENDING = 'pending'            # Transaction broadcast, not yet confirmed
    PARTIAL = 'partial'            # Proceeds obtained but residual tokens remain

    __slots__ = ('status', 'proceeds_usd', 'proceeds_raw', 'tx_hash', 'nonce',
                 'token_addr', 'managed_raw', 'sold_raw', 'residual_raw',
                 'cost_basis_usd', 'route',
                 'quote_sym', 'error', 'timestamp',
                 'tokens_confirmed_gone')  # True when delta check confirmed sale

    def __init__(self, status, **kwargs):
        self.status = status
        for slot in self.__slots__:
            if slot == 'status':
                continue
            setattr(self, slot, kwargs.get(slot))
        if self.timestamp is None:
            self.timestamp = time.time()

    @property
    def is_resolved(self):
        return self.status == self.RESOLVED

    @property
    def is_unresolved(self):
        return self.status == self.UNRESOLVED

    @property
    def is_failed(self):
        return self.status == self.FAILED

    @property
    def is_partial(self):
        """Proceeds obtained but residual tokens remain."""
        return self.status == self.PARTIAL

    def __repr__(self):
        return f'TradeOutcome({self.status}, proceeds_usd={self.proceeds_usd})'


# ── Transaction Coordinator — priority nonce management ──
import uuid
import collections


def _json_safe(obj):
    """Deep-convert Web3 types (HexBytes, AttributeDict) to JSON-serializable primitives.
    Handles nested structures safely. Used by TxOp.to_dict() for receipt serialization."""
    if isinstance(obj, bytes):
        return '0x' + obj.hex()
    if hasattr(obj, 'items'):
        # AttributeDict, dict, or dict-like
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(i) for i in obj]
    if isinstance(obj, int) and not isinstance(obj, bool):
        return obj
    if isinstance(obj, (str, float, bool)):
        return obj
    if obj is None:
        return obj
    # Fallback: convert to string
    return str(obj)


class TxOp:
    """A tracked wallet operation with durable identity and lifecycle."""
    PENDING = 'pending'       # Broadcast attempted, awaiting receipt
    CONFIRMED = 'confirmed'   # Receipt received, status == 1
    REVERTED = 'reverted'     # Receipt received, status == 0
    FAILED = 'failed'         # Broadcast or build failed (no tx hash)
    REPLACED = 'replaced'     # Superseded by a newer nonce

    TIMEOUT = 'timeout'       # wait_for_transaction_receipt timed out (tx may still confirm)

    __slots__ = ('op_id', 'op_type', 'status', 'nonce', 'tx_hash',
                 'receipt', 'token_addr', 'symbol', 'amount_raw',
                 'created_at', 'confirmed_at', 'error', 'generation',
                 'broadcast_attempted_at', 'signed_hash')

    def __init__(self, op_type, **kwargs):
        self.op_id = kwargs.get('op_id') or str(uuid.uuid4())[:12]
        self.op_type = op_type  # 'buy', 'sell', 'approve', 'unwrap', 'permit2'
        self.status = kwargs.get('status', self.PENDING)
        self.nonce = kwargs.get('nonce')
        self.tx_hash = kwargs.get('tx_hash')
        self.receipt = kwargs.get('receipt')
        self.token_addr = kwargs.get('token_addr')
        self.symbol = kwargs.get('symbol')
        self.amount_raw = kwargs.get('amount_raw')
        self.created_at = kwargs.get('created_at') or time.time()
        self.confirmed_at = kwargs.get('confirmed_at')
        self.error = kwargs.get('error')
        self.generation = kwargs.get('generation', 0)
        self.broadcast_attempted_at = kwargs.get('broadcast_attempted_at')
        self.signed_hash = kwargs.get('signed_hash')  # hash of signed tx before broadcast

    def to_dict(self):
        d = {}
        for s in self.__slots__:
            v = getattr(self, s)
            if v is None:
                continue
            # Deep-serialize Web3 receipts (HexBytes, AttributeDict, nested)
            if s == 'receipt':
                v = _json_safe(v)
            d[s] = v
        return d


class TxCoordinator:
    """Priority wallet transaction coordinator.
    - Owns nonce assignment: no inline get_transaction_count elsewhere
    - Protective sells get priority over scanning/reporting
    - Tracks pending/confirmed/replaced/reverted states
    - Reconciles crashes: broadcast → ack → confirm → account exactly once
    """

    def __init__(self, w3_instance, wallet_addr, account):
        self._w3 = w3_instance
        self._wallet = wallet_addr
        self._acct = account
        self._lock = threading.Lock()
        self._next_nonce = None  # Lazy-init from chain
        self._ops = collections.OrderedDict()  # op_id → TxOp
        self._generation = 0  # Incremented on new position

    def _sync_nonce(self):
        """Fetch on-chain nonce. Called once at init or after crash recovery."""
        chain_nonce = self._w3.eth.get_transaction_count(self._wallet)
        self._next_nonce = chain_nonce

    def acquire_nonce(self):
        """Thread-safe nonce acquisition. Returns (nonce, generation)."""
        with self._lock:
            if self._next_nonce is None:
                self._sync_nonce()
            n = self._next_nonce
            self._next_nonce += 1
            return n, self._generation

    def resync_nonce(self):
        """Re-read nonce from chain after a failed broadcast or timeout.
        SAFETY: never reissue a nonce that has a PENDING or TIMEOUT operation.
        A timed-out tx may still confirm and consume its nonce.
        The chain nonce advances only when a tx confirms, so unresolved ops
        with nonces >= chain_nonce must be reconciled first."""
        with self._lock:
            chain_nonce = self._w3.eth.get_transaction_count(self._wallet)
            # Find highest unresolved nonce (PENDING or TIMEOUT) — must not go below it + 1
            unresolved = [op for op in self._ops.values()
                          if op.status in (TxOp.PENDING, TxOp.TIMEOUT)]
            if unresolved:
                max_unresolved_nonce = max(
                    (op.nonce for op in unresolved if op.nonce is not None),
                    default=chain_nonce - 1)
                self._next_nonce = max(chain_nonce, max_unresolved_nonce + 1)
            else:
                self._next_nonce = chain_nonce

    def new_generation(self):
        """Increment generation counter (new position). Reject old callbacks."""
        with self._lock:
            self._generation += 1
            return self._generation

    @property
    def generation(self):
        return self._generation

    def register_op(self, op_type, **kwargs):
        """Register a new operation before broadcast."""
        op = TxOp(op_type, generation=self._generation, **kwargs)
        self._ops[op.op_id] = op
        return op

    def confirm_op(self, op_id, receipt):
        """Record receipt for a pending operation."""
        op = self._ops.get(op_id)
        if not op:
            return None
        op.receipt = receipt
        op.confirmed_at = time.time()
        if receipt and receipt.get('status') == 1:
            op.status = TxOp.CONFIRMED
        else:
            op.status = TxOp.REVERTED
        return op

    def fail_op(self, op_id, error):
        """Mark operation as failed (no tx hash obtained)."""
        op = self._ops.get(op_id)
        if not op:
            return None
        op.status = TxOp.FAILED
        op.error = str(error)
        return op

    def is_valid_generation(self, gen):
        """Check if a callback's generation matches current position."""
        return gen == self._generation

    def pending_ops(self):
        """Return list of pending operations."""
        return [op for op in self._ops.values() if op.status == TxOp.PENDING]

    def timeout_ops(self):
        """Return list of timed-out operations that need reconciliation."""
        return [op for op in self._ops.values() if op.status == TxOp.TIMEOUT]

    def has_unreconciled(self):
        """True if any ops are pending or timed-out — must reconcile before new buy/sell."""
        return any(op.status in (TxOp.PENDING, TxOp.TIMEOUT) for op in self._ops.values())

    def mark_broadcast(self, op_id, signed_hash=None):
        """Record transport attempt durably BEFORE calling send_raw_transaction.
        If we crash after this but before broadcast, we know intent was recorded."""
        op = self._ops.get(op_id)
        if op:
            op.broadcast_attempted_at = time.time()
            if signed_hash:
                op.signed_hash = signed_hash
        return op

    def mark_timeout(self, op_id, error=None):
        """Mark operation as timed-out (not failed — tx may still confirm later).
        Must be reconciled before permitting another operation on same nonce."""
        op = self._ops.get(op_id)
        if not op:
            return None
        op.status = TxOp.TIMEOUT
        op.error = str(error) if error else 'receipt timeout'
        return op

    def reconcile_op(self, op_id):
        """Attempt to reconcile a timed-out op by checking on-chain receipt.
        Returns the op with updated status, or None if not found."""
        op = self._ops.get(op_id)
        if not op or not op.tx_hash:
            return op
        try:
            receipt = self._w3.eth.get_transaction_receipt(op.tx_hash)
            if receipt:
                op.receipt = receipt
                op.confirmed_at = time.time()
                op.status = TxOp.CONFIRMED if receipt.get('status') == 1 else TxOp.REVERTED
        except Exception:
            pass  # Still unconfirmed — leave as timeout
        return op

    def ops_for_token(self, token_addr):
        """Return all ops for a specific token."""
        al = token_addr.lower() if token_addr else ''
        return [op for op in self._ops.values()
                if op.token_addr and op.token_addr.lower() == al]

    def to_dict(self):
        """Serialize for state persistence."""
        return {
            'generation': self._generation,
            'next_nonce': self._next_nonce,
            'ops': {k: v.to_dict() for k, v in self._ops.items()},
        }

    def load_from_dict(self, data):
        """Restore from persisted state."""
        self._generation = data.get('generation', 0)
        self._next_nonce = data.get('next_nonce')
        for op_id, od in data.get('ops', {}).items():
            op = TxOp(od.get('op_type', 'unknown'))
            for k, v in od.items():
                if hasattr(op, k):
                    setattr(op, k, v)
            self._ops[op_id] = op


# ── Position Ledger — full lifecycle tracking ──
class PositionLedger:
    """Tracks the full lifecycle of a position: acquired → monitoring → exiting → resolved.
    Persists: managed_raw, cost_basis, route, exec_peak, exit_latch, intents,
    nonces, hashes, and unresolved outcomes."""

    __slots__ = ('token_addr', 'symbol', 'route', 'generation',
                 'managed_raw', 'sold_raw', 'residual_raw',
                 'cost_basis_usd', 'proceeds_usd', 'gas_spent_usd',
                 'entry_price', 'exec_peak_usd', 'exit_latch',
                 'buy_op_id', 'sell_op_ids', 'approval_op_ids',
                 'status', 'created_at', 'resolved_at',
                 'buy_tx_hash', 'sell_tx_hashes', 'receipts')

    ACTIVE = 'active'
    EXITING = 'exiting'
    RESOLVED = 'resolved'
    UNRESOLVED = 'unresolved'
    PARTIAL = 'partial'

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))
        if self.status is None:
            self.status = self.ACTIVE
        if self.sold_raw is None:
            self.sold_raw = 0
        if self.residual_raw is None:
            self.residual_raw = 0
        if self.proceeds_usd is None:
            self.proceeds_usd = 0
        if self.gas_spent_usd is None:
            self.gas_spent_usd = 0
        if self.sell_op_ids is None:
            self.sell_op_ids = []
        if self.approval_op_ids is None:
            self.approval_op_ids = []
        if self.sell_tx_hashes is None:
            self.sell_tx_hashes = []
        if self.receipts is None:
            self.receipts = []
        if self.created_at is None:
            self.created_at = time.time()

    def record_sell(self, sold_raw, proceeds_usd, tx_hash=None, receipt=None):
        """Record a (possibly partial) sell. Idempotent on tx_hash — duplicate
        receipts are rejected to prevent double-crediting proceeds."""
        # Duplicate receipt guard — same tx_hash cannot credit twice
        if tx_hash and tx_hash in (self.sell_tx_hashes or []):
            return self  # Already credited — skip
        self.sold_raw = (self.sold_raw or 0) + sold_raw
        self.proceeds_usd = (self.proceeds_usd or 0) + proceeds_usd
        if tx_hash:
            self.sell_tx_hashes.append(tx_hash)
        if receipt:
            # Deep-serialize Web3 receipts (HexBytes, AttributeDict, nested)
            self.receipts.append(_json_safe(receipt))
        # Compute residual
        self.residual_raw = max(0, (self.managed_raw or 0) - (self.sold_raw or 0))
        # Update status
        if self.residual_raw == 0 and self.proceeds_usd > 0:
            self.status = self.RESOLVED
            self.resolved_at = time.time()
        elif self.residual_raw > 0 and (self.sold_raw or 0) > 0:
            self.status = self.PARTIAL
        return self

    def mark_unresolved(self, error=None):
        """Tokens gone but proceeds unknown."""
        self.status = self.UNRESOLVED
        return self

    def is_fully_resolved(self):
        """Position is fully resolved with attributed proceeds."""
        return self.status == self.RESOLVED and (self.residual_raw or 0) == 0

    @property
    def net_pnl_usd(self):
        if self.proceeds_usd and self.cost_basis_usd:
            return self.proceeds_usd - self.cost_basis_usd - (self.gas_spent_usd or 0)
        return None

    def to_dict(self):
        """Serialize to plain dict. Deep-converts Web3 types (HexBytes,
        AttributeDict, nested) via _json_safe(). Applies to all fields
        that may contain Web3 types (tx hashes, receipts, etc.)."""
        d = {}
        for s in self.__slots__:
            v = getattr(self, s)
            if v is None:
                continue
            # Deep-serialize ALL fields — tx hashes (bytes/HexBytes), receipts
            # (AttributeDict), and any nested Web3 types
            d[s] = _json_safe(v)
        return d

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


# ── Read-Only Provider Wrapper for shadow mode ──
_READ_ONLY_ALLOWED = frozenset([
    'eth_call', 'eth_getBalance', 'eth_getTransactionCount',
    'eth_getTransactionReceipt', 'eth_getBlockByNumber',
    'eth_blockNumber', 'eth_chainId', 'eth_gasPrice',
    'eth_getCode', 'eth_getLogs', 'eth_getStorageAt',
    'eth_estimateGas', 'net_version',
])


class ReadOnlyProvider(BaseProvider):
    """Wraps a Web3 provider to enforce read-only RPC access.
    Blocks wallet-changing RPCs at the transport layer.
    Used in shadow mode to prevent accidental signing/broadcasting.
    Inherits from BaseProvider so Web3 isinstance validation passes."""

    def __init__(self, inner_provider):
        super().__init__()
        self._inner = inner_provider

    def make_request(self, method, params):
        if method not in _READ_ONLY_ALLOWED:
            raise PermissionError(
                f'ReadOnlyProvider: blocked RPC method {method!r} — shadow mode is read-only')
        return self._inner.make_request(method, params)

    def is_connected(self, show_traceback=False):
        return self._inner.is_connected(show_traceback=show_traceback)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ── Quote freshness configuration ──
QUOTE_MAX_AGE_S = 4.0    # Executable quote valid for 4 seconds (2 blocks at 0.1s/block margin)
QUOTE_STALE_LABEL = '⚠️ STALE'


_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs')
os.makedirs(_log_dir, exist_ok=True)
_log_file = os.path.join(_log_dir, 'engine.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),                          # stdout (for nohup / Claude session)
        logging.FileHandler(_log_file, mode='a'),         # always append to logs/engine.log
    ]
)
logger = logging.getLogger('rot')

# ── Chain ──
RPC = 'https://rpc.mainnet.chain.robinhood.com'
CID = 4663
AGG = '0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc'
HOOK = '0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044'
POSMGR = '0x8366a39CC670B4001A1121B8F6A443A643e40951'
Z = '0x0000000000000000000000000000000000000000'
V3_ROUTER = '0xcaf681a66d020601342297493863e78c959e5cb2'   # SwapRouter02 on RH Chain
QUOTER_V2 = '0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7'   # QuoterV2 for V3 simulation
WETH_ADDR = '0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73'

# ── V4 Contracts (verified on-chain) ──
V4_POOL_MANAGER = '0x8366a39cc670b4001a1121b8f6a443a643e40951'  # PoolManager Singleton
V4_QUOTER       = '0x8dc178efb8111bb0973dd9d722ebeff267c98f94'  # V4 Quoter
V4_STATE_VIEW   = '0xf3334192d15450cdd385c8b70e03f9a6bd9e673b'  # StateView
V4_UNIVERSAL_ROUTER = '0x8876789976decbfcbbbe364623c63652db8c0904'  # Universal Router v2.1.1
PERMIT2         = '0x000000000022D473030F116dDEE9F6B43aC78BA3'  # Permit2

# ── Quote Token Registry (generic — any quote token with address, decimals, display name) ──
QUOTE_TOKENS = {
    # addr_lower: (symbol, decimals)
    '0x117cc2133c37b721f49de2a7a74833232b3b4c0c': ('SPY', 18),
    '0x5fc5360d0400a0fd4f2af552add042d716f1d168': ('USDG', 6),
    '0xccee82fe024c36fa15e1005ede3e9e4787e23d09': ('HIMS', 18),
    '0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec': ('NVDA', 18),
}
# Feature flags — enable new quotes one at a time
QUOTE_ENABLED = {
    '0x117cc2133c37b721f49de2a7a74833232b3b4c0c': True,   # SPY — tested, live
    '0x5fc5360d0400a0fd4f2af552add042d716f1d168': True,   # USDG — ENABLED (canary, $47 buffer)
    '0xccee82fe024c36fa15e1005ede3e9e4787e23d09': False,  # HIMS — pending canary
    '0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec': False,  # NVDA — pending canary
}
# Legacy alias (SPY-specific code still references this)
SPY_ADDR  = '0x117cc2133c37B721F49dE2A7a74833232B3B4C0C'
ACCEPTED_QUOTES = {  # Legacy — will be replaced by QUOTE_TOKENS + QUOTE_ENABLED
    '0x117cc2133c37b721f49de2a7a74833232b3b4c0c': ('SPY', 500),
}
ETH_USD = 2466

# ══════════════════════════════════════════════════════════════════════
# PROVISIONAL CONFIGURATION v1.0 — 2026-09-08
# Labeled PROVISIONAL: these are engineering defaults, not historically
# optimal values. Do not claim they are profit-optimized.
# ══════════════════════════════════════════════════════════════════════

# ── Staging (NOT a running deployment — production source untouched, service stopped) ──

# ── Entry Conditions ──
MIN_5M_ENTRY = 19.0       # +19% min 5-minute pump
MAX_5M_ENTRY = 225.0      # Refuse if pump already >+225%
MIN_BS_ENTRY = 1.02       # B/S tx-count ratio ≥ 1.02
MIN_LIQ = 20000           # Minimum pool liquidity $20k
MIN_1H_ENTRY = 0.0        # 1h change must be positive
VERIFY_OBS = 3             # 3 observations over ~6 seconds
VERIFY_INTERVAL = 2.0     # ~2s between verification observations
MOMENTUM_DROP_REJECT = 30.0  # Reject if 5m% drops >30% from highest reading to last

# ── Exit Rules ──
HARD_SL = 3.0             # Hard stop TRIGGER: estimated net liquidation ≤ -3%
                          # This is a trigger, not a guaranteed fill price.
                          # A gap past -3% produces the actual fill, not a fabricated -3%.
HARD_TP = 25.0            # Take profit: exit 100% when net estimated executable profit ≥ +25%
                          # Policy B runner DISABLED per Shaheer directive.
TRAIL_UNIFIED = 6.0       # Unified trailing stop: 6% decline from executable-value peak
TRAIL_TIGHT = 3.0         # Reserved (Policy B disabled)
TRAIL_WIDE = 7.0          # Reserved (Policy B disabled)
MOMENTUM_DEATH = 5.0      # Exit when 5m% falls below +5%
MOMENTUM_STRONG = 10.0    # 5m > +10% = strong momentum (informational)
SELLER_DOM_EXIT = 0.7     # Exit when B/S ratio falls below 0.7
SLOWING_DROP_PCT = 30.0   # Exit when 5m% drops >30% from previous observation
MOMENTUM_HISTORY = 4      # Track last 4 readings

# ── Position Sizing & Execution ──
# CEILING: 100% of deployable capital (gas reserves, pending commitments, and
# unmanaged holdings excluded). The largest position that passes actual-size
# execution, sellability, and economic checks is used. This is a ceiling —
# size is reduced or entry rejected if checks fail. Never widen execution
# tolerance to satisfy the capital target.
# NOTE: Near-full exposure can trigger the 3% daily circuit breaker after
# one approximately-3% losing position.
POSITION_SIZE_PCT = 100.0  # Ceiling: up to 100% of deployable capital
LIQ_CAP_PCT = 100.0       # No fixed liquidity cap — actual-size checks take precedence
MAX_ACTIVE_POSITIONS = 1  # One position at a time (explicit)
MAX_SLIPPAGE = 2.0        # Max acceptable slippage %
MAX_RT_COST_EXPECTED = 1.5  # Max quoted immediate round-trip loss %
MAX_RT_COST_BOUNDED = 2.5  # Max modeled round-trip loss with execution slippage
EXEC_SLIPPAGE_ALLOW = 0.5  # Normal additional execution slippage per swap
GAS_RESERVE = 0.003       # ETH kept for gas (approvals + protective exits)

# ── Monitoring Timing ──
POLL_SEC = 2              # Main loop price check interval
SCAN_INTERVAL = 5         # Seconds between candidate scans
FAST_POLL_SEC = 0.4       # Fast monitor poll interval
FAST_SL_THRESHOLD = -3.0  # Fast stop-loss (matches HARD_SL)
FAST_PEAK_DROP = 6.0      # Fast trailing: 6% drop from peak (matches TRAIL_UNIFIED)
EXEC_QUOTE_MAX_AGE = 1.0  # Max age (seconds) for execution quote before signing
PRICE_OBS_MAX_AGE = 2.0   # Max age for price observation
# Integer slippage multiplier: 1000 - (EXEC_SLIPPAGE_ALLOW * 10) = 995 for 0.5%
_SLIP_NUMER = 995          # Numerator for integer min_out: proceeds_raw * 995 // 1000
CONFIRM = 2               # Consecutive ticks to confirm signal

# ── Re-entry & Cooldown ──
EXIT_COOLDOWN_SEC = 60    # 60s cooldown after completed position exit
MAX_SESSION_LOSSES = 2    # 2 losing exits on same token within 24h → entry block
MAX_BUY_REVERTS = 2       # Max confirmed reverted buy attempts per setup

# ── Warmup ──
WARMUP_TICKS = 0          # No time-based warmup exemption — all exits active immediately

# ── Portfolio Control ──
DAILY_DRAWDOWN_LIMIT = 3.0  # Pause new entries if equity drops 3% from day start
                            # NOTE: with near-full position sizing, one ~3% loss triggers this
_day_start_equity = None     # Set at UTC midnight or engine start
_entry_paused = False        # True when circuit breaker tripped

# Known Pons-compatible tokens (V4 hook=0xE5e702, ETH pair)
PONS_WHITELIST = {
    '0x9d0e8be0ef309c7aa92ef031997c3dde0540c4a1',  # MORALS
    '0x2862d471505f7d365ce6f53cf1176baaa8d666f6',  # ENTITY
    '0x64c2cc275e3a09cb8c63e8633e0473ef0a1612a4',  # ATLAS
    '0x5ec9cd905ff6695fc8b5943a5e1e83a23b610156',  # PONINU
    '0xe76a12bcd2f0e6d3db9f9012321642198e6cbd1b',  # RH4
    '0x17c35105dab9936f113af60f3bf74783caf08301',  # HD
    '0x000d9659a0c1ddedce0d73fe4be5c55a781e980e',  # GIVER
    '0x47366e0f257ac009e82bd46fb74e2fb50826ce98',  # AOBS
    '0x86202ef2ecf0fb70a3b74794cd5bdc5a91bb0f97',  # ONLY
}
COST_BASIS = 803.0

# Blacklisted tokens — consistently bad fills / slippage death traps
_HARDCODED_BLACKLIST = {
    '0x78b96280c3347e0f58a7147b73eb0ec5ffff025d',  # RSTR — -10% to -25% slippage every time
    '0x9e88da78e13755bea9a4988c987849794ec3f5e8',  # Pledge — repeated bad fills, -6.8% instant SL (Shaheer directive)
    '0xf6994adbe500c8466e60ce11e38fe69d1b1e7777',  # Pledge (alt addr) — blacklisted
    '0xf1d0c16a8924207783c0b2397b68bc7635908736',  # IPUNK — V4 honeypot, sells always revert
}
# Blacklist file path — initially in source dir, migrated to _STATE_DIR after wallet init.
# _load_blacklist() and _save_blacklist() handle the lazy resolution.
_BLACKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.engine_state', 'rh_blacklist.json')
def _load_blacklist():
    bl = set(_HARDCODED_BLACKLIST)
    try:
        with open(_BLACKLIST_FILE) as f:
            bl.update(json.load(f))
    except Exception:
        pass
    return bl
def _save_blacklist():
    try:
        dynamic = [a for a in PONS_BLACKLIST if a not in _HARDCODED_BLACKLIST]
        with open(_BLACKLIST_FILE, 'w') as f:
            json.dump(dynamic, f)
    except Exception:
        pass
PONS_BLACKLIST = _load_blacklist()
HDR = {'User-Agent': 'rot/3.0'}

# ── ABI ──
E20 = json.loads('[{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"s","type":"address"},{"name":"v","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
SWAP = json.loads('[{"inputs":[{"name":"steps","type":"tuple[]","components":[{"name":"stepType","type":"uint8"},{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"pool","type":"address"},{"name":"fee","type":"uint24"},{"name":"tickSpacing","type":"int24"},{"name":"hook","type":"address"},{"name":"hookData","type":"bytes"},{"name":"recipient","type":"address"},{"name":"poolId","type":"bytes32"}]},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"deadline","type":"uint256"}],"name":"swap","outputs":[{"name":"","type":"uint256"}],"stateMutability":"payable","type":"function"}]')
# SwapRouter02 ABI — NO deadline in params (handled via multicall wrapper)
V3_SWAP_ABI = json.loads('[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"},{"name":"minHopPriceX36","type":"uint160"}],"name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"},{"inputs":[{"name":"amountMinimum","type":"uint256"},{"name":"recipient","type":"address"}],"name":"unwrapWETH9","outputs":[],"stateMutability":"payable","type":"function"},{"inputs":[{"name":"deadline","type":"uint256"},{"name":"data","type":"bytes[]"}],"name":"multicall","outputs":[{"name":"results","type":"bytes[]"}],"stateMutability":"payable","type":"function"},{"inputs":[],"name":"refundETH","outputs":[],"stateMutability":"payable","type":"function"}]')
QUOTER_V2_ABI = json.loads('[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"fee","type":"uint24"},{"name":"minHopPriceX36","type":"uint160"}],"name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},{"name":"sqrtPriceX96After","type":"uint160"},{"name":"initializedTicksCrossed","type":"uint32"},{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]')
WETH_ABI = json.loads('[{"inputs":[],"name":"deposit","outputs":[],"stateMutability":"payable","type":"function"},{"inputs":[{"name":"wad","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"}]')

# ── V4 ABIs ──
# Universal Router: execute(bytes commands, bytes[] inputs, uint256 deadline)
V4_ROUTER_ABI = json.loads('[{"inputs":[{"name":"commands","type":"bytes"},{"name":"inputs","type":"bytes[]"},{"name":"deadline","type":"uint256"}],"name":"execute","outputs":[],"stateMutability":"payable","type":"function"}]')
# V4 Quoter: quoteExactInputSingle (uses PoolKey + params)
V4_QUOTER_ABI = json.loads('[{"inputs":[{"components":[{"components":[{"name":"currency0","type":"address"},{"name":"currency1","type":"address"},{"name":"fee","type":"uint24"},{"name":"tickSpacing","type":"int24"},{"name":"hooks","type":"address"}],"name":"poolKey","type":"tuple"},{"name":"zeroForOne","type":"bool"},{"name":"exactAmount","type":"uint128"},{"name":"hookData","type":"bytes"}],"name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]')
# StateView: getSlot0 and getLiquidity (by poolId bytes32)
V4_STATE_VIEW_ABI = json.loads('[{"inputs":[{"name":"poolId","type":"bytes32"}],"name":"getSlot0","outputs":[{"name":"sqrtPriceX96","type":"uint160"},{"name":"tick","type":"int24"},{"name":"protocolFee","type":"uint24"},{"name":"lpFee","type":"uint24"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"poolId","type":"bytes32"}],"name":"getLiquidity","outputs":[{"name":"liquidity","type":"uint128"}],"stateMutability":"view","type":"function"}]')
# Permit2: approve + allowance
PERMIT2_ABI = json.loads('[{"inputs":[{"name":"owner","type":"address"},{"name":"token","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"amount","type":"uint160"},{"name":"expiration","type":"uint48"},{"name":"nonce","type":"uint48"}],"stateMutability":"view","type":"function"},{"inputs":[{"name":"token","type":"address"},{"name":"spender","type":"address"},{"name":"amount","type":"uint160"},{"name":"expiration","type":"uint48"}],"name":"approve","outputs":[],"stateMutability":"nonpayable","type":"function"}]')

# V4 fee tiers to probe (common on Uniswap V4)
V4_FEES_TICKS = [(500, 10), (3000, 60), (10000, 200), (100, 1)]  # (fee, tickSpacing) pairs

# Track which route each token uses: 'pons' or 'v3' or 'v4'
TOKEN_ROUTE = {}  # addr_lower -> 'pons' | 'v3'
V3_FEES = [10000, 3000, 500, 100]  # Fee tiers to try

# ── Scan speed caches ──
_v3_fail_cache = {}    # addr_lower -> timestamp of last failure (skip re-test for 5 min)
_pair_data_cache = {}  # addr_lower -> {'quote_sym': ..., 'quote_addr': ..., 'ts': ..., 'pair_addr': ...}
_v4_pool_cache = {}    # dexscreener_token_addr_lower -> {'pool_id': bytes32, 'pool_key': tuple, 'c0': addr, 'c1': addr, 'quote_sym': str, 'quote_dec': int}
V3_FAIL_TTL = 300      # 5 min before re-testing a failed V3 token

# V4 pool discovery constants (all RH Chain V4 pools use these)
V4_DEFAULT_FEE = 0
V4_DEFAULT_TICK_SPACING = 200
# Initialize event topic for V4 PoolManager
V4_INIT_TOPIC = None  # Computed lazily after w3 is initialized
# Persistence file for V4 routes + pool cache (Finding 6: survive restarts)
V4_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.v4_routes_cache.json')

# ── Fast Price Monitor (sub-second) ──
MONITOR_BLOCK_RANGE = 20     # How many blocks to scan per poll (~1.5s of blocks)
V3_SWAP_TOPIC = Web3.keccak(text='Swap(address,address,int256,int256,uint160,uint128,int24)').hex()
V3_FACTORY_RH = '0x1f7d7550B1b028f7571E69A784071f0205FD2EfA'  # Correct V3 Factory (24.5KB bytecode)
FACTORY_ABI = json.loads('[{"inputs":[{"name":"tokenA","type":"address"},{"name":"tokenB","type":"address"},{"name":"fee","type":"uint24"}],"name":"getPool","outputs":[{"name":"pool","type":"address"}],"stateMutability":"view","type":"function"}]')
POOL_SLOT0_ABI = json.loads('[{"inputs":[],"name":"slot0","outputs":[{"name":"sqrtPriceX96","type":"uint160"},{"name":"tick","type":"int24"},{"name":"observationIndex","type":"uint16"},{"name":"observationCardinality","type":"uint16"},{"name":"observationCardinalityNext","type":"uint16"},{"name":"feeProtocol","type":"uint8"},{"name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]')

# ── Wallet — deferred key loading for shadow/test safety ──
# RPC connection is always needed (read-only), but signing key is only loaded
# for production modes (main, --status). Shadow mode and imports skip key loading.
_KEY_FILE = os.environ.get('RH_KEY_FILE', '/root/shaheer-project/.rh_chain_key.json')
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={'headers': {'User-Agent':'rot/3','Content-Type':'application/json'}, 'timeout': 15}))

# Deferred: loaded by _init_wallet() below. Set to None so imports don't crash.
acct = None  # type: ignore
W = None     # type: ignore
_tx_coord = None  # type: ignore
_pos_ledger = None  # Set on successful buy in enter(), cleared on full resolve
_restored_position = None  # Populated by _load_state() if engine crashed with open position

# Contracts that need only RPC (no key)
sc = None       # type: ignore
v3r = None      # type: ignore
quoter_v2 = w3.eth.contract(address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_V2_ABI)
weth_c = None   # type: ignore
v4_router = None  # type: ignore
v4_quoter = w3.eth.contract(address=Web3.to_checksum_address(V4_QUOTER), abi=V4_QUOTER_ABI)
v4_state = w3.eth.contract(address=Web3.to_checksum_address(V4_STATE_VIEW), abi=V4_STATE_VIEW_ABI)
permit2_c = None  # type: ignore

_wallet_initialized = False

def _init_wallet():
    """Load signing key and initialize wallet-dependent contracts.
    Called by main() and --status. NOT called by --shadow or test imports."""
    global acct, W, sc, v3r, weth_c, v4_router, permit2_c, _tx_coord, _wallet_initialized
    if _wallet_initialized:
        return
    with open(_KEY_FILE) as f:
        _k = json.load(f)
    acct = Account.from_key(_k['private_key'])
    W = acct.address
    sc = w3.eth.contract(address=Web3.to_checksum_address(AGG), abi=SWAP)
    v3r = w3.eth.contract(address=Web3.to_checksum_address(V3_ROUTER), abi=V3_SWAP_ABI)
    weth_c = w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDR), abi=WETH_ABI)
    v4_router = w3.eth.contract(address=Web3.to_checksum_address(V4_UNIVERSAL_ROUTER), abi=V4_ROUTER_ABI)
    permit2_c = w3.eth.contract(address=Web3.to_checksum_address(PERMIT2), abi=PERMIT2_ABI)
    _tx_coord = TxCoordinator(w3, W, acct)
    _wallet_initialized = True
    _init_state_dir()
    logger.info('Wallet initialized: %s', W)

def _init_shadow_wallet():
    """Initialize wallet address for shadow mode — truly credential-free.
    Reads ONLY the 'address' field from the key file (or RH_WALLET_ADDR env var).
    NEVER loads private_key, NEVER calls Account.from_key.
    Wraps ALL providers in ReadOnlyProvider to block wallet-changing RPCs.
    Uses isolated shadow state directory to prevent production state corruption."""
    global w3, W, _tx_coord, _wallet_initialized, quoter_v2, v4_quoter, v4_state, _shadow_mode
    if _wallet_initialized:
        return
    # Resolve wallet address WITHOUT loading signing credentials
    addr = os.environ.get('RH_WALLET_ADDR', '')
    if not addr:
        try:
            with open(_KEY_FILE) as f:
                _k = json.load(f)
            addr = _k.get('address', '')
            # NEVER read private_key — only the address field
            del _k
        except Exception:
            pass
    if not addr:
        raise RuntimeError('Shadow mode requires RH_WALLET_ADDR env var or address in key file')
    W = Web3.to_checksum_address(addr)
    # Install read-only provider on the GLOBAL w3 instance — every contract/helper
    # that uses w3 will go through the transport-level block
    ro_provider = ReadOnlyProvider(w3.provider)
    w3 = Web3(ro_provider)
    # Rebuild read-only contract instances against the secured provider
    quoter_v2 = w3.eth.contract(
        address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_V2_ABI)
    v4_quoter = w3.eth.contract(
        address=Web3.to_checksum_address(V4_QUOTER), abi=V4_QUOTER_ABI)
    v4_state = w3.eth.contract(
        address=Web3.to_checksum_address(V4_STATE_VIEW), abi=V4_STATE_VIEW_ABI)
    # No coordinator in shadow — there are no transactions to coordinate
    _tx_coord = None
    _shadow_mode = True  # Enable isolated state directory
    _wallet_initialized = True
    _init_state_dir()
    logger.info('Shadow wallet initialized (credential-free, read-only, isolated state): %s', W)


def _acquire_nonce(op_type='tx', token_addr=None, symbol=None):
    """Acquire next nonce from coordinator for a BROADCAST transaction.
    Registers a durable TxOp BEFORE returning the nonce — intent is persisted
    before any broadcast attempt. Returns (nonce, op, generation) tuple.
    Pass generation to _sign_and_send() to enable stale-position rejection.
    For simulation/eth_call, use w3.eth.get_transaction_count(W, generation=_gen) directly."""
    nonce, gen = _tx_coord.acquire_nonce()
    op = _tx_coord.register_op(op_type, nonce=nonce,
                                token_addr=token_addr, symbol=symbol)
    return nonce, op, gen


def _broadcast(signed_tx, op):
    """Durably record transport attempt, then broadcast. Returns tx hash.
    Records broadcast_attempted_at BEFORE calling send_raw_transaction so
    crash-recovery knows whether broadcast was ever attempted.
    Raises on send failure — caller must handle and call _tx_coord.fail_op()."""
    # Record durable intent before transport attempt
    signed_hash = signed_tx.hash.hex() if hasattr(signed_tx, 'hash') else None
    _tx_coord.mark_broadcast(op.op_id, signed_hash=signed_hash)
    _save_state()  # Persist before broadcast — crash-safe
    # Attempt broadcast
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    op.tx_hash = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
    return tx_hash


def _wait_receipt(tx_hash, op, timeout=120):
    """Wait for transaction receipt with timeout handling.
    On timeout: marks op as TIMEOUT (not FAILED — tx may still confirm later).
    On success: marks op as CONFIRMED or REVERTED.
    Returns receipt or None on timeout."""
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        if receipt:
            _tx_coord.confirm_op(op.op_id, receipt)
        return receipt
    except Exception as ex:
        if 'timeout' in str(ex).lower() or 'timed out' in str(ex).lower():
            logger.warning('BROADCAST TIMEOUT for op %s nonce=%s — tx may still confirm (marking TIMEOUT)',
                          op.op_id, op.nonce)
            _tx_coord.mark_timeout(op.op_id, error=str(ex))
            _save_state()
            return None
        # Other receipt errors — still mark timeout (could be transient)
        logger.warning('RECEIPT ERROR for op %s: %s — marking TIMEOUT for reconciliation',
                      op.op_id, str(ex)[:80])
        _tx_coord.mark_timeout(op.op_id, error=str(ex))
        _save_state()
        return None


def _sign_and_send(tx_dict, op, timeout=120, generation=None):
    """Sign, broadcast with durable recording, and wait for receipt.
    Handles the full coordinator lifecycle for one transaction.
    If generation is supplied, validates it is still current before signing —
    rejects stale-generation transactions to prevent cross-position leakage.
    Returns receipt on success, None on timeout.
    Raises on broadcast failure (op is marked FAILED)."""
    # Generation guard — reject if position changed since nonce was acquired
    if generation is not None and _tx_coord and not _tx_coord.is_valid_generation(generation):
        _tx_coord.fail_op(op.op_id, f'stale generation {generation} (current {_tx_coord.generation})')
        raise RuntimeError(f'Stale generation {generation}: position changed, aborting tx')
    try:
        signed = acct.sign_transaction(tx_dict)
    except Exception as sign_ex:
        _tx_coord.fail_op(op.op_id, f'sign failed: {sign_ex}')
        raise
    try:
        tx_hash = _broadcast(signed, op)
    except Exception as send_ex:
        _tx_coord.fail_op(op.op_id, f'broadcast failed: {send_ex}')
        raise
    return _wait_receipt(tx_hash, op, timeout=timeout)


# ── V4 Cache Persistence (Finding 6: survive restarts) ──
def _save_v4_cache():
    """Persist V4 routes + pool cache to disk so positions survive restarts."""
    try:
        data = {}
        for al, pool_data in _v4_pool_cache.items():
            # Convert bytes to hex for JSON serialization
            data[al] = {
                'pool_id': '0x' + pool_data['pool_id'].hex(),
                'pool_key': list(pool_data['pool_key']),  # tuple → list
                'c0': pool_data['c0'],
                'c1': pool_data['c1'],
                'fee': pool_data['fee'],
                'tick_spacing': pool_data['tick_spacing'],
                'hooks': pool_data['hooks'],
                'quote_sym': pool_data['quote_sym'],
                'quote_dec': pool_data['quote_dec'],
                'quote_addr': pool_data['quote_addr'],
                'liq': pool_data['liq'],
            }
        # Also save TOKEN_ROUTE entries that are V4
        v4_routes = {k: v for k, v in TOKEN_ROUTE.items() if v.startswith('v4')}
        payload = {'v4_pool_cache': data, 'v4_routes': v4_routes, 'ts': time.time()}
        with open(V4_CACHE_FILE, 'w') as f:
            json.dump(payload, f, indent=2)
        logger.info('V4 cache saved: %d pools, %d routes', len(data), len(v4_routes))
    except Exception as ex:
        logger.warning('V4 cache save failed: %s', str(ex)[:80])


def _load_v4_cache():
    """Load V4 routes + pool cache from disk on startup."""
    if not os.path.exists(V4_CACHE_FILE):
        return
    try:
        with open(V4_CACHE_FILE) as f:
            payload = json.load(f)
        loaded_pools = 0
        loaded_routes = 0
        for al, pd in payload.get('v4_pool_cache', {}).items():
            pool_id_hex = pd['pool_id']
            pool_id_bytes = bytes.fromhex(pool_id_hex[2:] if pool_id_hex.startswith('0x') else pool_id_hex)
            _v4_pool_cache[al] = {
                'pool_id': pool_id_bytes,
                'pool_key': tuple(pd['pool_key']),  # list → tuple
                'c0': pd['c0'],
                'c1': pd['c1'],
                'fee': pd['fee'],
                'tick_spacing': pd['tick_spacing'],
                'hooks': pd['hooks'],
                'quote_sym': pd['quote_sym'],
                'quote_dec': pd['quote_dec'],
                'quote_addr': pd['quote_addr'],
                'liq': pd['liq'],
            }
            loaded_pools += 1
        for al, route in payload.get('v4_routes', {}).items():
            if al not in TOKEN_ROUTE:
                # Finding 6 (R2): Filter cached routes through QUOTE_ENABLED
                # Don't restore routes for disabled quote tokens
                pool_d = _v4_pool_cache.get(al)
                if pool_d:
                    qa = pool_d.get('quote_addr', '')
                    qs = pool_d.get('quote_sym', '?')
                    if qs not in ('ETH', '?') and not QUOTE_ENABLED.get(qa, False):
                        logger.info('V4 cache: skipping route %s for %s — %s quote disabled', route, al[:10], qs)
                        continue
                TOKEN_ROUTE[al] = route
                loaded_routes += 1
        age = time.time() - payload.get('ts', 0)
        logger.info('V4 cache loaded: %d pools, %d routes (%.0fs old)', loaded_pools, loaded_routes, age)
    except Exception as ex:
        logger.warning('V4 cache load failed: %s', str(ex)[:80])


# Load V4 cache on startup
_load_v4_cache()


def fetch(url):
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=10).read())
    except:
        return None


def price(addr):
    """Get price + momentum data from DexScreener."""
    d = fetch(f'https://api.dexscreener.com/latest/dex/tokens/{addr}')
    if not d: return None
    best = None
    best_liq = 0
    for p in (d.get('pairs') or []):
        if p.get('chainId') == 'robinhood':
            liq = float(p.get('liquidity', {}).get('usd', 0))
            if liq > best_liq:
                best_liq = liq
                best = p
    if not best: return None
    return {
        'p': float(best.get('priceUsd', 0)),
        'c5': best.get('priceChange', {}).get('m5', 0) or 0,
        'c1h': best.get('priceChange', {}).get('h1', 0) or 0,
        'b5': best.get('txns', {}).get('m5', {}).get('buys', 0),
        's5': best.get('txns', {}).get('m5', {}).get('sells', 0),
        'liq': best_liq,
        'fdv': float(best.get('fdv', 0)),
        'sym': best.get('baseToken', {}).get('symbol', '?'),
    }


def bal(addr):
    tc = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=E20)
    r = tc.functions.balanceOf(W).call()
    d = tc.functions.decimals().call()
    return r, d


def eth_bal():
    return float(Web3.from_wei(w3.eth.get_balance(W), 'ether'))


def update_bal(sym=None, addr=None, tamt=0, tval=0):
    try:
        e = eth_bal()
        t = tval + e * ETH_USD
        d = {'total_usd': round(t, 2), 'holdings': [], 'eth_balance': e, 'eth_price': ETH_USD,
             'wallet': W, 'chain_id': CID, 'platform': 'robinhood_chain', 'cost_basis': COST_BASIS, 'source': 'robinhood_chain'}
        if sym and tamt > 0:
            d['holdings'].append({'symbol': sym, 'amount': tamt, 'value_usd': round(tval, 2), 'address': addr})
        with open('/tmp/robinhood-balance.json', 'w') as f: json.dump(d, f, indent=2)
    except: pass


def approve(addr):
    tc = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=E20)
    spender = Web3.to_checksum_address(AGG)
    a = tc.functions.allowance(W, spender).call()
    if a > 10**30: return True
    # Zero-reset: some ERC20 tokens (e.g. USDT-like) require approve(0) before
    # approve(MAX) when current allowance is nonzero but insufficient
    if a > 0:
        logger.info('  approve: zero-reset required for %s (current=%d)', addr[:10], a)
        nonce0, op0, _gen = _acquire_nonce('approve', token_addr=addr)
        try:
            tx0 = tc.functions.approve(spender, 0).build_transaction(
                {'from': W, 'nonce': nonce0,
                 'gas': 100000, 'gasPrice': int(w3.eth.gas_price * 2), 'chainId': CID})
        except Exception as build_ex:
            logger.error('  approve: zero-reset build_tx failed for %s: %s — releasing nonce', addr[:10], build_ex)
            _tx_coord.fail_op(op0.op_id, f'approve zero-reset build failed: {build_ex}')
            _tx_coord.resync_nonce()
            return False
        r0 = _sign_and_send(tx0, op0, timeout=60, generation=_gen)
        if not r0 or r0['status'] != 1:
            logger.error('  approve: zero-reset REVERTED for %s', addr[:10])
            return False
    nonce, op, _gen = _acquire_nonce('approve', token_addr=addr)
    try:
        tx = tc.functions.approve(spender, 2**256-1).build_transaction(
            {'from': W, 'nonce': nonce, 'gas': 100000, 'gasPrice': int(w3.eth.gas_price*2), 'chainId': CID})
    except Exception as build_ex:
        logger.error('  approve: build_tx failed for %s: %s — releasing nonce', addr[:10], build_ex)
        _tx_coord.fail_op(op.op_id, f'approve build failed: {build_ex}')
        _tx_coord.resync_nonce()
        return False
    r = _sign_and_send(tx, op, timeout=60, generation=_gen)
    return r is not None and r['status'] == 1


def _quote_min_out(sell_quote, sell_amount):
    """Compute min_out from a fresh SellQuote using integer-safe math.
    Returns (min_out, True) if quote is valid and fresh, or (0, False) otherwise.

    Validates:
      - sell_quote is not None, ok=True
      - timestamp is finite and 0 <= age <= EXEC_QUOTE_MAX_AGE (rejects future/NaN)
      - proceeds_raw is a positive integer
      - token_raw must be present AND match sell_amount (strict binding)
    """
    if not sell_quote or not sell_quote.ok:
        return 0, False
    # Reject non-finite timestamps (NaN, inf)
    ts = sell_quote.timestamp
    if not isinstance(ts, (int, float)) or ts != ts or ts == float('inf') or ts == float('-inf'):
        logger.warning('Quote has non-finite timestamp: %r — rejecting', ts)
        return 0, False
    age = time.time() - ts
    if age < 0 or age > EXEC_QUOTE_MAX_AGE:
        return 0, False
    if not sell_quote.proceeds_raw or sell_quote.proceeds_raw <= 0:
        return 0, False
    if not isinstance(sell_quote.proceeds_raw, int):
        logger.warning('Quote proceeds_raw is not int: %r — rejecting', type(sell_quote.proceeds_raw))
        return 0, False
    # Strict binding: token_raw must be present and match sell_amount exactly
    # Reject None/0 token_raw when sell_amount is positive (fail-closed)
    if not sell_quote.token_raw or sell_quote.token_raw <= 0:
        if sell_amount and sell_amount > 0:
            logger.warning('Quote missing token_raw (%r) for sell_amount=%d — rejecting (fail-closed)',
                           sell_quote.token_raw, sell_amount)
            return 0, False
    elif sell_amount and sell_quote.token_raw != sell_amount:
        logger.warning('Quote/tx mismatch: quoted %d vs sell %d — rejecting',
                       sell_quote.token_raw, sell_amount)
        return 0, False
    # Integer-safe: proceeds_raw * 995 // 1000 (0.5% slippage)
    min_out = sell_quote.proceeds_raw * _SLIP_NUMER // 1000
    return min_out, True


def sell(addr, sym, market_price=0, managed_raw=0, sell_quote=None):
    r, d = bal(addr)
    if r == 0: return 0.0
    # Sell exact managed quantity if provided, otherwise full balance
    if managed_raw > 0 and managed_raw <= r:
        r = managed_raw
    if not approve(addr): return 0.0
    tokens_human = r / (10**d)
    logger.info('SELL %s (%s tokens)', sym, f'{tokens_human:,.0f}')
    steps = [{'stepType':2,'tokenIn':Web3.to_checksum_address(addr),'tokenOut':Z,'pool':Z,'fee':0,'tickSpacing':200,'hook':HOOK,'hookData':b'','recipient':POSMGR,'poolId':b'\x00'*32}]
    # ═══ Executable quote → min_out (integer-safe, 1s freshness at signing) ═══
    # Pons has no on-chain quoter (sell_quote.ok=False) — market_price fallback is Pons-only.
    min_out, used_quote = _quote_min_out(sell_quote, r)
    # Timestamp ONLY valid for executable quotes. For market_price fallback,
    # force signing boundary to always refresh — market_price was observed
    # by the caller (scan loop) and may already be stale.
    _pons_minout_ts = sell_quote.timestamp if used_quote else 0  # 0 → always triggers re-check
    if used_quote:
        logger.info('  SELL minOut: %d (exec quote, integer-safe)', min_out)
    elif market_price > 0 and tokens_human > 0:
        # Pons-only fallback: chart price approximation
        expected_eth = (tokens_human * market_price) / ETH_USD
        min_out = int(Web3.to_wei(expected_eth, 'ether')) * _SLIP_NUMER // 1000
        logger.info('  SELL minOut: %d (Pons market_price fallback)', min_out)
    try:
        # Snapshot pre-sell ETH balance for delta calculation
        pre_eth = eth_bal()
        if min_out > 0:
            attempts = [(min_out, f'quote-{EXEC_SLIPPAGE_ALLOW}%')]
        else:
            # No valid quote — do NOT fall back to zero.
            # Log warning, attempt with unprotected single try.
            logger.warning('SELL %s: no valid quote — unprotected attempt (quote unavailable)', sym)
            # No valid quote — do NOT sign or broadcast without execution protection.
            # Return 0 to retain exit intent; caller will retry when quote available.
            logger.error('SELL %s: no executable quote — refusing to sign. Exit intent retained.', sym)
            return 0.0  # minimum 1 wei, not zero
        for attempt_min, attempt_label in attempts:
            nonce, op, _gen = _acquire_nonce('sell', token_addr=addr, symbol=sym)
            try:
                tx = sc.functions.swap(steps, Z, r, attempt_min, int(time.time())+300).build_transaction(
                    {'from':W,'value':0,'nonce':nonce,'gas':400000,'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})  # 5x gas = INSTANT
            except Exception as build_ex:
                logger.error('SELL %s: build_transaction failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op.op_id, f'build_transaction failed: {build_ex}')
                _tx_coord.resync_nonce()  # Reclaim nonce gap
                return 0.0
            # ═══ SIGNING BOUNDARY FRESHNESS CHECK (Pons) ═══
            # RPCs above (nonce, gas, approval) take time. Revalidate before signing.
            # Pons: sell_quote.ok is typically False (no on-chain quoter). Check both
            # exec-quote path AND market_price fallback via _pons_minout_ts.
            _signing_age = time.time() - _pons_minout_ts
            if _signing_age > EXEC_QUOTE_MAX_AGE:
                logger.warning('SELL %s: min_out stale at signing (age=%.1fs > %.1fs) — re-computing',
                               sym, _signing_age, EXEC_QUOTE_MAX_AGE)
                try:
                    # Try exec quote first
                    fresh_sq = get_sell_quote(addr, r, sym, route='pons')
                    fresh_min, fresh_ok = _quote_min_out(fresh_sq, r)
                    if fresh_ok and fresh_min > 0:
                        sell_quote = fresh_sq  # Update active quote
                        attempt_min = fresh_min
                        _pons_minout_ts = fresh_sq.timestamp  # Track actual quote time
                    elif market_price > 0 and tokens_human > 0:
                        # Re-fetch chart price for Pons fallback — MUST get fresh price, no stale fallback
                        _fresh_info = price(addr)
                        if not _fresh_info or _fresh_info.get('p', 0) <= 0:
                            logger.error('SELL %s: price() returned no data at signing boundary — refusing to sign stale', sym)
                            _tx_coord.fail_op(op.op_id, 'signing boundary price refresh failed')
                            _tx_coord.resync_nonce()
                            return 0.0
                        _fresh_mkt = _fresh_info['p']
                        expected_eth = (tokens_human * _fresh_mkt) / ETH_USD
                        attempt_min = int(Web3.to_wei(expected_eth, 'ether')) * _SLIP_NUMER // 1000
                        _pons_minout_ts = time.time()  # price() just called — fresh
                        logger.info('SELL %s: refreshed market_price fallback minOut=%d (fresh price=$%.8f)', sym, attempt_min, _fresh_mkt)
                    else:
                        logger.error('SELL %s: re-quote failed — refusing to sign stale', sym)
                        _tx_coord.fail_op(op.op_id, 'signing boundary re-quote failed')
                        _tx_coord.resync_nonce()  # Reclaim nonce gap
                        return 0.0
                    # Validate refreshed min_out is non-zero (rounding to 0 = unprotected)
                    if attempt_min <= 0:
                        logger.error('SELL %s: refreshed minOut rounded to 0 — refusing unprotected sign', sym)
                        _tx_coord.fail_op(op.op_id, 'refreshed min_out is zero')
                        _tx_coord.resync_nonce()
                        return 0.0
                    tx = sc.functions.swap(steps, Z, r, attempt_min, int(time.time())+300).build_transaction(
                        {'from':W,'value':0,'nonce':nonce,'gas':400000,'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})
                    # Re-verify age after rebuild — build_transaction RPCs can be slow
                    _post_build_age = time.time() - _pons_minout_ts
                    if _post_build_age > EXEC_QUOTE_MAX_AGE:
                        logger.error('SELL %s: post-rebuild age %.1fs > %.1fs — refusing to sign', sym, _post_build_age, EXEC_QUOTE_MAX_AGE)
                        _tx_coord.fail_op(op.op_id, f'post-rebuild stale: {_post_build_age:.1f}s')
                        _tx_coord.resync_nonce()
                        return 0.0
                    logger.info('SELL %s: rebuilt tx with fresh minOut=%d', sym, attempt_min)
                except Exception as rq_ex:
                    logger.error('SELL %s: signing boundary re-compute error: %s — refusing to sign', sym, rq_ex)
                    _tx_coord.fail_op(op.op_id, f'signing boundary re-quote error: {rq_ex}')
                    _tx_coord.resync_nonce()  # Reclaim nonce gap
                    return 0.0
            rc = _sign_and_send(tx, op, timeout=120, generation=_gen)
            if rc is None:
                logger.warning('SELL %s TIMEOUT (%s) — will reconcile', sym, attempt_label)
                return 0.0
            if rc['status'] == 1:
                received = eth_bal() - pre_eth
                logger.info('SOLD %s → %.6f ETH ($%.2f) [%s]', sym, received, received*ETH_USD, attempt_label)
                update_bal()
                return received
            logger.error('SELL %s REVERTED (%s) — caller will retry next tick with fresh quote', sym, attempt_label)
        return 0.0
    except Exception as ex:
        logger.error('SELL %s ERR: %s', sym, ex)
        return 0.0


def buy(addr, eth_amt, sym, market_price=0):
    wei = Web3.to_wei(eth_amt, 'ether')
    logger.info('BUY %s with %.6f ETH ($%.2f)', sym, eth_amt, eth_amt*ETH_USD)
    steps = [{'stepType':2,'tokenIn':Z,'tokenOut':Web3.to_checksum_address(addr),'pool':Z,'fee':0,'tickSpacing':200,'hook':HOOK,'hookData':b'','recipient':POSMGR,'poolId':b'\x00'*32}]
    # ═══ FIX 1: amountOutMin — reject on-chain if fill > 3% worse than market ═══
    min_out = 0
    if market_price > 0:
        try:
            tc = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=E20)
            d = tc.functions.decimals().call()
        except Exception:
            d = 18
        expected_tokens = (eth_amt * ETH_USD) / market_price
        min_tokens = expected_tokens * (1 - MAX_SLIPPAGE / 100)
        min_out = int(min_tokens * (10 ** d))
        logger.info('  amountOutMin: %.0f tokens (%.0f expected, %d%% tolerance)', min_tokens, expected_tokens, MAX_SLIPPAGE)
    try:
        # Snapshot pre-buy token balance for delta calculation
        pre_r, pre_d = bal(addr)
        nonce, op, _gen = _acquire_nonce('buy', token_addr=addr, symbol=sym)
        try:
            tx = sc.functions.swap(steps, Z, wei, min_out, int(time.time())+300).build_transaction(
                {'from':W,'value':wei,'nonce':nonce,'gas':350000,'gasPrice':int(w3.eth.gas_price*2.5),'chainId':CID})
        except Exception as build_ex:
            logger.error('BUY %s: build_transaction failed: %s — releasing nonce', sym, build_ex)
            _tx_coord.fail_op(op.op_id, f'buy build failed: {build_ex}')
            _tx_coord.resync_nonce()
            return 0, 0
        rc = _sign_and_send(tx, op, timeout=120, generation=_gen)
        if rc is None:
            logger.warning('BUY %s TIMEOUT — will reconcile', sym)
            return 0, 0
        if rc['status'] == 1:
            post_r, post_d = bal(addr)
            tb = (post_r - pre_r) / (10**post_d)
            logger.info('BOUGHT %s: %s tokens', sym, f'{tb:,.0f}')
            try:
                approve(addr)
            except Exception as appr_ex:
                logger.error('PONS BUY %s: post-buy approval failed (non-fatal): %s', sym, appr_ex)
            entry_usd = eth_amt * ETH_USD / tb if tb > 0 else 0
            return tb, entry_usd
        logger.error('BUY %s REVERTED', sym)
        return 0, 0
    except Exception as ex:
        logger.error('BUY %s ERR: %s', sym, ex)
        return 0, 0


def pons_ok(addr):
    for attempt in range(2):
        try:
            ca = Web3.to_checksum_address(addr)
            steps = [{'stepType':2,'tokenIn':Z,'tokenOut':ca,'pool':Z,'fee':0,'tickSpacing':200,'hook':HOOK,'hookData':b'','recipient':POSMGR,'poolId':b'\x00'*32}]
            nonce = w3.eth.get_transaction_count(W)
            gp = int(w3.eth.gas_price * 2)
            tx = sc.functions.swap(steps, Z, Web3.to_wei(0.0005,'ether'), 0, int(time.time())+300).build_transaction(
                {'from':W,'value':Web3.to_wei(0.0005,'ether'),'nonce':nonce,'gas':500000,'gasPrice':gp,'chainId':CID})
            w3.eth.estimate_gas(tx)
            return True
        except:
            if attempt == 0: time.sleep(1)
    return False


def honeypot_check(addr):
    """CRITICAL: Simulate buy + sell via eth_call with state overrides BEFORE committing real money.
    Returns True if token is SAFE (can be sold). Returns False if HONEYPOT.

    FIX (Codex Ultra): Previous implementation used separate eth_calls for buy and sell,
    so the sell sim didn't have the tokens from the buy sim → false positives.
    Now uses state overrides to inject a fake token balance for the sell simulation,
    making the sell test independent of buy state."""
    ca = Web3.to_checksum_address(addr)
    route = TOKEN_ROUTE.get(addr.lower(), 'pons')

    try:
        # Step 1: Simulate buy to verify pool exists and we can get tokens
        if route.startswith('v4'):
            # V4: use cached pool data (exact on-chain PoolKey) for Quoter check
            pool_data = _v4_pool_cache.get(addr.lower())
            if not pool_data:
                logger.warning('  HONEYPOT CHECK: V4 no cached pool data — SKIP')
                return False
            pool_key = pool_data['pool_key']
            quote_dec = pool_data['quote_dec']
            c0, c1 = pool_data['c0'], pool_data['c1']
            qt_is_c0 = (pool_data['quote_addr'] == c0.lower())
            buy_zero_for_one = qt_is_c0  # Send quote (c0) to receive token (c1)
            test_amt = int(0.0001 * (10 ** quote_dec))
            try:
                result = v4_quoter.functions.quoteExactInputSingle(
                    (pool_key, buy_zero_for_one, test_amt, b'')).call()
                tokens_out = result[0]
            except Exception:
                tokens_out = 0
            if tokens_out == 0:
                logger.warning('  HONEYPOT CHECK: V4 buy quote returned 0 — SKIP')
                return False
            # Verify sell direction works too (reverse direction)
            sell_zero_for_one = not buy_zero_for_one
            try:
                sell_result = v4_quoter.functions.quoteExactInputSingle(
                    (pool_key, sell_zero_for_one, tokens_out, b'')).call()
                sell_out = sell_result[0]
            except Exception:
                sell_out = 0
            if sell_out == 0:
                logger.warning('  HONEYPOT CHECK: V4 sell quote returned 0 — HONEYPOT! BLACKLISTING %s', addr[:10])
                return False
            # Roundtrip sanity: should get back at least 50% of what we put in
            roundtrip = sell_out / test_amt * 100 if test_amt > 0 else 0
            if roundtrip < 50:
                logger.warning('  HONEYPOT CHECK: V4 roundtrip %.1f%% — too much tax/fee — SKIP %s', roundtrip, addr[:10])
                return False
            # Finding 5 (R2): Simulate actual token transfer to catch transfer taxes/blacklists.
            # Use state override to inject a fake balance, then call transfer(W, 1).
            v4_token = c1 if qt_is_c0 else c0
            v4_token_cs = Web3.to_checksum_address(v4_token)
            try:
                # Build transfer(W, 1) calldata — tests the real transfer path
                token_c = w3.eth.contract(address=v4_token_cs, abi=E20)
                transfer_abi = json.loads('[{"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]')
                token_t = w3.eth.contract(address=v4_token_cs, abi=transfer_abi)
                tx_data = token_t.functions.transfer(W, 1).build_transaction(
                    {'from': W, 'gas': 200000, 'gasPrice': 0, 'nonce': 0, 'chainId': CID, 'value': 0})
                # State override: give W a huge token balance (slot 0 mapping assumption)
                # balanceOf mapping slot = keccak256(abi.encode(address, uint256(slot)))
                # Try common balance slots (0, 1, 2) via brute-force override of balanceOf return
                # Simpler: just override the balance via the account's storage directly
                # Most ERC20s store balances at mapping slot 0: keccak(addr . slot)
                bal_slot = Web3.keccak(
                    bytes.fromhex(W[2:].lower().zfill(64)) + (0).to_bytes(32, 'big')
                )
                big_bal = (10**30).to_bytes(32, 'big')
                state_override = {
                    v4_token_cs: {
                        'stateDiff': {
                            '0x' + bal_slot.hex(): '0x' + big_bal.hex(),
                        }
                    }
                }
                # eth_call with state override
                result = w3.eth.call(tx_data, 'latest', state_override)
                # If we get here without revert, transfer is allowed
                logger.info('  HONEYPOT CHECK: V4 transfer sim OK for %s', v4_token[:10])
            except Exception as tex:
                err_str = str(tex)[:120]
                if 'revert' in err_str.lower() or 'execution reverted' in err_str.lower():
                    logger.warning('  HONEYPOT CHECK: V4 token %s BLOCKS transfer — HONEYPOT! %s', v4_token[:10], err_str)
                    return False
                # FIX: Non-revert errors (state override not supported, RPC issues) — BLOCK entry
                # An inconclusive transfer sim cannot prove the token is sellable
                logger.warning('  HONEYPOT CHECK: V4 transfer sim INCONCLUSIVE: %s — BLOCKING entry (roundtrip %.1f%%)', err_str, roundtrip)
                return False
            # CRITICAL: Simulate actual V4 sell through Universal Router to catch
            # hook-blocked sells (e.g. IPUNK — Quoter passes but UR reverts because
            # beforeSwap hook rejects unauthorized sellers).
            try:
                sell_commands, sell_inputs = _encode_v4_swap(
                    pool_key, sell_zero_for_one, tokens_out, 0)
                sell_deadline = int(time.time()) + 300
                sell_tx = v4_router.functions.execute(
                    sell_commands, sell_inputs, sell_deadline
                ).build_transaction({
                    'from': W, 'value': 0,
                    'nonce': w3.eth.get_transaction_count(W),
                    'gas': 500000, 'gasPrice': int(w3.eth.gas_price * 2),
                    'chainId': CID})
                permit2_ca_cs = Web3.to_checksum_address(PERMIT2)
                p2_slot = Web3.keccak(
                    bytes.fromhex(permit2_ca_cs[2:].lower().zfill(64)) +
                    Web3.keccak(bytes.fromhex(W[2:].lower().zfill(64)) + (1).to_bytes(32, 'big'))
                )
                ur_sell_override = {
                    v4_token_cs: {
                        'stateDiff': {
                            '0x' + bal_slot.hex(): '0x' + big_bal.hex(),
                            '0x' + p2_slot.hex(): '0x' + ((2**256-1).to_bytes(32, 'big')).hex(),
                        }
                    }
                }
                w3.eth.call(sell_tx, 'latest', ur_sell_override)
                logger.info('  HONEYPOT CHECK: V4 UR sell sim OK — hook allows sells')
            except Exception as ur_ex:
                ur_err = str(ur_ex)[:200]
                if 'sell not allowed' in ur_err.lower() or '6570f956' in ur_err or '90bfb865' in ur_err:
                    logger.warning('  🚨 HONEYPOT DETECTED: V4 hook BLOCKS sells for %s — BLACKLISTING: %s',
                                   addr[:10], ur_err[:80])
                    PONS_BLACKLIST.add(addr.lower())
                    return False
                # FIX: Unknown revert must NOT pass as SAFE — block entry without blacklisting
                logger.warning('  HONEYPOT CHECK: V4 UR sell sim INCONCLUSIVE: %s — BLOCKING entry (not blacklisting)',
                            ur_err[:80])
                return False
            logger.info('  HONEYPOT CHECK: V4 roundtrip OK (%.1f%%) + UR sell OK — SAFE', roundtrip)
            return True

        elif route.startswith('v3'):
            fee = int(route.split('_')[1])
            weth = Web3.to_checksum_address(WETH_ADDR)

            if route.startswith('v3spy'):
                quote_ca = Web3.to_checksum_address(SPY_ADDR)
                test_buy = int(0.0001 * 1e18)
                # Use QuoterV2 for stateless quote (no state override needed)
                try:
                    result = quoter_v2.functions.quoteExactInputSingle(
                        (quote_ca, ca, test_buy, fee, 0)).call()
                    tokens_out = result[0]
                except Exception:
                    tokens_out = 0
                if tokens_out == 0:
                    logger.warning('  HONEYPOT CHECK: V3-SPY buy quote returned 0 — SKIP')
                    return False
                # Quote sell direction
                try:
                    sell_result = quoter_v2.functions.quoteExactInputSingle(
                        (ca, quote_ca, tokens_out, fee, 0)).call()
                    sell_out = sell_result[0]
                except Exception:
                    sell_out = 0
            else:
                test_buy = Web3.to_wei(0.0001, 'ether')
                # Use QuoterV2 for stateless quote
                try:
                    result = quoter_v2.functions.quoteExactInputSingle(
                        (weth, ca, test_buy, fee, 0)).call()
                    tokens_out = result[0]
                except Exception:
                    tokens_out = 0
                if tokens_out == 0:
                    logger.warning('  HONEYPOT CHECK: V3 buy quote returned 0 — SKIP')
                    return False
                # Quote sell direction
                try:
                    sell_result = quoter_v2.functions.quoteExactInputSingle(
                        (ca, weth, tokens_out, fee, 0)).call()
                    sell_out = sell_result[0]
                except Exception:
                    sell_out = 0

            if sell_out == 0:
                logger.warning('  HONEYPOT CHECK: sell quote returned 0 — HONEYPOT! BLACKLISTING %s', addr[:10])
                return False

            # Roundtrip sanity check: should get back at least 50% (accounts for fees/slippage)
            roundtrip = sell_out / test_buy * 100 if test_buy > 0 else 0
            if roundtrip < 50:
                logger.warning('  HONEYPOT CHECK: roundtrip %.1f%% — excessive tax — SKIP %s', roundtrip, addr[:10])
                return False
            logger.info('  HONEYPOT CHECK: V3 roundtrip OK (%.1f%%) — SAFE', roundtrip)
            return True

        else:
            # Pons: The swap contract returns EMPTY bytes from eth_call() — no
            # return data at all — so we cannot read tokens_out from the call
            # result.  Use estimate_gas() instead: success = swap would work.
            # (Codex Ultra fix — Sep 7 2026)

            # ── BUY check: estimate_gas on a real-sized Pons swap ──
            steps_buy = [{'stepType':2,'tokenIn':Z,'tokenOut':ca,'pool':Z,'fee':0,'tickSpacing':200,'hook':HOOK,'hookData':b'','recipient':POSMGR,'poolId':b'\x00'*32}]
            test_wei = Web3.to_wei(0.0005, 'ether')
            buy_tx = sc.functions.swap(steps_buy, Z, test_wei, 0, int(time.time())+300).build_transaction(
                {'from':W,'value':test_wei,'nonce':w3.eth.get_transaction_count(W),
                 'gas':500000,'gasPrice':int(w3.eth.gas_price*2),'chainId':CID})
            try:
                buy_gas = w3.eth.estimate_gas(buy_tx)
            except Exception as bex:
                logger.warning('  HONEYPOT CHECK: Pons buy estimate_gas FAILED: %s — INCONCLUSIVE', str(bex)[:80])
                return False  # inconclusive — block but caller must NOT blacklist

            logger.info('  HONEYPOT CHECK: Pons buy estimate_gas OK (%d gas)', buy_gas)

            # ── SELL check: estimate_gas with state override ──
            # Inject a fake token balance + approval so the sell sim has tokens
            # to sell.  Use a large synthetic amount (10^18 raw units).
            synthetic_tokens = 10**18
            steps_sell = [{'stepType':2,'tokenIn':ca,'tokenOut':Z,'pool':Z,'fee':0,'tickSpacing':200,'hook':HOOK,'hookData':b'','recipient':POSMGR,'poolId':b'\x00'*32}]
            sell_tx = sc.functions.swap(steps_sell, Z, synthetic_tokens, 0, int(time.time())+300).build_transaction(
                {'from':W,'value':0,'nonce':w3.eth.get_transaction_count(W),
                 'gas':500000,'gasPrice':int(w3.eth.gas_price*2),'chainId':CID})

            # Build state override: inject balance at slot 0, approval at slot 1
            balance_slot = Web3.keccak(
                bytes.fromhex(W[2:].lower().zfill(64)) +
                (0).to_bytes(32, 'big')  # slot 0 — most common for balanceOf
            )
            approval_slot = Web3.keccak(
                bytes.fromhex(AGG[2:].lower().zfill(64)) +
                Web3.keccak(
                    bytes.fromhex(W[2:].lower().zfill(64)) +
                    (1).to_bytes(32, 'big')  # slot 1 — most common for allowances
                )
            )
            big_bal = (10**30).to_bytes(32, 'big')
            max_approval = ((2**256 - 1).to_bytes(32, 'big'))
            state_override = {
                ca: {
                    'stateDiff': {
                        '0x' + balance_slot.hex(): '0x' + big_bal.hex(),
                        '0x' + approval_slot.hex(): '0x' + max_approval.hex(),
                    }
                }
            }

            try:
                sell_gas = w3.eth.estimate_gas(sell_tx, 'latest', state_override)
                logger.info('  HONEYPOT CHECK: Pons sell estimate_gas OK (%d gas) — SAFE', sell_gas)
                return True
            except Exception as sex:
                err_str = str(sex)[:120]
                if 'revert' in err_str.lower() or 'execution reverted' in err_str.lower():
                    logger.warning('  🚨 HONEYPOT DETECTED: Pons sell reverted — BLACKLISTING %s: %s', addr[:10], err_str)
                    PONS_BLACKLIST.add(addr.lower())
                    _save_blacklist()
                    return False
                # Non-revert error (state override unsupported, RPC glitch) — INCONCLUSIVE
                # Block entry but do NOT blacklist — token can retry next scan.
                logger.warning('  HONEYPOT CHECK: Pons sell sim inconclusive: %s — BLOCKING (not blacklisting)', err_str)
                return False

    except Exception as ex:
        logger.warning('  🚨 HONEYPOT CHECK FAILED: %s — INCONCLUSIVE (not blacklisting)', str(ex)[:80])
        return False


def v3_ok(addr):
    """Check if token can be swapped via Uniswap V3 (WETH pair ONLY).

    CRITICAL: Must verify the DexScreener pair is actually WETH-denominated.
    Gas estimation alone is NOT sufficient — the router accepts non-WETH pairs
    but returns 0 tokens (learned the hard way with hoodrat/USDG).
    """
    # Step 1: Check pair data to verify quote token is WETH/ETH
    # Use cached pair data from scan() if available (saves ~1s API call)
    al = addr.lower()
    cached = _pair_data_cache.get(al)
    if cached and (time.time() - cached['ts']) < 120:  # Cache valid for 2 min
        qt_addr = cached['quote_addr']
        qt_sym = cached['quote_sym']
    else:
        # Fallback: fetch from DexScreener (only if cache miss)
        d = fetch(f'https://api.dexscreener.com/latest/dex/tokens/{addr}')
        if not d:
            return False
        qt_addr = ''
        qt_sym = '?'
        for p in (d.get('pairs') or []):
            if p.get('chainId') != 'robinhood':
                continue
            qt = p.get('quoteToken', {})
            qt_addr = (qt.get('address') or '').lower()
            qt_sym = (qt.get('symbol') or '').upper()
            # Cache pair data for v4_ok (saves re-fetch)
            _pair_data_cache[al] = {
                'quote_sym': qt_sym,
                'quote_addr': qt_addr,
                'pair_addr': p.get('pairAddress', ''),
                'ts': time.time(),
            }
            break
    weth_pair = (qt_addr == WETH_ADDR.lower() or qt_sym in ('WETH', 'ETH'))
    spy_pair = qt_addr in ACCEPTED_QUOTES
    # Also accept any enabled quote token from the registry
    enabled_quote = qt_addr in QUOTE_TOKENS and QUOTE_ENABLED.get(qt_addr, False)
    if not weth_pair and not spy_pair and not enabled_quote:
        logger.info('  %s: V3 REJECTED — not WETH/SPY/enabled-quote-paired (quote=%s)', addr[:10], qt_sym)
        return False

    # Step 2: Simulate the swap via eth_call to verify we'd get tokens back
    # Gas estimation alone is NOT enough — V3 pools can execute with 0 output
    # if concentrated liquidity is out of range.
    ca = Web3.to_checksum_address(addr)

    if spy_pair:
        # SPY-paired token: simulate SPY → token swap
        quote_sym, quote_fee = ACCEPTED_QUOTES[qt_addr]
        spy_ca = Web3.to_checksum_address(SPY_ADDR)
        test_amt = int(0.0001 * 1e18)  # 0.0001 SPY
        try:
            params = (spy_ca, ca, quote_fee, W, test_amt, 0, 0)
            tx = v3r.functions.exactInputSingle(params).build_transaction(
                {'from':W,'value':0,'nonce':w3.eth.get_transaction_count(W),
                 'gas':500000,'gasPrice':int(w3.eth.gas_price*2),'chainId':CID})
            result = w3.eth.call(tx)
            out_amt = int.from_bytes(result[:32], 'big') if len(result) >= 32 else 0
            if out_amt > 0:
                TOKEN_ROUTE[addr.lower()] = f'v3spy_{quote_fee}'
                logger.info('  %s: V3-SPY fee=%d VERIFIED (output=%d for 0.0001 SPY)', addr[:10], quote_fee, out_amt)
                return True
            logger.info('  %s: V3-SPY fee=%d output=0 — SKIP', addr[:10], quote_fee)
        except:
            pass
        return False

    # WETH-paired: existing logic
    weth = Web3.to_checksum_address(WETH_ADDR)
    test_amt = Web3.to_wei(0.0001, 'ether')
    for fee in V3_FEES:
        try:
            # SwapRouter02 params: no deadline in tuple
            params = (weth, ca, fee, W, test_amt, 0, 0)
            tx = v3r.functions.exactInputSingle(params).build_transaction(
                {'from':W,'value':test_amt,'nonce':w3.eth.get_transaction_count(W),
                 'gas':500000,'gasPrice':int(w3.eth.gas_price*2),'chainId':CID})
            # Use eth_call to simulate and get actual output amount
            result = w3.eth.call(tx)
            out_amt = int.from_bytes(result[:32], 'big') if len(result) >= 32 else 0
            if out_amt == 0:
                logger.info('  %s: V3 fee=%d simulated OK but output=0 — SKIP', addr[:10], fee)
                continue
            TOKEN_ROUTE[addr.lower()] = f'v3_{fee}'
            logger.info('  %s: V3 fee=%d VERIFIED (output=%d for 0.0001 ETH)', addr[:10], fee, out_amt)
            return True
        except:
            pass
    return False


def _unwrap_all_weth(max_amount=None):
    """Unwrap WETH back to native ETH. Called on V3/V4 failures and sell success.
    If max_amount (wei) is provided, unwraps at most that amount instead of entire
    balance — prevents consuming unrelated WETH from other operations."""
    try:
        weth_erc = w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDR), abi=E20)
        weth_bal = weth_erc.functions.balanceOf(W).call()
        if weth_bal > 0:
            unwrap_amt = min(weth_bal, max_amount) if max_amount is not None and max_amount > 0 else weth_bal
            logger.info('Unwrapping %s WETH → ETH (of %s total)',
                        Web3.from_wei(unwrap_amt, 'ether'), Web3.from_wei(weth_bal, 'ether'))
            nonce, op, _gen = _acquire_nonce('unwrap')
            try:
                tx = weth_c.functions.withdraw(unwrap_amt).build_transaction(
                    {'from': W, 'nonce': nonce,
                     'gas': 60000, 'gasPrice': int(w3.eth.gas_price * 3), 'chainId': CID})
            except Exception as build_ex:
                logger.error('WETH unwrap build_tx failed: %s — releasing nonce', build_ex)
                _tx_coord.fail_op(op.op_id, f'unwrap build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return
            r = _sign_and_send(tx, op, timeout=60, generation=_gen)
            if r and r['status'] == 1:
                logger.info('WETH unwrapped OK. ETH: %.6f', eth_bal())
            else:
                logger.error('WETH unwrap REVERTED or TIMEOUT')
    except Exception as ex:
        logger.error('WETH unwrap ERR: %s', ex)


def v3_buy(addr, eth_amt, sym, market_price=0):
    """Buy token via V3 SwapRouter02: wrap ETH → WETH, then swap WETH → token."""
    route = TOKEN_ROUTE.get(addr.lower(), 'v3_10000')
    fee = int(route.split('_')[1])
    wei = Web3.to_wei(eth_amt, 'ether')
    logger.info('V3 BUY %s with %.6f ETH ($%.2f) fee=%d', sym, eth_amt, eth_amt*ETH_USD, fee)
    # ═══ FIX 1: amountOutMin for V3 buy ═══
    min_out = 0
    if market_price > 0:
        try:
            tc = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=E20)
            d = tc.functions.decimals().call()
        except Exception:
            d = 18
        expected_tokens = (eth_amt * ETH_USD) / market_price
        min_tokens = expected_tokens * (1 - MAX_SLIPPAGE / 100)
        min_out = int(min_tokens * (10 ** d))
        logger.info('  V3 amountOutMin: %.0f tokens (%d%% tolerance)', min_tokens, MAX_SLIPPAGE)
    try:
        # Snapshot pre-buy token balance for delta calculation
        pre_r, pre_d = bal(addr)
        # Wrap ETH → WETH first
        nonce_w, op_w, _gen = _acquire_nonce('wrap', token_addr=addr, symbol=sym)
        try:
            wrap_tx = weth_c.functions.deposit().build_transaction(
                {'from':W,'value':wei,'nonce':nonce_w,
                 'gas':60000,'gasPrice':int(w3.eth.gas_price*3),'chainId':CID})
        except Exception as build_ex:
            logger.error('V3 BUY %s: wrap build_tx failed: %s — releasing nonce', sym, build_ex)
            _tx_coord.fail_op(op_w.op_id, f'v3 wrap build failed: {build_ex}')
            _tx_coord.resync_nonce()
            return 0, 0
        rc_w = _sign_and_send(wrap_tx, op_w, timeout=60, generation=_gen)
        if rc_w is None:
            logger.warning('V3 BUY %s: WETH wrap TIMEOUT — aborting', sym)
            return 0, 0

        # Approve WETH for V3 Router
        weth_erc = w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDR), abi=E20)
        allowance = weth_erc.functions.allowance(W, Web3.to_checksum_address(V3_ROUTER)).call()
        if allowance < wei:
            nonce_a, op_a, _gen = _acquire_nonce('approve', token_addr=WETH_ADDR, symbol='WETH')
            try:
                appr_tx = weth_erc.functions.approve(Web3.to_checksum_address(V3_ROUTER), 2**256-1).build_transaction(
                    {'from':W,'nonce':nonce_a,'gas':100000,
                     'gasPrice':int(w3.eth.gas_price*3),'chainId':CID})
            except Exception as build_ex:
                logger.error('V3 BUY %s: WETH approve build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_a.op_id, f'v3 weth approve build failed: {build_ex}')
                _tx_coord.resync_nonce()
                _unwrap_all_weth(max_amount=wei)
                return 0, 0
            rc_a = _sign_and_send(appr_tx, op_a, timeout=60, generation=_gen)
            if rc_a is None:
                logger.warning('V3 BUY %s: WETH approve TIMEOUT — aborting', sym)
                _unwrap_all_weth(max_amount=wei)
                return 0, 0

        # Swap WETH → token via multicall (deadline-protected)
        ca = Web3.to_checksum_address(addr)
        weth = Web3.to_checksum_address(WETH_ADDR)
        params = (weth, ca, fee, W, wei, min_out, 0)
        swap_calldata = v3r.encode_abi('exactInputSingle', args=[params])
        deadline = int(time.time()) + 300
        nonce_s, op_s, _gen = _acquire_nonce('buy', token_addr=addr, symbol=sym)
        try:
            swap_tx = v3r.functions.multicall(deadline, [bytes.fromhex(swap_calldata[2:])]).build_transaction(
                {'from':W,'value':0,'nonce':nonce_s,
                 'gas':400000,'gasPrice':int(w3.eth.gas_price*3),'chainId':CID})
        except Exception as build_ex:
            logger.error('V3 BUY %s: swap build_tx failed: %s — releasing nonce', sym, build_ex)
            _tx_coord.fail_op(op_s.op_id, f'v3 buy swap build failed: {build_ex}')
            _tx_coord.resync_nonce()
            _unwrap_all_weth(max_amount=wei)
            return 0, 0
        rc = _sign_and_send(swap_tx, op_s, timeout=120, generation=_gen)
        if rc is None:
            logger.warning('V3 BUY %s: swap TIMEOUT — will reconcile', sym)
            return 0, 0
        if rc['status'] == 1:
            post_r, post_d = bal(addr)
            tb = (post_r - pre_r) / (10**post_d)
            if tb <= 0:
                # Swap succeeded on-chain but returned 0 tokens — BAD PAIR
                logger.error('V3 BUY %s: swap ok but 0 tokens received — unwrapping WETH', sym)
                _unwrap_all_weth(max_amount=wei)
                PONS_BLACKLIST.add(addr.lower())  # Never try this token again
                _save_blacklist()
                return 0, 0
            logger.info('V3 BOUGHT %s: %s tokens', sym, f'{tb:,.0f}')
            # Pre-approve token for V3 Router (for selling later) — non-fatal
            try:
                tok_c = w3.eth.contract(address=ca, abi=E20)
                a = tok_c.functions.allowance(W, Web3.to_checksum_address(V3_ROUTER)).call()
                if a < 10**30:
                    nonce_pa, op_pa, _gen = _acquire_nonce('approve', token_addr=addr, symbol=sym)
                    try:
                        appr_tx = tok_c.functions.approve(Web3.to_checksum_address(V3_ROUTER), 2**256-1).build_transaction(
                            {'from':W,'nonce':nonce_pa,'gas':100000,
                             'gasPrice':int(w3.eth.gas_price*3),'chainId':CID})
                    except Exception as build_ex:
                        logger.error('V3 BUY %s: post-buy approve build_tx failed: %s — releasing nonce', sym, build_ex)
                        _tx_coord.fail_op(op_pa.op_id, f'post-buy approve build failed: {build_ex}')
                        _tx_coord.resync_nonce()
                        raise  # Re-raise to outer except (non-fatal)
                    _sign_and_send(appr_tx, op_pa, timeout=60, generation=_gen)
            except Exception as appr_ex:
                logger.error('V3 BUY %s: post-buy approval failed (non-fatal): %s', sym, appr_ex)
            entry_usd = eth_amt * ETH_USD / tb if tb > 0 else 0
            return tb, entry_usd
        logger.error('V3 BUY %s REVERTED — unwrapping WETH', sym)
        _unwrap_all_weth(max_amount=wei)
        return 0, 0
    except Exception as ex:
        logger.error('V3 BUY %s ERR: %s — unwrapping WETH', sym, ex)
        _unwrap_all_weth(max_amount=wei)
        return 0, 0


def v3_sell(addr, sym, market_price=0, managed_raw=0, sell_quote=None):
    """Sell token via V3 SwapRouter02: token → WETH, then unwrap WETH → ETH."""
    route = TOKEN_ROUTE.get(addr.lower(), 'v3_10000')
    fee = int(route.split('_')[1])
    r, d = bal(addr)
    if r == 0: return 0.0
    # Sell exact managed quantity if provided, otherwise full balance
    if managed_raw > 0 and managed_raw <= r:
        r = managed_raw
    tokens_human = r / (10**d)
    logger.info('V3 SELL %s (%s tokens) fee=%d', sym, f'{tokens_human:,.0f}', fee)
    try:
        ca = Web3.to_checksum_address(addr)
        weth = Web3.to_checksum_address(WETH_ADDR)

        # Approve token for V3 Router FIRST (before computing min_out)
        tok_c = w3.eth.contract(address=ca, abi=E20)
        a = tok_c.functions.allowance(W, Web3.to_checksum_address(V3_ROUTER)).call()
        if a < r:
            nonce_a, op_a, _gen = _acquire_nonce('approve', token_addr=addr, symbol=sym)
            try:
                appr_tx = tok_c.functions.approve(Web3.to_checksum_address(V3_ROUTER), 2**256-1).build_transaction(
                    {'from':W,'nonce':nonce_a,'gas':100000,
                     'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})
            except Exception as build_ex:
                logger.error('V3 SELL %s: approve build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_a.op_id, f'v3 sell approve build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return 0.0
            rc_a = _sign_and_send(appr_tx, op_a, timeout=60, generation=_gen)
            if rc_a is None:
                logger.warning('V3 SELL %s: approve TIMEOUT — aborting', sym)
                return 0.0

        # ═══ Executable quote → min_out (integer-safe, 1s freshness AT SIGNING) ═══
        # Computed AFTER approval so the quote is fresh when the swap tx is built.
        # V3 route: quote-only, no price-based fallback — must have executable quote or refuse.
        min_out, used_quote = _quote_min_out(sell_quote, r)
        if used_quote:
            logger.info('  V3 SELL minOut: %d (exec quote, integer-safe)', min_out)
        else:
            # Quote stale or unavailable — re-fetch from on-chain quoter
            if sell_quote and sell_quote.ok:
                logger.info('  V3 SELL: original quote stale (age=%.1fs), re-fetching', time.time() - sell_quote.timestamp)
            try:
                fresh_sq = get_sell_quote(addr, r, sym, route=route)
                min_out, used_quote = _quote_min_out(fresh_sq, r)
                if used_quote:
                    sell_quote = fresh_sq  # Track active quote for signing boundary check
                    logger.info('  V3 SELL minOut: %d (re-fetched exec quote, integer-safe)', min_out)
            except Exception:
                pass

        # Snapshot pre-sell ETH balance for delta calculation
        pre_eth = eth_bal()
        if min_out > 0:
            attempts = [(min_out, f'quote-{EXEC_SLIPPAGE_ALLOW}%')]
        else:
            logger.warning('V3 SELL %s: no valid quote — quote unavailable', sym)
            # No valid quote — do NOT sign or broadcast without execution protection.
            # Return 0 to retain exit intent; caller will retry when quote available.
            logger.error('SELL %s: no executable quote — refusing to sign. Exit intent retained.', sym)
            return 0.0
        for attempt_min, attempt_label in attempts:
            params = (ca, weth, fee, W, r, attempt_min, 0)
            swap_calldata = v3r.encode_abi('exactInputSingle', args=[params])
            deadline = int(time.time()) + 300
            nonce_s, op_s, _gen = _acquire_nonce('sell', token_addr=addr, symbol=sym)
            try:
                swap_tx = v3r.functions.multicall(deadline, [bytes.fromhex(swap_calldata[2:])]).build_transaction(
                    {'from':W,'value':0,'nonce':nonce_s,
                     'gas':400000,'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})
            except Exception as build_ex:
                logger.error('V3 SELL %s: swap build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_s.op_id, f'v3 sell swap build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return 0.0
            # ═══ SIGNING BOUNDARY FRESHNESS CHECK ═══
            # RPCs above (nonce, gas) take time. Revalidate the ACTIVE quote (sell_quote,
            # which tracks refetches) before signing — not the original caller quote.
            if sell_quote and sell_quote.ok:
                signing_age = time.time() - sell_quote.timestamp
                if signing_age > EXEC_QUOTE_MAX_AGE:
                    logger.warning('V3 SELL %s: quote stale at signing (age=%.1fs > %.1fs) — re-quoting',
                                   sym, signing_age, EXEC_QUOTE_MAX_AGE)
                    try:
                        fresh_sq = get_sell_quote(addr, r, sym, route=route)
                        fresh_min, fresh_ok = _quote_min_out(fresh_sq, r)
                        if fresh_ok and fresh_min > 0:
                            sell_quote = fresh_sq  # Update active quote
                            attempt_min = fresh_min
                            # Rebuild tx with fresh min_out
                            params = (ca, weth, fee, W, r, attempt_min, 0)
                            swap_calldata = v3r.encode_abi('exactInputSingle', args=[params])
                            swap_tx = v3r.functions.multicall(deadline, [bytes.fromhex(swap_calldata[2:])]).build_transaction(
                                {'from':W,'value':0,'nonce':nonce_s,
                                 'gas':400000,'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})
                            logger.info('V3 SELL %s: rebuilt tx with fresh quote minOut=%d', sym, attempt_min)
                        else:
                            logger.error('V3 SELL %s: re-quote failed — refusing to sign stale', sym)
                            _tx_coord.fail_op(op_s.op_id, 'signing boundary re-quote failed')
                            _tx_coord.resync_nonce()  # Reclaim nonce gap
                            return 0.0
                    except Exception as rq_ex:
                        logger.error('V3 SELL %s: re-quote exception: %s — refusing to sign', sym, rq_ex)
                        _tx_coord.fail_op(op_s.op_id, f'signing boundary re-quote error: {rq_ex}')
                        _tx_coord.resync_nonce()  # Reclaim nonce gap
                        return 0.0
            rc = _sign_and_send(swap_tx, op_s, timeout=120, generation=_gen)
            if rc is None:
                logger.warning('V3 SELL %s TIMEOUT (%s) — will reconcile', sym, attempt_label)
                return 0.0
            if rc['status'] == 1:
                # Unwrap WETH → ETH
                _unwrap_all_weth()
                received = eth_bal() - pre_eth
                logger.info('V3 SOLD %s → %.6f ETH ($%.2f) [%s]', sym, received, received*ETH_USD, attempt_label)
                update_bal()
                return received
            logger.error('V3 SELL %s REVERTED (%s) — caller will retry next tick with fresh quote', sym, attempt_label)
        return 0.0
    except Exception as ex:
        logger.error('V3 SELL %s ERR: %s', sym, ex)
        return 0.0


def spy_bal():
    """Get SPY token balance in human-readable units.
    Returns 0.0 on RPC failure (backward compat for non-critical callers)."""
    try:
        return spy_bal_strict()
    except Exception:
        return 0.0


def spy_bal_strict():
    """Get SPY token balance — RAISES on RPC failure (use in delta calculations).
    Callers MUST handle the exception to avoid fabricated proceeds."""
    spy_c = w3.eth.contract(address=Web3.to_checksum_address(SPY_ADDR), abi=E20)
    raw = spy_c.functions.balanceOf(W).call()
    return raw / 1e18  # SPY has 18 decimals


def spy_bal_usd():
    """Get SPY balance in USD using QuoterV2 (SPY → WETH → USD)."""
    sb = spy_bal()
    if sb <= 0:
        return 0.0
    try:
        # Quote SPY → WETH to get USD value
        spy_raw = int(sb * 1e18)
        result = quoter_v2.functions.quoteExactInputSingle(
            (Web3.to_checksum_address(SPY_ADDR), Web3.to_checksum_address(WETH_ADDR),
             spy_raw, 500, 0)).call()
        weth_out = result[0]
        return float(Web3.from_wei(weth_out, 'ether')) * ETH_USD
    except:
        return sb * 750  # Fallback rough estimate


USDG_ADDR = '0x5fc5360D0400a0Fd4f2af552ADD042d716F1d168'


def usdg_bal():
    """Get USDG token balance in USD (USDG is a stablecoin, 6 decimals, 1:1 USD)."""
    try:
        usdg_c = w3.eth.contract(address=Web3.to_checksum_address(USDG_ADDR), abi=E20)
        raw = usdg_c.functions.balanceOf(W).call()
        return raw / 1e6  # USDG has 6 decimals, pegged 1:1 to USD
    except:
        return 0.0


# ══════════════════════════════════════════════════════════════════════
# EXECUTABLE SELL QUOTE — actual quoter/router calls for managed quantity
# ══════════════════════════════════════════════════════════════════════

class SellQuote:
    """Structured result from get_sell_quote(). Carries token identity,
    raw quantities, route, block, timestamp, and quote status through
    to signing and PnL decisions."""
    __slots__ = ('ok', 'proceeds_usd', 'proceeds_raw', 'quote_sym', 'quote_dec',
                 'token_addr', 'token_raw', 'route', 'pool_key', 'block',
                 'timestamp', 'gas_estimate', 'error')

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))
        if self.ok is None:
            self.ok = False
        if self.timestamp is None:
            self.timestamp = time.time()

    @property
    def net_pnl_pct(self):
        """Cannot compute PnL without cost basis — caller must compute."""
        return None

    def __repr__(self):
        return (f'SellQuote(ok={self.ok}, proceeds_usd={self.proceeds_usd}, '
                f'route={self.route}, quote_sym={self.quote_sym})')


def get_sell_quote(addr, managed_raw, sym, route=None):
    """Get an executable sell quote for the exact managed quantity.

    Uses actual quoter/router call — NOT chart_price × tokens.
    Returns SellQuote with proceeds_usd, proceeds_raw, route info, block, timestamp.

    Args:
        addr: Token address (DexScreener or V4 on-chain)
        managed_raw: Exact managed raw token quantity (integer, in smallest unit)
        sym: Token symbol for logging
        route: Override route string (default: look up TOKEN_ROUTE)

    Returns:
        SellQuote with ok=True if quote succeeded, ok=False otherwise.
    """
    if route is None:
        route = TOKEN_ROUTE.get(addr.lower(), 'pons')
    ts = time.time()

    try:
        blk = w3.eth.block_number
    except Exception:
        blk = 0

    base = dict(token_addr=addr, token_raw=managed_raw, route=route,
                block=blk, timestamp=ts)

    if managed_raw <= 0:
        return SellQuote(ok=False, error='zero managed quantity', gas_estimate=0, **base)

    try:
        if route.startswith('v4'):
            pool_data = _v4_pool_cache.get(addr.lower())
            if not pool_data:
                return SellQuote(ok=False, error='no V4 pool cache', **base)

            pool_key = pool_data['pool_key']
            quote_sym = pool_data['quote_sym']
            quote_dec = pool_data['quote_dec']
            quote_addr = pool_data['quote_addr']
            c0, c1 = pool_data['c0'], pool_data['c1']
            qt_is_c0 = (quote_addr == c0.lower())

            # V4 token address may differ from DexScreener address
            v4_token = c1 if qt_is_c0 else c0

            # Sell direction: token → quote
            sell_zfo = not qt_is_c0

            result = v4_quoter.functions.quoteExactInputSingle(
                (pool_key, sell_zfo, managed_raw, b'')).call()
            amount_out = result[0]
            gas_est = result[1] if len(result) > 1 else 0

            if amount_out <= 0:
                return SellQuote(ok=False, error='quoter returned 0', quote_sym=quote_sym,
                                 quote_dec=quote_dec, gas_estimate=gas_est, **base)

            proceeds_human = amount_out / (10 ** quote_dec)
            if quote_sym == 'ETH':
                proceeds_usd = proceeds_human * ETH_USD
            elif quote_sym == 'USDG':
                proceeds_usd = proceeds_human  # 1:1 USD
            elif quote_sym == 'SPY':
                try:
                    spy_price = spy_bal_usd() / spy_bal() if spy_bal() > 0 else 750
                except Exception:
                    spy_price = 750
                proceeds_usd = proceeds_human * spy_price
            else:
                proceeds_usd = proceeds_human  # Best effort

            return SellQuote(ok=True, proceeds_usd=proceeds_usd, proceeds_raw=amount_out,
                             quote_sym=quote_sym, quote_dec=quote_dec,
                             pool_key=pool_key, gas_estimate=gas_est, **base)

        elif route.startswith('v3spy'):
            # V3 SPY route: token → SPY via V3
            fee = int(route.split('_')[1])
            ca = Web3.to_checksum_address(addr)
            spy_ca = Web3.to_checksum_address(SPY_ADDR)
            _, d = bal(addr)
            if d == 0:
                d = 18

            result = quoter_v2.functions.quoteExactInputSingle(
                (ca, spy_ca, managed_raw, fee, 0)).call()
            spy_out = result[0]
            gas_est = result[3] if len(result) > 3 else 0

            if spy_out <= 0:
                return SellQuote(ok=False, error='V3 SPY quoter returned 0',
                                 quote_sym='SPY', quote_dec=18, gas_estimate=gas_est, **base)

            spy_human = spy_out / 1e18
            try:
                spy_price = spy_bal_usd() / spy_bal() if spy_bal() > 0 else 750
            except Exception:
                spy_price = 750
            proceeds_usd = spy_human * spy_price

            return SellQuote(ok=True, proceeds_usd=proceeds_usd, proceeds_raw=spy_out,
                             quote_sym='SPY', quote_dec=18, gas_estimate=gas_est, **base)

        elif route.startswith('v3'):
            # V3 WETH route: token → WETH
            fee = int(route.split('_')[1])
            ca = Web3.to_checksum_address(addr)
            weth = Web3.to_checksum_address(WETH_ADDR)

            result = quoter_v2.functions.quoteExactInputSingle(
                (ca, weth, managed_raw, fee, 0)).call()
            weth_out = result[0]
            gas_est = result[3] if len(result) > 3 else 0

            if weth_out <= 0:
                return SellQuote(ok=False, error='V3 WETH quoter returned 0',
                                 quote_sym='ETH', quote_dec=18, gas_estimate=gas_est, **base)

            eth_human = float(Web3.from_wei(weth_out, 'ether'))
            proceeds_usd = eth_human * ETH_USD

            return SellQuote(ok=True, proceeds_usd=proceeds_usd, proceeds_raw=weth_out,
                             quote_sym='ETH', quote_dec=18, gas_estimate=gas_est, **base)

        else:
            # Pons route: cannot get executable quotes (eth_call returns empty)
            # Fall back to chart price estimate — clearly marked as non-executable
            return SellQuote(ok=False, error='pons route: no executable quote available',
                             quote_sym='ETH', quote_dec=18, gas_estimate=0, **base)

    except Exception as ex:
        return SellQuote(ok=False, error=f'quote exception: {str(ex)[:100]}',
                         quote_sym='ETH', quote_dec=18, gas_estimate=0, **base)


def get_rt_cost_model(addr, deploy_amt, managed_raw, sym, market_price, route=None, liquidity=0):
    """Calculate actual modeled round-trip cost at proposed size.

    Instead of the trivially-equivalent 2×slip, this:
    1. Gets a buy quote (tokens out for deploy_amt)
    2. Gets a sell quote (proceeds for those tokens)
    3. Computes net recovery = sell_proceeds / deploy_cost
    4. RT cost = 1 - net_recovery (includes fees, impact, wrapping)

    Returns (ok, expected_rt_pct, bounded_rt_pct, buy_tokens_raw, sell_proceeds_usd).
    """
    if route is None:
        route = TOKEN_ROUTE.get(addr.lower(), 'pons')

    try:
        if route.startswith('v4'):
            pool_data = _v4_pool_cache.get(addr.lower())
            if not pool_data:
                return False, 100.0, 100.0, 0, 0

            pool_key = pool_data['pool_key']
            quote_dec = pool_data['quote_dec']
            c0 = pool_data['c0']
            qt_is_c0 = (pool_data['quote_addr'] == c0.lower())
            buy_zfo = qt_is_c0

            # Buy simulation: quote → token
            buy_raw = int(deploy_amt * (10 ** quote_dec)) if pool_data['quote_sym'] != 'ETH' else Web3.to_wei(deploy_amt, 'ether')
            buy_result = v4_quoter.functions.quoteExactInputSingle(
                (pool_key, buy_zfo, buy_raw, b'')).call()
            tokens_out = buy_result[0]
            if tokens_out <= 0:
                return False, 100.0, 100.0, 0, 0

            # Sell simulation: token → quote (exact tokens we'd receive from buy)
            sell_zfo = not buy_zfo
            sell_result = v4_quoter.functions.quoteExactInputSingle(
                (pool_key, sell_zfo, tokens_out, b'')).call()
            quote_back = sell_result[0]
            if quote_back <= 0:
                return False, 100.0, 100.0, tokens_out, 0

            # Net recovery ratio
            recovery = quote_back / buy_raw if buy_raw > 0 else 0
            expected_rt_pct = (1.0 - recovery) * 100
            # Bounded: add execution slippage tolerance both ways
            bounded_rt_pct = expected_rt_pct + (EXEC_SLIPPAGE_ALLOW * 2)

            # Convert sell proceeds to USD
            if pool_data['quote_sym'] == 'ETH':
                sell_usd = float(Web3.from_wei(quote_back, 'ether')) * ETH_USD
            elif pool_data['quote_sym'] == 'USDG':
                sell_usd = quote_back / (10 ** quote_dec)
            elif pool_data['quote_sym'] == 'SPY':
                try:
                    spy_price = spy_bal_usd() / spy_bal() if spy_bal() > 0 else 750
                except Exception:
                    spy_price = 750
                sell_usd = (quote_back / (10 ** quote_dec)) * spy_price
            else:
                sell_usd = quote_back / (10 ** quote_dec)

            return True, expected_rt_pct, bounded_rt_pct, tokens_out, sell_usd

        elif route.startswith('v3'):
            fee = int(route.split('_')[1])
            ca = Web3.to_checksum_address(addr)
            weth = Web3.to_checksum_address(WETH_ADDR)
            if route.startswith('v3spy'):
                weth = Web3.to_checksum_address(SPY_ADDR)

            wei = Web3.to_wei(deploy_amt, 'ether')
            # Buy sim: WETH/SPY → token
            buy_result = quoter_v2.functions.quoteExactInputSingle(
                (weth, ca, wei, fee, 0)).call()
            tokens_out = buy_result[0]
            if tokens_out <= 0:
                return False, 100.0, 100.0, 0, 0

            # Sell sim: token → WETH/SPY
            sell_result = quoter_v2.functions.quoteExactInputSingle(
                (ca, weth, tokens_out, fee, 0)).call()
            quote_back = sell_result[0]
            if quote_back <= 0:
                return False, 100.0, 100.0, tokens_out, 0

            recovery = quote_back / wei if wei > 0 else 0
            expected_rt_pct = (1.0 - recovery) * 100
            bounded_rt_pct = expected_rt_pct + (EXEC_SLIPPAGE_ALLOW * 2)

            if route.startswith('v3spy'):
                try:
                    spy_price = spy_bal_usd() / spy_bal() if spy_bal() > 0 else 750
                except Exception:
                    spy_price = 750
                sell_usd = float(Web3.from_wei(quote_back, 'ether')) * spy_price
            else:
                sell_usd = float(Web3.from_wei(quote_back, 'ether')) * ETH_USD

            return True, expected_rt_pct, bounded_rt_pct, tokens_out, sell_usd

        else:
            # Pons: no executable simulation available
            # Fall back to liquidity-based estimation (clearly marked)
            if liquidity >= 10000:
                deploy_usd = deploy_amt * ETH_USD
                est_impact = deploy_usd / liquidity * 1000  # 10x safety
                expected_rt_pct = est_impact * 2  # Buy + sell
                bounded_rt_pct = expected_rt_pct + (EXEC_SLIPPAGE_ALLOW * 2)
                return True, expected_rt_pct, bounded_rt_pct, 0, 0
            return False, 100.0, 100.0, 0, 0

    except Exception as ex:
        logger.warning('RT cost model failed for %s: %s', sym, str(ex)[:80])
        return False, 100.0, 100.0, 0, 0


def v3_buy_spy(addr, spy_amt, sym, market_price=0):
    """Buy token via V3: SPY → token. Same logic as v3_buy but with SPY as quote."""
    route = TOKEN_ROUTE.get(addr.lower(), 'v3spy_500')
    fee = int(route.split('_')[1])
    spy_wei = int(spy_amt * 1e18)
    pre_spy_bal = spy_bal()  # Capture BEFORE swap for accurate entry_usd
    spy_usd = spy_bal_usd()  # For logging
    deploy_usd_pre = spy_amt / pre_spy_bal * spy_usd if pre_spy_bal > 0 else spy_amt * 750  # Pre-swap USD value
    logger.info('V3-SPY BUY %s with %.6f SPY (~$%.2f) fee=%d', sym, spy_amt, spy_amt / pre_spy_bal * spy_usd if pre_spy_bal > 0 else 0, fee)
    # amountOutMin — same 2% tolerance as WETH buys
    min_out = 0
    if market_price > 0:
        try:
            tc = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=E20)
            d = tc.functions.decimals().call()
        except Exception:
            d = 18
        deploy_usd = spy_amt / spy_bal() * spy_usd if spy_bal() > 0 else spy_amt * 750
        expected_tokens = deploy_usd / market_price
        min_tokens = expected_tokens * (1 - MAX_SLIPPAGE / 100)
        min_out = int(min_tokens * (10 ** d))
        logger.info('  V3-SPY amountOutMin: %.0f tokens (%d%% tolerance)', min_tokens, MAX_SLIPPAGE)
    try:
        spy_ca = Web3.to_checksum_address(SPY_ADDR)
        ca = Web3.to_checksum_address(addr)

        # Approve SPY for V3 Router
        spy_c = w3.eth.contract(address=spy_ca, abi=E20)
        allowance = spy_c.functions.allowance(W, Web3.to_checksum_address(V3_ROUTER)).call()
        if allowance < spy_wei:
            nonce_a, op_a, _gen = _acquire_nonce('approve', token_addr=SPY_ADDR, symbol='SPY')
            try:
                appr_tx = spy_c.functions.approve(Web3.to_checksum_address(V3_ROUTER), 2**256-1).build_transaction(
                    {'from':W,'nonce':nonce_a,'gas':100000,
                     'gasPrice':int(w3.eth.gas_price*3),'chainId':CID})
            except Exception as build_ex:
                logger.error('V3-SPY BUY %s: SPY approve build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_a.op_id, f'v3spy buy approve build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return 0, 0
            rc_a = _sign_and_send(appr_tx, op_a, timeout=60, generation=_gen)
            if rc_a is None:
                logger.warning('V3-SPY BUY %s: SPY approve TIMEOUT — aborting', sym)
                return 0, 0

        # Snapshot pre-buy token balance for delta calculation
        pre_r, pre_d = bal(addr)
        # Swap SPY → token via multicall (deadline-protected)
        params = (spy_ca, ca, fee, W, spy_wei, min_out, 0)
        swap_calldata = v3r.encode_abi('exactInputSingle', args=[params])
        deadline = int(time.time()) + 300
        nonce_s, op_s, _gen = _acquire_nonce('buy', token_addr=addr, symbol=sym)
        try:
            swap_tx = v3r.functions.multicall(deadline, [bytes.fromhex(swap_calldata[2:])]).build_transaction(
                {'from':W,'value':0,'nonce':nonce_s,
                 'gas':400000,'gasPrice':int(w3.eth.gas_price*3),'chainId':CID})
        except Exception as build_ex:
            logger.error('V3-SPY BUY %s: swap build_tx failed: %s — releasing nonce', sym, build_ex)
            _tx_coord.fail_op(op_s.op_id, f'v3spy buy swap build failed: {build_ex}')
            _tx_coord.resync_nonce()
            return 0, 0
        rc = _sign_and_send(swap_tx, op_s, timeout=120, generation=_gen)
        if rc is None:
            logger.warning('V3-SPY BUY %s: swap TIMEOUT — will reconcile', sym)
            return 0, 0
        if rc['status'] == 1:
            post_r, post_d = bal(addr)
            tb = (post_r - pre_r) / (10**post_d)
            if tb <= 0:
                logger.error('V3-SPY BUY %s: swap ok but 0 tokens — BLACKLIST', sym)
                PONS_BLACKLIST.add(addr.lower())
                _save_blacklist()
                return 0, 0
            logger.info('V3-SPY BOUGHT %s: %s tokens', sym, f'{tb:,.0f}')
            # Pre-approve token for selling later (non-fatal — sell will retry)
            try:
                tok_c = w3.eth.contract(address=ca, abi=E20)
                a = tok_c.functions.allowance(W, Web3.to_checksum_address(V3_ROUTER)).call()
                if a < 10**30:
                    nonce_pa, op_pa, _gen = _acquire_nonce('approve', token_addr=addr, symbol=sym)
                    try:
                        appr_tx = tok_c.functions.approve(Web3.to_checksum_address(V3_ROUTER), 2**256-1).build_transaction(
                            {'from':W,'nonce':nonce_pa,'gas':100000,
                             'gasPrice':int(w3.eth.gas_price*3),'chainId':CID})
                    except Exception as build_ex:
                        logger.error('V3-SPY BUY %s: post-buy approve build_tx failed: %s — releasing nonce', sym, build_ex)
                        _tx_coord.fail_op(op_pa.op_id, f'post-buy approve build failed: {build_ex}')
                        _tx_coord.resync_nonce()
                        raise  # Re-raise to outer except (non-fatal)
                    _sign_and_send(appr_tx, op_pa, timeout=60, generation=_gen)
            except Exception as appr_ex:
                logger.error('V3-SPY BUY %s: post-buy approval failed (non-fatal): %s', sym, appr_ex)
            entry_usd = deploy_usd_pre / tb if tb > 0 else 0
            return tb, entry_usd
        logger.error('V3-SPY BUY %s REVERTED', sym)
        return 0, 0
    except Exception as ex:
        logger.error('V3-SPY BUY %s ERR: %s', sym, ex)
        return 0, 0


def v3_sell_spy(addr, sym, market_price=0, managed_raw=0, sell_quote=None):
    """Sell token via V3: token → SPY. Same logic as v3_sell but outputs SPY."""
    route = TOKEN_ROUTE.get(addr.lower(), 'v3spy_500')
    fee = int(route.split('_')[1])
    r, d = bal(addr)
    if r == 0: return 0.0
    # Sell exact managed quantity if provided, otherwise full balance
    if managed_raw > 0 and managed_raw <= r:
        r = managed_raw
    tokens_human = r / (10**d)
    logger.info('V3-SPY SELL %s (%s tokens) fee=%d', sym, f'{tokens_human:,.0f}', fee)
    try:
        ca = Web3.to_checksum_address(addr)
        spy_ca = Web3.to_checksum_address(SPY_ADDR)

        # Approve token for V3 Router FIRST (before computing min_out)
        tok_c = w3.eth.contract(address=ca, abi=E20)
        a = tok_c.functions.allowance(W, Web3.to_checksum_address(V3_ROUTER)).call()
        if a < r:
            nonce_a, op_a, _gen = _acquire_nonce('approve', token_addr=addr, symbol=sym)
            try:
                appr_tx = tok_c.functions.approve(Web3.to_checksum_address(V3_ROUTER), 2**256-1).build_transaction(
                    {'from':W,'nonce':nonce_a,'gas':100000,
                     'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})
            except Exception as build_ex:
                logger.error('V3-SPY SELL %s: approve build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_a.op_id, f'v3spy sell approve build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return 0.0
            rc_a = _sign_and_send(appr_tx, op_a, timeout=60, generation=_gen)
            if rc_a is None:
                logger.warning('V3-SPY SELL %s: approve TIMEOUT — aborting', sym)
                return 0.0

        # ═══ Executable quote → min_out (integer-safe, 1s freshness AT SIGNING) ═══
        # V3-SPY route: quote-only, no price-based fallback — must have executable quote or refuse.
        min_out, used_quote = _quote_min_out(sell_quote, r)
        if used_quote:
            logger.info('  V3-SPY SELL minOut: %d (exec quote, integer-safe)', min_out)
        else:
            if sell_quote and sell_quote.ok:
                logger.info('  V3-SPY SELL: original quote stale (age=%.1fs), re-fetching', time.time() - sell_quote.timestamp)
            try:
                fresh_sq = get_sell_quote(addr, r, sym, route=route)
                min_out, used_quote = _quote_min_out(fresh_sq, r)
                if used_quote:
                    sell_quote = fresh_sq  # Track active quote for signing boundary check
                    logger.info('  V3-SPY SELL minOut: %d (re-fetched exec quote, integer-safe)', min_out)
            except Exception:
                pass

        # Snapshot pre-sell SPY balance for delta calculation
        # MUST use spy_bal_strict() — spy_bal() returns 0.0 on RPC failure,
        # which would fabricate positive proceeds (post - 0 = full balance).
        try:
            pre_spy = spy_bal_strict()
        except Exception as snap_ex:
            logger.error('V3-SPY SELL %s: pre-sell SPY balance RPC failed: %s — aborting', sym, snap_ex)
            return 0.0
        if min_out > 0:
            attempts = [(min_out, f'quote-{EXEC_SLIPPAGE_ALLOW}%')]
        else:
            logger.warning('V3-SPY SELL %s: no valid quote — quote unavailable', sym)
            # No valid quote — do NOT sign or broadcast without execution protection.
            # Return 0 to retain exit intent; caller will retry when quote available.
            logger.error('SELL %s: no executable quote — refusing to sign. Exit intent retained.', sym)
            return 0.0
        for attempt_min, attempt_label in attempts:
            params = (ca, spy_ca, fee, W, r, attempt_min, 0)
            swap_calldata = v3r.encode_abi('exactInputSingle', args=[params])
            deadline = int(time.time()) + 300
            nonce_s, op_s, _gen = _acquire_nonce('sell', token_addr=addr, symbol=sym)
            try:
                swap_tx = v3r.functions.multicall(deadline, [bytes.fromhex(swap_calldata[2:])]).build_transaction(
                    {'from':W,'value':0,'nonce':nonce_s,
                     'gas':400000,'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})
            except Exception as build_ex:
                logger.error('V3-SPY SELL %s: swap build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_s.op_id, f'v3spy sell swap build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return 0.0
            # ═══ SIGNING BOUNDARY FRESHNESS CHECK ═══
            # Validate the ACTIVE quote (sell_quote tracks refetches), not just the original.
            if sell_quote and sell_quote.ok:
                signing_age = time.time() - sell_quote.timestamp
                if signing_age > EXEC_QUOTE_MAX_AGE:
                    logger.warning('V3-SPY SELL %s: quote stale at signing (age=%.1fs) — re-quoting',
                                   sym, signing_age)
                    try:
                        fresh_sq = get_sell_quote(addr, r, sym, route=route)
                        fresh_min, fresh_ok = _quote_min_out(fresh_sq, r)
                        if fresh_ok and fresh_min > 0:
                            sell_quote = fresh_sq  # Update active quote
                            attempt_min = fresh_min
                            params = (ca, spy_ca, fee, W, r, attempt_min, 0)
                            swap_calldata = v3r.encode_abi('exactInputSingle', args=[params])
                            swap_tx = v3r.functions.multicall(deadline, [bytes.fromhex(swap_calldata[2:])]).build_transaction(
                                {'from':W,'value':0,'nonce':nonce_s,
                                 'gas':400000,'gasPrice':int(w3.eth.gas_price*5),'chainId':CID})
                            logger.info('V3-SPY SELL %s: rebuilt tx with fresh quote minOut=%d', sym, attempt_min)
                        else:
                            logger.error('V3-SPY SELL %s: re-quote failed — refusing to sign stale', sym)
                            _tx_coord.fail_op(op_s.op_id, 'signing boundary re-quote failed')
                            _tx_coord.resync_nonce()  # Reclaim nonce gap
                            return 0.0
                    except Exception as rq_ex:
                        logger.error('V3-SPY SELL %s: re-quote exception: %s — refusing to sign', sym, rq_ex)
                        _tx_coord.fail_op(op_s.op_id, f'signing boundary re-quote error: {rq_ex}')
                        _tx_coord.resync_nonce()  # Reclaim nonce gap
                        return 0.0
            rc = _sign_and_send(swap_tx, op_s, timeout=120, generation=_gen)
            if rc is None:
                logger.warning('V3-SPY SELL %s TIMEOUT (%s) — will reconcile', sym, attempt_label)
                return 0.0
            if rc['status'] == 1:
                try:
                    post_spy = spy_bal_strict()
                except Exception as spy_ex:
                    logger.error('V3-SPY SELL %s: post-sell SPY balance RPC failed: %s — cannot compute proceeds', sym, spy_ex)
                    return 0.0  # Caller treats as UNRESOLVED — will verify next tick
                received_spy = post_spy - pre_spy
                try:
                    spy_price = spy_bal_usd() / post_spy if post_spy > 0 else 750
                except Exception:
                    spy_price = 750
                received_usd = received_spy * spy_price
                eth_equiv = received_usd / ETH_USD if ETH_USD > 0 else 0
                logger.info('V3-SPY SOLD %s → %.6f SPY (~$%.2f) [%s]', sym, received_spy, received_usd, attempt_label)
                update_bal()
                return eth_equiv
            logger.error('V3-SPY SELL %s REVERTED (%s) — caller will retry next tick with fresh quote', sym, attempt_label)
        return 0.0
    except Exception as ex:
        logger.error('V3-SPY SELL %s ERR: %s', sym, ex)
        return 0.0


def _v4_pool_key(token_addr, quote_addr, fee, tick_spacing, hooks=Z):
    """Build a PoolKey tuple for V4. currency0 must be < currency1 (sorted)."""
    a = Web3.to_checksum_address(token_addr)
    b = Web3.to_checksum_address(quote_addr)
    if int(a, 16) < int(b, 16):
        return (a, b, fee, tick_spacing, Web3.to_checksum_address(hooks))
    else:
        return (b, a, fee, tick_spacing, Web3.to_checksum_address(hooks))


def _v4_zero_for_one(token_addr, quote_addr):
    """For a buy: we send quote (WETH/SPY) and receive token.
    zeroForOne = True if we're selling currency0 for currency1.
    For a sell: vice versa."""
    return int(Web3.to_checksum_address(quote_addr), 16) < int(Web3.to_checksum_address(token_addr), 16)


def _v4_pool_id(pool_key):
    """Compute PoolManager pool ID = keccak256(abi.encode(PoolKey))."""
    from eth_abi import encode
    encoded = encode(
        ['address', 'address', 'uint24', 'int24', 'address'],
        list(pool_key)
    )
    return Web3.keccak(encoded)


def _resolve_v4_pool(dex_token_addr, pool_id_hex):
    """Resolve a V4 pool from its DexScreener pool ID.
    Queries the V4 PoolManager Initialize event to get the exact PoolKey.
    Returns dict with pool_key, c0, c1, quote_sym, quote_dec or None."""
    al = dex_token_addr.lower()
    if al in _v4_pool_cache:
        return _v4_pool_cache[al]

    try:
        pool_id_bytes = bytes.fromhex(pool_id_hex[2:] if pool_id_hex.startswith('0x') else pool_id_hex)

        # Verify pool exists via StateView
        slot0 = v4_state.functions.getSlot0(pool_id_bytes).call()
        if slot0[0] == 0:  # sqrtPriceX96 == 0 means not initialized
            return None
        liq = v4_state.functions.getLiquidity(pool_id_bytes).call()
        if liq == 0:
            logger.info('  V4 pool %s has zero liquidity — skip', pool_id_hex[:16])
            return None

        # Query Initialize event to get exact currency0, currency1
        global V4_INIT_TOPIC
        if V4_INIT_TOPIC is None:
            V4_INIT_TOPIC = '0x' + Web3.keccak(
                text='Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)'
            ).hex()

        pm = Web3.to_checksum_address(V4_POOL_MANAGER)
        current = w3.eth.block_number
        logs = w3.eth.get_logs({
            'fromBlock': max(0, current - 300000),
            'toBlock': 'latest',
            'address': pm,
            'topics': [V4_INIT_TOPIC, '0x' + pool_id_bytes.hex()],
        })

        if not logs:
            logger.warning('  V4 pool %s: no Initialize event found', pool_id_hex[:16])
            return None

        log = logs[0]
        topics = [t.hex() if isinstance(t, bytes) else t for t in log.get('topics', [])]
        if len(topics) < 4:
            return None

        c0 = Web3.to_checksum_address('0x' + topics[2][-40:])
        c1 = Web3.to_checksum_address('0x' + topics[3][-40:])

        # Data: fee(uint24), tickSpacing(int24), hooks(address), sqrtPriceX96, tick
        data = log['data']
        if isinstance(data, str):
            data = bytes.fromhex(data[2:] if data.startswith('0x') else data)
        fee = int.from_bytes(data[0:32], 'big')
        ts_raw = int.from_bytes(data[32:64], 'big')
        if ts_raw >= 2**23:
            ts_raw -= 2**24
        hooks = Web3.to_checksum_address('0x' + data[76:96].hex())

        pool_key = (c0, c1, fee, ts_raw, hooks)

        # Finding 2 (R2): Verify DexScreener token is plausibly one of c0/c1
        # V4 on-chain addresses may differ from DexScreener (e.g. RENT).
        # Match first 10 hex chars after '0x' (5 bytes = 40 bits) — strict enough to reject unrelated ERC20s.
        # NO known-quote bypass: a quote token should never be the "token we buy".
        dex_lower = dex_token_addr.lower()
        dex_prefix = dex_lower[:12]  # '0x' + first 10 hex chars
        c0_lower, c1_lower = c0.lower(), c1.lower()
        c0_prefix, c1_prefix = c0_lower[:12], c1_lower[:12]
        exact_match = (dex_lower == c0_lower or dex_lower == c1_lower)
        prefix_match = (dex_prefix == c0_prefix or dex_prefix == c1_prefix)
        if not exact_match and not prefix_match:
            logger.warning('  V4 pool %s: DexScreener token %s does NOT match c0=%s or c1=%s — SKIP (fail closed)',
                           pool_id_hex[:16], dex_token_addr[:10], c0[:10], c1[:10])
            return None

        # Identify the quote token
        quote_sym = '?'
        quote_dec = 18
        quote_addr_lower = ''
        for qt_a, (qt_s, qt_d) in QUOTE_TOKENS.items():
            if c0.lower() == qt_a or c1.lower() == qt_a:
                quote_sym = qt_s
                quote_dec = qt_d
                quote_addr_lower = qt_a
                break
        # Also check WETH
        if c0.lower() == WETH_ADDR.lower() or c1.lower() == WETH_ADDR.lower():
            quote_sym = 'ETH'
            quote_dec = 18
            quote_addr_lower = WETH_ADDR.lower()

        result = {
            'pool_id': pool_id_bytes,
            'pool_key': pool_key,
            'c0': c0,
            'c1': c1,
            'fee': fee,
            'tick_spacing': ts_raw,
            'hooks': hooks,
            'quote_sym': quote_sym,
            'quote_dec': quote_dec,
            'quote_addr': quote_addr_lower,
            'liq': liq,
        }
        _v4_pool_cache[al] = result
        logger.info('  V4 pool resolved: %s/%s fee=%d ts=%d hooks=%s liq=%d',
                     quote_sym, '?', fee, ts_raw, hooks[:10], liq)
        return result

    except Exception as ex:
        logger.warning('  V4 pool resolve failed: %s', str(ex)[:80])
        return None


def v4_ok(addr):
    """Check if token has a V4 pool by looking up cached DexScreener pool ID.
    On RH Chain, V4 pool IDs appear as 66-char pair addresses in DexScreener data.
    Uses Initialize event to recover exact PoolKey (currency addresses may differ from DexScreener).
    Returns True and sets TOKEN_ROUTE if a working V4 pool is found."""
    al = addr.lower()

    # Skip if already known
    if al in TOKEN_ROUTE:
        return False

    # Skip recently-failed tokens
    if al in _v3_fail_cache and (time.time() - _v3_fail_cache[al]) < V3_FAIL_TTL:
        return False

    # Check if we have a cached V4 pool ID from scan()
    cached = _pair_data_cache.get(al)
    pair_addr = cached.get('pair_addr', '') if cached else ''

    # V4 pool IDs are 66 chars (0x + 64 hex = 32 bytes), not 42-char contract addresses
    if len(pair_addr) != 66:
        return False  # Not a V4 pool

    # Resolve pool via Initialize event
    pool_data = _resolve_v4_pool(addr, pair_addr)
    if not pool_data:
        return False

    quote_sym = pool_data['quote_sym']
    # Check if this quote token is enabled
    if quote_sym == 'ETH':
        pass  # Always enabled
    elif quote_sym != '?':
        qt_lower = pool_data['quote_addr']
        if not QUOTE_ENABLED.get(qt_lower, False):
            logger.info('  %s: V4 pool found but %s quote not enabled yet', addr[:10], quote_sym)
            return False
    else:
        logger.info('  %s: V4 pool found but unknown quote token', addr[:10])
        return False

    # Verify via V4 Quoter that we can actually swap
    pool_key = pool_data['pool_key']
    quote_dec = pool_data['quote_dec']
    c0, c1 = pool_data['c0'], pool_data['c1']

    # Determine direction for a buy (send quote, receive token)
    # quote is one of c0/c1; the other is the token we're buying
    qt_is_c0 = (pool_data['quote_addr'] == c0.lower())
    buy_zero_for_one = qt_is_c0  # If quote is c0, send c0 to receive c1

    test_amt = int(0.0001 * (10 ** quote_dec))
    try:
        result = v4_quoter.functions.quoteExactInputSingle(
            (pool_key, buy_zero_for_one, test_amt, b'')).call()
        amount_out = result[0]
        if amount_out > 0:
            route_name = f'v4{quote_sym.lower()}_{pool_data["fee"]}_{pool_data["tick_spacing"]}'
            TOKEN_ROUTE[al] = route_name
            logger.info('  %s: V4 %s VERIFIED (output=%d for 0.0001 %s) route=%s',
                        addr[:10], quote_sym, amount_out, quote_sym, route_name)
            _save_v4_cache()  # Finding 6: persist immediately
            return True
    except Exception as ex:
        logger.info('  %s: V4 Quoter failed: %s', addr[:10], str(ex)[:60])

    return False


def _ensure_permit2_approval(token_addr, spender, amount):
    """Ensure Permit2 has approval for token → spender. Two-step:
    1. ERC20 approve token → Permit2 (max)
    2. Permit2 approve token → spender (amount, 30-day expiry)"""
    ca = Web3.to_checksum_address(token_addr)
    spender_ca = Web3.to_checksum_address(spender)
    permit2_ca = Web3.to_checksum_address(PERMIT2)

    # Step 1: ERC20 token → Permit2 (only if needed)
    tok_c = w3.eth.contract(address=ca, abi=E20)
    erc20_allowance = tok_c.functions.allowance(W, permit2_ca).call()
    if erc20_allowance < amount:
        # Zero-reset: if nonzero but insufficient, approve(0) first
        if erc20_allowance > 0:
            logger.info('  Permit2: zero-reset ERC20 %s → Permit2 (current=%d)', ca[:10], erc20_allowance)
            nonce0, op0, _gen = _acquire_nonce('approve', token_addr=token_addr)
            try:
                tx0 = tok_c.functions.approve(permit2_ca, 0).build_transaction(
                    {'from': W, 'nonce': nonce0,
                     'gas': 100000, 'gasPrice': int(w3.eth.gas_price * 3), 'chainId': CID})
            except Exception as build_ex:
                _tx_coord.fail_op(op0.op_id, f'permit2 zero-reset build failed: {build_ex}')
                _tx_coord.resync_nonce()
                raise RuntimeError(f'Permit2 zero-reset build_tx failed: {build_ex}')
            rc0 = _sign_and_send(tx0, op0, timeout=60, generation=_gen)
            if rc0 is None:
                raise RuntimeError(f'ERC20 zero-reset {ca[:10]}→Permit2 TIMEOUT')
            if rc0['status'] != 1:
                raise RuntimeError(f'ERC20 zero-reset {ca[:10]}→Permit2 REVERTED')
        logger.info('  Permit2: ERC20 approve %s → Permit2', ca[:10])
        nonce1, op1, _gen = _acquire_nonce('approve', token_addr=token_addr)
        try:
            tx = tok_c.functions.approve(permit2_ca, 2**256-1).build_transaction(
                {'from': W, 'nonce': nonce1,
                 'gas': 100000, 'gasPrice': int(w3.eth.gas_price * 3), 'chainId': CID})
        except Exception as build_ex:
            _tx_coord.fail_op(op1.op_id, f'permit2 erc20 approve build failed: {build_ex}')
            _tx_coord.resync_nonce()
            raise RuntimeError(f'Permit2 ERC20 approve build_tx failed: {build_ex}')
        rc = _sign_and_send(tx, op1, timeout=60, generation=_gen)
        if rc is None:
            raise RuntimeError(f'ERC20 approve {ca[:10]}→Permit2 TIMEOUT')
        # Finding 7 (R2): Check receipt status — mined revert must not be silently ignored
        if rc['status'] != 1:
            raise RuntimeError(f'ERC20 approve {ca[:10]}→Permit2 REVERTED')

    # Step 2: Permit2 approve token → spender
    p2_allowance = permit2_c.functions.allowance(W, ca, spender_ca).call()
    p2_amount = p2_allowance[0]
    p2_expiry = p2_allowance[1]
    if p2_amount < amount or p2_expiry < int(time.time()) + 3600:
        logger.info('  Permit2: approve %s → %s', ca[:10], spender_ca[:10])
        expiry = int(time.time()) + 30 * 86400  # 30 days
        nonce2, op2, _gen = _acquire_nonce('permit2', token_addr=token_addr)
        try:
            tx = permit2_c.functions.approve(ca, spender_ca, 2**160 - 1, expiry).build_transaction(
                {'from': W, 'nonce': nonce2,
                 'gas': 100000, 'gasPrice': int(w3.eth.gas_price * 3), 'chainId': CID})
        except Exception as build_ex:
            _tx_coord.fail_op(op2.op_id, f'permit2 approve build failed: {build_ex}')
            _tx_coord.resync_nonce()
            raise RuntimeError(f'Permit2 approve build_tx failed: {build_ex}')
        rc = _sign_and_send(tx, op2, timeout=60, generation=_gen)
        if rc is None:
            raise RuntimeError(f'Permit2 approve {ca[:10]}→{spender_ca[:10]} TIMEOUT')
        # Finding 7 (R2): Check receipt status
        if rc['status'] != 1:
            raise RuntimeError(f'Permit2 approve {ca[:10]}→{spender_ca[:10]} REVERTED')


def _encode_v4_swap(pool_key, zero_for_one, amount_in, min_out):
    """Encode a V4 swap via Universal Router v2.1.1.
    Command 0x10 = V4_SWAP. Actions: SWAP_EXACT_IN_SINGLE (0x06), SETTLE_ALL (0x0c), TAKE_ALL (0x0f).

    CRITICAL (Codex R2 on-chain verified): RH Chain Universal Router v2.1.1 ExactInputSingleParams
    includes minHopPriceX36 (uint256). Without it, the router bare-reverts on ABI decode.
    Struct: (PoolKey, bool zeroForOne, uint128 amountIn, uint128 amountOutMin, uint256 minHopPriceX36, bytes hookData)
    Returns (commands_bytes, inputs_list) for Universal Router execute()."""
    from eth_abi import encode

    # V4_SWAP command byte
    commands = bytes([0x10])

    # Action 0x06 = SWAP_EXACT_IN_SINGLE
    # ExactInputSingleParams = ((PoolKey), bool, uint128, uint128, uint256 minHopPriceX36, bytes hookData)
    # minHopPriceX36 = 0 means no price limit (accept any price)
    swap_action = encode(
        ['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'],
        [(pool_key, zero_for_one, amount_in, min_out, 0, b'')]
    )

    # Action 0x0c = SETTLE_ALL (settle the input currency)
    # Params: (address currency, uint256 maxAmount)
    input_currency = pool_key[0] if zero_for_one else pool_key[1]
    settle_action = encode(['address', 'uint256'], [input_currency, amount_in])

    # Action 0x0f = TAKE_ALL (take the output currency)
    # Params: (address currency, uint256 minAmount) — Codex Ultra: NOT (address, address)!
    output_currency = pool_key[1] if zero_for_one else pool_key[0]
    take_action = encode(['address', 'uint256'], [output_currency, min_out])

    # Combine actions: [SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL]
    actions = bytes([0x06, 0x0c, 0x0f])
    params_list = [swap_action, settle_action, take_action]

    # Encode the full V4_SWAP input: (bytes actions, bytes[] params)
    v4_input = encode(['bytes', 'bytes[]'], [actions, params_list])

    return commands, [v4_input]


def _parse_v4_route(route_str):
    """Parse V4 route string like 'v4eth_500_10' or 'v4spy_3000_60'.
    Returns (quote_addr, quote_sym, quote_dec, fee, tick_spacing)."""
    parts = route_str.split('_')
    quote_key = parts[0][2:]  # Remove 'v4' prefix → 'eth', 'spy', 'usdg', etc.
    fee = int(parts[1])
    tick_spacing = int(parts[2])

    if quote_key == 'eth':
        return WETH_ADDR, 'ETH', 18, fee, tick_spacing

    # Look up in QUOTE_TOKENS
    for qt_addr, (qt_sym, qt_dec) in QUOTE_TOKENS.items():
        if qt_sym.lower() == quote_key:
            return Web3.to_checksum_address(qt_addr), qt_sym, qt_dec, fee, tick_spacing

    # Fallback
    return WETH_ADDR, 'ETH', 18, fee, tick_spacing


def v4_buy(addr, amount, sym, market_price=0):
    """Buy token via V4 Universal Router + Permit2.
    Uses cached V4 pool data (exact PoolKey from Initialize event).
    amount = ETH amount (for ETH-quoted) or quote token amount (for SPY/USDG-quoted).
    Returns (tokens_received, entry_price_usd)."""
    al = addr.lower()
    route = TOKEN_ROUTE.get(al, '')
    if not route.startswith('v4'):
        logger.error('v4_buy called but route is %s', route)
        return 0, 0

    # Get resolved pool data (has exact PoolKey with correct on-chain addresses)
    pool_data = _v4_pool_cache.get(al)
    if not pool_data:
        logger.error('v4_buy: no cached pool data for %s', addr[:10])
        return 0, 0

    pool_key = pool_data['pool_key']
    quote_sym = pool_data['quote_sym']
    quote_dec = pool_data['quote_dec']
    quote_addr = pool_data['quote_addr']
    c0, c1 = pool_data['c0'], pool_data['c1']
    is_eth = (quote_sym == 'ETH')

    if is_eth:
        wei = Web3.to_wei(amount, 'ether')
        deploy_usd = amount * ETH_USD
    else:
        wei = int(amount * (10 ** quote_dec))
        if quote_sym == 'SPY':
            sb = spy_bal()
            sb_usd = spy_bal_usd()
            deploy_usd = amount / sb * sb_usd if sb > 0 else amount * 750
        elif quote_sym == 'USDG':
            deploy_usd = amount
        else:
            deploy_usd = amount * market_price if market_price > 0 else 0

    logger.info('V4 BUY %s with %.6f %s (~$%.2f) fee=%d tick=%d',
                sym, amount, quote_sym, deploy_usd, pool_data['fee'], pool_data['tick_spacing'])

    # Determine buy direction and token address
    qt_is_c0 = (quote_addr == c0.lower())
    buy_zero_for_one = qt_is_c0
    v4_token_addr = c1 if qt_is_c0 else c0
    try:
        d = w3.eth.contract(
            address=Web3.to_checksum_address(v4_token_addr), abi=E20
        ).functions.decimals().call()
    except Exception:
        d = 18

    try:
        # Snapshot pre-buy token balance for delta calculation (both V4 and DexScreener addresses)
        pre_r_v4, pre_d_v4 = bal(v4_token_addr)
        pre_r_dex, pre_d_dex = bal(addr) if addr.lower() != v4_token_addr.lower() else (pre_r_v4, pre_d_v4)
        quote_ca = Web3.to_checksum_address(quote_addr) if quote_addr else (c0 if qt_is_c0 else c1)

        if is_eth:
            # Wrap ETH → WETH
            nonce_w, op_w, _gen = _acquire_nonce('wrap', token_addr=addr, symbol=sym)
            try:
                wrap_tx = weth_c.functions.deposit().build_transaction(
                    {'from': W, 'value': wei, 'nonce': nonce_w,
                     'gas': 60000, 'gasPrice': int(w3.eth.gas_price * 3), 'chainId': CID})
            except Exception as build_ex:
                logger.error('V4 BUY %s: wrap build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_w.op_id, f'v4 wrap build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return 0, 0
            rc_w = _sign_and_send(wrap_tx, op_w, timeout=60, generation=_gen)
            if rc_w is None:
                logger.warning('V4 BUY %s: WETH wrap TIMEOUT — aborting', sym)
                return 0, 0
            _ensure_permit2_approval(WETH_ADDR, V4_UNIVERSAL_ROUTER, wei)
        else:
            _ensure_permit2_approval(quote_ca, V4_UNIVERSAL_ROUTER, wei)

        # Get real quote from V4 Quoter (includes pool fee + price impact)
        # This replaces the old DexScreener-based min_out which ignored pool fees
        # and caused reverts on high-fee pools (e.g. fee=99000 = 9.9%)
        try:
            quoted_out = int(v4_quoter.functions.quoteExactInputSingle(
                (pool_key, buy_zero_for_one, wei, b'')).call()[0])
        except Exception as qex:
            logger.error('V4 BUY %s: quoter failed (%s) — REJECTING (no unprotected buys)', sym, str(qex)[:60])
            return 0, 0
        if quoted_out <= 0:
            logger.error('V4 BUY %s: quote returned 0 — REJECTING', sym)
            return 0, 0
        slippage_bps = int(round(MAX_SLIPPAGE * 100))
        min_out = quoted_out * (10_000 - slippage_bps) // 10_000
        logger.info('  V4 quote: %.0f tokens; amountOutMin: %.0f (%.1f%% below quote)',
                    quoted_out / (10 ** d), min_out / (10 ** d), MAX_SLIPPAGE)

        commands, inputs = _encode_v4_swap(pool_key, buy_zero_for_one, wei, min_out)
        deadline = int(time.time()) + 300

        nonce_s, op_s, _gen = _acquire_nonce('buy', token_addr=addr, symbol=sym)
        try:
            tx = v4_router.functions.execute(commands, inputs, deadline).build_transaction(
                {'from': W, 'value': 0, 'nonce': nonce_s,
                 'gas': 500000, 'gasPrice': int(w3.eth.gas_price * 3), 'chainId': CID})
        except Exception as build_ex:
            logger.error('V4 BUY %s: swap build_tx failed: %s — releasing nonce', sym, build_ex)
            _tx_coord.fail_op(op_s.op_id, f'v4 buy swap build failed: {build_ex}')
            _tx_coord.resync_nonce()
            if is_eth:
                _unwrap_all_weth(max_amount=wei)
            return 0, 0
        rc = _sign_and_send(tx, op_s, timeout=120, generation=_gen)
        if rc is None:
            logger.warning('V4 BUY %s: swap TIMEOUT — will reconcile', sym)
            if is_eth:
                _unwrap_all_weth(max_amount=wei)
            return 0, 0

        if rc['status'] == 1:
            # Check token balance delta using the V4 token address
            post_r_v4, post_d_v4 = bal(v4_token_addr)
            tb = (post_r_v4 - pre_r_v4) / (10 ** post_d_v4)
            if tb <= 0:
                # Try DexScreener address as fallback
                post_r_dex, post_d_dex = bal(addr)
                tb = (post_r_dex - pre_r_dex) / (10 ** post_d_dex)
                r = post_r_dex  # for Permit2 approval below
            else:
                r = post_r_v4  # for Permit2 approval below
            if tb <= 0:
                logger.error('V4 BUY %s: swap ok but 0 tokens — BLACKLIST', sym)
                if is_eth:
                    _unwrap_all_weth(max_amount=wei)
                PONS_BLACKLIST.add(al)
                _save_blacklist()
                return 0, 0
            logger.info('V4 BOUGHT %s: %s tokens', sym, f'{tb:,.0f}')
            # Finding 7: Pre-approve token for sell — wrap in try/except so failure
            # doesn't return (0,0) when we already own tokens. Sell will retry approval.
            try:
                _ensure_permit2_approval(v4_token_addr, V4_UNIVERSAL_ROUTER, r)
            except Exception as pex:
                logger.warning('V4 BUY %s: post-buy Permit2 approval failed: %s — sell will retry', sym, str(pex)[:60])
            entry_usd = deploy_usd / tb if tb > 0 else 0
            # Finding 6: Persist V4 cache after successful buy
            _save_v4_cache()
            return tb, entry_usd

        logger.error('V4 BUY %s REVERTED', sym)
        if is_eth:
            _unwrap_all_weth(max_amount=wei)
        return 0, 0

    except Exception as ex:
        logger.error('V4 BUY %s ERR: %s', sym, ex)
        if is_eth:
            _unwrap_all_weth(max_amount=wei)
        return 0, 0


def v4_sell(addr, sym, market_price=0, managed_raw=0, sell_quote=None):
    """Sell token via V4 Universal Router + Permit2.
    Uses cached V4 pool data with exact on-chain addresses."""
    al = addr.lower()
    route = TOKEN_ROUTE.get(al, '')
    if not route.startswith('v4'):
        logger.error('v4_sell called but route is %s', route)
        return 0.0

    pool_data = _v4_pool_cache.get(al)
    if not pool_data:
        logger.error('v4_sell: no cached pool data for %s', addr[:10])
        return 0.0

    pool_key = pool_data['pool_key']
    quote_sym = pool_data['quote_sym']
    quote_dec = pool_data['quote_dec']
    quote_addr = pool_data['quote_addr']
    c0, c1 = pool_data['c0'], pool_data['c1']
    is_eth = (quote_sym == 'ETH')

    # Token address in V4 pool (may differ from DexScreener)
    v4_token_addr = c1 if c0.lower() == quote_addr else c0

    r, d = bal(v4_token_addr)
    if r == 0:
        # Fallback: try DexScreener address
        r, d = bal(addr)
    if r == 0:
        return 0.0
    # Sell exact managed quantity if provided, otherwise full balance
    if managed_raw > 0 and managed_raw <= r:
        r = managed_raw
    tokens_human = r / (10 ** d)
    logger.info('V4 SELL %s (%s tokens) fee=%d tick=%d → %s',
                sym, f'{tokens_human:,.0f}', pool_data['fee'], pool_data['tick_spacing'], quote_sym)

    try:
        _ensure_permit2_approval(v4_token_addr, V4_UNIVERSAL_ROUTER, r)

        # Sell direction: send token, receive quote
        qt_is_c0 = (quote_addr == c0.lower())
        sell_zero_for_one = not qt_is_c0  # If quote is c1, sell c0 to get c1

        # Snapshot pre-sell quote balances for delta calculation
        # Call RPC directly (not through spy_bal() which swallows errors) to detect failures.
        pre_eth = 0
        pre_spy = 0
        pre_qt_bal = 0
        try:
            if is_eth:
                pre_eth = eth_bal()
            elif quote_sym == 'SPY':
                # Direct RPC call — spy_bal() has bare except returning 0.0 which masks failures
                spy_c = w3.eth.contract(address=Web3.to_checksum_address(SPY_ADDR), abi=E20)
                pre_spy = spy_c.functions.balanceOf(W).call() / 1e18
            else:
                qt_ca = Web3.to_checksum_address(quote_addr) if quote_addr else c0
                qt_c_pre = w3.eth.contract(address=qt_ca, abi=E20)
                pre_qt_bal = qt_c_pre.functions.balanceOf(W).call() / (10 ** quote_dec)
        except Exception as snap_ex:
            logger.error('V4 SELL %s: pre-sell snapshot failed: %s — aborting to prevent fabricated proceeds', sym, snap_ex)
            return 0.0

        # ═══ Executable quote → min_out (integer-safe, 1s freshness AT SIGNING) ═══
        # V4 route: quote-only, no price-based fallback — must have executable quote or refuse.
        min_out, used_quote = _quote_min_out(sell_quote, r)
        if used_quote:
            logger.info('  V4 SELL minOut: %d (exec quote, integer-safe)', min_out)
        else:
            if sell_quote and sell_quote.ok:
                logger.info('  V4 SELL: original quote stale (age=%.1fs), re-fetching', time.time() - sell_quote.timestamp)
            try:
                fresh_sq = get_sell_quote(addr, r, sym, route=route)
                min_out, used_quote = _quote_min_out(fresh_sq, r)
                if used_quote:
                    sell_quote = fresh_sq  # Track active quote for signing boundary check
                    logger.info('  V4 SELL minOut: %d (re-fetched exec quote, integer-safe)', min_out)
            except Exception:
                pass

        if min_out > 0:
            attempts = [(min_out, f'quote-{EXEC_SLIPPAGE_ALLOW}%')]
        else:
            logger.warning('V4 SELL %s: no valid quote — quote unavailable', sym)
            # No valid quote — do NOT sign or broadcast without execution protection.
            # Return 0 to retain exit intent; caller will retry when quote available.
            logger.error('SELL %s: no executable quote — refusing to sign. Exit intent retained.', sym)
            return 0.0
        for attempt_min, attempt_label in attempts:
            # Re-check balance before each attempt (tokens may be gone)
            r_now, d_now = bal(v4_token_addr)
            if r_now == 0:
                r_now, d_now = bal(addr)
            if r_now == 0:
                logger.warning('V4 SELL %s: no tokens left in wallet — already sold?', sym)
                update_bal()
                return 0.0  # _do_sell will detect tokens gone via balance check

            # CRITICAL: use managed quantity, not full wallet balance.
            # Without this, managed_raw=100 with wallet=1000 would swap 1000
            # but set minOut for only 100 tokens worth — selling unrelated inventory.
            sell_amount = r  # Already capped to managed_raw above
            if sell_amount > r_now:
                sell_amount = r_now  # Can't sell more than wallet holds
            # If sell_amount was reduced, the original min_out (based on full amount) is
            # too high. Re-quote for the actual sell_amount.
            if sell_amount != r and sell_amount > 0:
                logger.info('V4 SELL %s: quantity reduced %d→%d, re-quoting for actual amount', sym, r, sell_amount)
                try:
                    adj_sq = get_sell_quote(addr, sell_amount, sym, route=route)
                    adj_min, adj_ok = _quote_min_out(adj_sq, sell_amount)
                    if adj_ok and adj_min > 0:
                        attempt_min = adj_min
                        logger.info('V4 SELL %s: adjusted minOut=%d for reduced quantity', sym, attempt_min)
                    else:
                        logger.error('V4 SELL %s: re-quote for reduced qty failed — refusing', sym)
                        return 0.0
                except Exception as adj_ex:
                    logger.error('V4 SELL %s: re-quote exception for reduced qty: %s', sym, adj_ex)
                    return 0.0
            commands, inputs = _encode_v4_swap(pool_key, sell_zero_for_one, sell_amount, attempt_min)
            deadline = int(time.time()) + 300

            nonce_s, op_s, _gen = _acquire_nonce('sell', token_addr=addr, symbol=sym)
            try:
                tx = v4_router.functions.execute(commands, inputs, deadline).build_transaction(
                    {'from': W, 'value': 0, 'nonce': nonce_s,
                     'gas': 500000, 'gasPrice': int(w3.eth.gas_price * 5), 'chainId': CID})
            except Exception as build_ex:
                logger.error('V4 SELL %s: swap build_tx failed: %s — releasing nonce', sym, build_ex)
                _tx_coord.fail_op(op_s.op_id, f'v4 sell swap build failed: {build_ex}')
                _tx_coord.resync_nonce()
                return 0.0
            # ═══ SIGNING BOUNDARY FRESHNESS CHECK ═══
            # RPCs above (balance, nonce, gas) take time. Revalidate the ACTIVE quote
            # (sell_quote tracks refetches) before signing.
            if sell_quote and sell_quote.ok:
                signing_age = time.time() - sell_quote.timestamp
                if signing_age > EXEC_QUOTE_MAX_AGE:
                    logger.warning('V4 SELL %s: quote stale at signing (age=%.1fs > %.1fs) — re-quoting',
                                   sym, signing_age, EXEC_QUOTE_MAX_AGE)
                    try:
                        fresh_sq = get_sell_quote(addr, sell_amount, sym, route=route)
                        fresh_min, fresh_ok = _quote_min_out(fresh_sq, sell_amount)
                        if fresh_ok and fresh_min > 0:
                            sell_quote = fresh_sq  # Update active quote
                            attempt_min = fresh_min
                            commands, inputs = _encode_v4_swap(pool_key, sell_zero_for_one, sell_amount, attempt_min)
                            tx = v4_router.functions.execute(commands, inputs, deadline).build_transaction(
                                {'from': W, 'value': 0, 'nonce': nonce_s,
                                 'gas': 500000, 'gasPrice': int(w3.eth.gas_price * 5), 'chainId': CID})
                            logger.info('V4 SELL %s: rebuilt tx with fresh quote minOut=%d', sym, attempt_min)
                        else:
                            logger.error('V4 SELL %s: re-quote failed — refusing to sign stale', sym)
                            _tx_coord.fail_op(op_s.op_id, 'signing boundary re-quote failed')
                            _tx_coord.resync_nonce()  # Reclaim nonce gap
                            return 0.0
                    except Exception as rq_ex:
                        logger.error('V4 SELL %s: re-quote exception: %s — refusing to sign', sym, rq_ex)
                        _tx_coord.fail_op(op_s.op_id, f'signing boundary re-quote error: {rq_ex}')
                        _tx_coord.resync_nonce()  # Reclaim nonce gap
                        return 0.0
            try:
                rc = _sign_and_send(tx, op_s, timeout=120, generation=_gen)
            except Exception as tx_ex:
                _tx_coord.fail_op(op_s.op_id, f'V4 SELL broadcast error: {tx_ex}')
                logger.error('V4 SELL %s tx error (%s): %s', sym, attempt_label, tx_ex)
                continue  # Try next attempt
            if rc is None:
                logger.warning('V4 SELL %s TIMEOUT (%s) — will reconcile', sym, attempt_label)
                return 0.0

            if rc['status'] == 1:
                # Codex Ultra: verify tokens are actually gone (receipt success != full sell)
                # Use delta-based check: compare against pre-sell balance to avoid
                # misclassifying unrelated same-token inventory as residual
                r_pre_swap = sell_amount  # We attempted to sell exactly sell_amount
                r_after, d_after = bal(v4_token_addr)
                if r_after == 0:
                    r_after, d_after = bal(addr)
                # Delta-based: how many tokens actually left the wallet this swap?
                # r_now was snapshotted at the top of this attempt
                tokens_swapped = max(0, r_now - r_after) if r_now > r_after else 0
                managed_remaining = max(0, sell_amount - tokens_swapped)
                if managed_remaining > 0 and tokens_swapped < sell_amount:
                    logger.warning('V4 SELL %s: tx succeeded but only %d of %d managed tokens sold — partial fill, retrying',
                                   sym, tokens_swapped, sell_amount)
                    continue  # Try next attempt with remaining tokens

                if is_eth:
                    _unwrap_all_weth()
                    received = eth_bal() - pre_eth
                    logger.info('V4 SOLD %s → %.6f ETH ($%.2f) [%s]', sym, received, received * ETH_USD, attempt_label)
                    update_bal()
                    return received
                elif quote_sym == 'SPY':
                    # Use spy_bal_strict() — MUST raise on RPC failure to prevent
                    # fabricated proceeds (spy_bal() returns 0.0, giving negative delta)
                    try:
                        post_spy = spy_bal_strict()
                    except Exception as spy_ex:
                        logger.error('V4 SELL %s: post-sell SPY balance RPC failed: %s — cannot compute proceeds', sym, spy_ex)
                        return 0.0  # Caller treats as UNRESOLVED — will verify next tick
                    received_spy = post_spy - pre_spy
                    try:
                        spy_price = spy_bal_usd() / post_spy if post_spy > 0 else 750
                    except Exception:
                        spy_price = 750
                    received_usd = received_spy * spy_price
                    eth_equiv = received_usd / ETH_USD if ETH_USD > 0 else 0
                    logger.info('V4 SOLD %s → %.6f SPY (~$%.2f) [%s]', sym, received_spy, received_usd, attempt_label)
                    update_bal()
                    return eth_equiv
                else:
                    qt_ca = Web3.to_checksum_address(quote_addr) if quote_addr else c0
                    qt_c = w3.eth.contract(address=qt_ca, abi=E20)
                    qt_bal_now = qt_c.functions.balanceOf(W).call() / (10 ** quote_dec)
                    received_qt = qt_bal_now - pre_qt_bal
                    qt_usd = received_qt if quote_sym == 'USDG' else received_qt
                    eth_equiv = qt_usd / ETH_USD if ETH_USD > 0 else 0
                    logger.info('V4 SOLD %s → %.6f %s (~$%.2f) [%s]', sym, received_qt, quote_sym, qt_usd, attempt_label)
                    update_bal()
                    return eth_equiv

            logger.error('V4 SELL %s REVERTED (%s) — caller will retry next tick with fresh quote', sym, attempt_label)

        return 0.0

    except Exception as ex:
        logger.error('V4 SELL %s ERR: %s', sym, ex)
        return 0.0


def scan():
    """Find coin that is ACTIVELY PUMPING right now. Optimized for speed.

    Speed optimizations vs v4.0:
    1. Single DexScreener call for ALL RH chain pairs (not profiles+boosts+batch)
    2. Cache pair quote-token data from scan (v3_ok skips re-fetch)
    3. Cache V3 failures for 5min (skip re-testing known-bad tokens)
    4. Batch size 30 (DexScreener limit) instead of 5
    5. No sleep between batches (DexScreener allows burst)
    6. Already-routed tokens skip pons_ok/v3_ok entirely
    """
    now = time.time()
    addrs = set()

    # Fetch profiles + boosts in parallel-ish (still sequential but no sleep)
    profiles = fetch('https://api.dexscreener.com/token-profiles/latest/v1')
    if profiles:
        for t in profiles:
            if t.get('chainId') == 'robinhood':
                addrs.add(t['tokenAddress'])
    boosted = fetch('https://api.dexscreener.com/token-boosts/latest/v1')
    if boosted:
        for t in boosted:
            if t.get('chainId') == 'robinhood':
                addrs.add(t['tokenAddress'])
    if not addrs: return None
    addrs = list(addrs)

    cands = []
    # Batch 30 at a time (DexScreener max), NO sleep between batches
    for i in range(0, len(addrs), 30):
        batch = addrs[i:i+30]
        d = fetch(f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}")
        if not d: continue
        for p in (d.get('pairs') or []):
            if p.get('chainId') != 'robinhood': continue
            a = p.get('baseToken', {}).get('address', '')
            liq = float(p.get('liquidity', {}).get('usd', 0))
            c5 = p.get('priceChange', {}).get('m5', 0) or 0
            c1h = p.get('priceChange', {}).get('h1', 0) or 0
            b5 = p.get('txns', {}).get('m5', {}).get('buys', 0)
            s5 = p.get('txns', {}).get('m5', {}).get('sells', 0)
            sym = p.get('baseToken', {}).get('symbol', '?')

            # Cache pair quote-token info for v3_ok + v4_ok (avoids re-fetch)
            # Finding 3: Don't overwrite V4 (66-char) pair with non-V4 pair
            qt = p.get('quoteToken', {})
            new_pair = {
                'quote_sym': (qt.get('symbol') or '').upper(),
                'quote_addr': (qt.get('address') or '').lower(),
                'pair_addr': p.get('pairAddress', ''),  # V4 pools: 66-char pool ID
                'ts': now,
            }
            existing = _pair_data_cache.get(a.lower())
            if existing and len(existing.get('pair_addr', '')) == 66 and len(new_pair['pair_addr']) != 66:
                pass  # Keep existing V4 pool entry — don't overwrite with non-V4
            else:
                _pair_data_cache[a.lower()] = new_pair

            # Filter: ONLY mid-pump coins with buyers in control
            if liq < MIN_LIQ: continue
            if c5 < MIN_5M_ENTRY: continue
            if c5 > MAX_5M_ENTRY:
                logger.info('SKIP %s — pump already ripped too hard (5m=%+.1f%% > +%.0f%% cap)', sym, c5, MAX_5M_ENTRY)
                continue
            if c1h < MIN_1H_ENTRY: continue
            ratio = b5 / max(s5, 1)
            if ratio < MIN_BS_ENTRY: continue

            score = c5 * 0.5 + min(b5, 50) * 0.3 + min(liq/10000, 5)
            cands.append({'sym': sym, 'addr': a, 'c5': c5, 'c1h': c1h,
                          'b': b5, 's': s5, 'liq': liq, 'score': score})

    cands.sort(key=lambda x: -x['score'])
    logger.info('Pumping candidates: %d', len(cands))

    # Remove blacklisted tokens
    cands = [c for c in cands if c['addr'].lower() not in PONS_BLACKLIST]

    # FIX: Filter out cooldown, session-blocked, and buy-failed tokens BEFORE ranking
    # This prevents a blocked top candidate from hiding valid lower-ranked candidates
    now_t = time.time()
    eligible = []
    for c in cands:
        al = c['addr'].lower()
        # Skip cooldown (Policy C uses longer cooldown)
        effective_cooldown = EXIT_COOLDOWN_SEC
        if al in EXIT_COOLDOWN and (now_t - EXIT_COOLDOWN[al]) < effective_cooldown:
            continue
        # Skip session-blacklisted (2+ losses)
        if SESSION_LOSSES.get(al, 0) >= MAX_SESSION_LOSSES:
            logger.info('SKIP %s — session-blacklisted (%d losing exits)', c['sym'], SESSION_LOSSES[al])
            continue
        # Skip buy-failed (2+ reverts)
        if BUY_FAIL_COUNT.get(al, 0) >= MAX_BUY_REVERTS:
            continue
        # Skip blacklisted
        if al in PONS_BLACKLIST:
            continue
        eligible.append(c)
    cands = eligible

    # Prioritize: whitelisted > already-routed > unknown
    wl = [c for c in cands if c['addr'].lower() in PONS_WHITELIST]
    routed = [c for c in cands if c['addr'].lower() not in PONS_WHITELIST and c['addr'].lower() in TOKEN_ROUTE]
    rest = [c for c in cands if c['addr'].lower() not in PONS_WHITELIST and c['addr'].lower() not in TOKEN_ROUTE]

    # Whitelisted = instant pick (no validation needed)
    for c in wl:
        logger.info('PICK (WL): %s 5m=%+.1f%% B/S=%d/%d liq=$%s',
                     c['sym'], c['c5'], c['b'], c['s'], f"{c['liq']:,.0f}")
        return c

    # Already-routed V3 tokens = instant pick (validated before)
    for c in routed:
        logger.info('PICK (cached): %s 5m=%+.1f%% route=%s', c['sym'], c['c5'], TOKEN_ROUTE[c['addr'].lower()])
        return c

    # Unknown tokens = need validation (skip recently-failed)
    for c in rest[:5]:
        al = c['addr'].lower()
        # Skip tokens that failed V3 validation recently
        if al in _v3_fail_cache and (now - _v3_fail_cache[al]) < V3_FAIL_TTL:
            continue
        logger.info('Testing %s 5m=%+.1f%%...', c['sym'], c['c5'])
        if pons_ok(c['addr']):
            PONS_WHITELIST.add(al)
            TOKEN_ROUTE[al] = 'pons'
            logger.info('PICK (new/pons): %s', c['sym'])
            return c
        if v3_ok(c['addr']):
            logger.info('PICK (new/v3): %s fee=%s', c['sym'], TOKEN_ROUTE.get(al))
            return c
        if v4_ok(c['addr']):
            logger.info('PICK (new/v4): %s route=%s', c['sym'], TOKEN_ROUTE.get(al))
            return c
        _v3_fail_cache[al] = now  # Cache failure — don't re-test for 5 min
        logger.info('  %s: not compatible (pons, v3, or v4)', c['sym'])
    return None


def verify_momentum(addr, sym, initial_c5):
    """Take 3 momentum readings over ~6s. Only approve if momentum is STABLE or RISING."""
    readings = [initial_c5]
    logger.info('⏱ PRE-ENTRY: Studying %s momentum... (3 readings, 6s)', sym)
    for i in range(2):
        time.sleep(3)
        info = price(addr)
        if not info or info['p'] <= 0:
            logger.warning('  Reading %d: price unavailable — ABORT', i+2)
            return False, readings
        c5 = info['c5']
        b5 = info['b5']
        s5 = info['s5']
        bs = b5 / max(s5, 1)
        readings.append(c5)
        logger.info('  Reading %d: 5m=%+.1f%% B/S=%d/%d (%.2f)', i+2, c5, b5, s5, bs)

        # If momentum already died during observation, abort
        if c5 < MOMENTUM_DEATH:
            logger.warning('  Momentum DIED during observation (%+.1f%%) — ABORT', c5)
            return False, readings

        # If momentum spiked too high during observation, abort (buying the top)
        if c5 > MAX_5M_ENTRY:
            logger.warning('  Momentum SPIKED too high during observation (%+.1f%% > +%.0f%%) — ABORT (buying the top)', c5, MAX_5M_ENTRY)
            return False, readings

        # If B/S flipped to sellers during observation, abort
        if bs < 0.7:
            logger.warning('  Sellers took over during observation (B/S=%.2f) — ABORT', bs)
            return False, readings

    # Check trend: momentum should NOT be declining steeply
    # Compare highest reading to the final reading — catches ANY large drop
    # regardless of whether the decline is strictly monotonic
    # FIX: old code required readings[2] < readings[1] < readings[0] (strict monotonic)
    # which missed [100, 100, 19] (81% drop) because 100 < 100 is False
    if len(readings) == 3:
        highest = max(readings)
        last = readings[-1]
        if highest > 0 and last < highest:
            drop_pct = (highest - last) / highest * 100
            if drop_pct > 30:
                logger.warning('  Momentum DROPPED: %.1f→%.1f→%.1f (highest %.1f → last %.1f = -%.0f%%) — ABORT',
                              readings[0], readings[1], readings[2], highest, last, drop_pct)
                return False, readings
            else:
                logger.info('  Momentum dipping slightly: %.1f→%.1f→%.1f — OK (%.0f%% drop from peak)',
                           readings[0], readings[1], readings[2], drop_pct)

        # If latest reading dropped below entry threshold, don't enter
        if readings[-1] < MIN_5M_ENTRY:
            logger.warning('  Latest reading %+.1f%% below entry threshold %+.1f%% — ABORT',
                          readings[-1], MIN_5M_ENTRY)
            return False, readings

        # Reapply B/S ratio check on the final reading
        final_info = price(addr)
        if final_info:
            final_bs = final_info['b5'] / max(final_info['s5'], 1)
            if final_bs < MIN_BS_ENTRY:
                logger.warning('  Final B/S ratio %.2f below threshold %.2f — ABORT', final_bs, MIN_BS_ENTRY)
                return False, readings

    logger.info('  ✅ Momentum VERIFIED: %.1f→%.1f→%.1f — ENTERING', readings[0], readings[1], readings[2])
    return True, readings


# ── Persistent state — service-owned durable directory (not /tmp) ──
# Deferred until wallet address is known. Initialized by _init_state_dir().
_STATE_DIR = None
_STATE_FILE = None
_state_dir_initialized = False
_shadow_mode = False  # Set by _init_shadow_wallet — controls state isolation

def _init_state_dir():
    """Initialize state directory once wallet address is known.
    Called by _init_wallet() and _init_shadow_wallet().
    Shadow mode uses an isolated subdirectory ('shadow/') to prevent
    accidental overwrite of production state."""
    global _STATE_DIR, _STATE_FILE, _BLACKLIST_FILE, PONS_BLACKLIST, _state_dir_initialized
    if _state_dir_initialized:
        return
    base_dir = os.path.join('/var/lib/rh-engine', f'{CID}-{W.lower()}')
    if _shadow_mode:
        # Shadow uses isolated subdirectory — never writes to production state
        _STATE_DIR = os.path.join(base_dir, 'shadow')
    else:
        _STATE_DIR = base_dir
    # Migrate from old truncated-address directory if it exists
    _OLD_STATE_DIR = os.path.join('/var/lib/rh-engine', f'{CID}-{W[:10].lower()}')
    if os.path.isdir(_OLD_STATE_DIR) and not os.path.isdir(_STATE_DIR):
        import shutil
        try:
            shutil.copytree(_OLD_STATE_DIR, _STATE_DIR)
            logger.info('Migrated state dir from %s → %s', _OLD_STATE_DIR, _STATE_DIR)
        except Exception as _mig_ex:
            logger.warning('State dir migration failed: %s — starting fresh', _mig_ex)
    os.makedirs(_STATE_DIR, mode=0o700, exist_ok=True)
    _STATE_FILE = os.path.join(_STATE_DIR, 'rh_engine_state.json')

    # Migrate blacklist to stable directory (from source-relative to service-owned)
    _BLACKLIST_STABLE = os.path.join(_STATE_DIR, 'rh_blacklist.json')
    if not os.path.exists(_BLACKLIST_STABLE) and os.path.exists(_BLACKLIST_FILE):
        import shutil
        try:
            shutil.copy2(_BLACKLIST_FILE, _BLACKLIST_STABLE)
            logger.info('Migrated blacklist from %s to %s', _BLACKLIST_FILE, _BLACKLIST_STABLE)
        except Exception:
            pass
    _BLACKLIST_FILE = _BLACKLIST_STABLE
    # Reload blacklist from new location
    PONS_BLACKLIST = _load_blacklist()
    _state_dir_initialized = True
    _load_persisted_state()

def _load_state():
    """Load persisted cooldowns, losses, buy fail counts, daily drawdown state,
    tx coordinator state, position ledger, and active position.

    Returns:
        Tuple of (cooldowns, losses, buy_fails, loss_timestamps).
        Also sets module-level _day_start_equity, _entry_paused, _pos_ledger.
        Active position dict stored in _restored_position for main() to consume.
    """
    import json as _json
    global _day_start_equity, _entry_paused, _pos_ledger, _restored_position
    if _STATE_FILE is None:
        return {}, {}, {}, {}
    try:
        with open(_STATE_FILE) as f:
            data = _json.load(f)
        # Restore daily drawdown state across restart
        dse = data.get('day_start_equity')
        if isinstance(dse, dict) and 'date' in dse and 'equity' in dse:
            _day_start_equity = dse
        ep = data.get('entry_paused', False)
        _entry_paused = bool(ep)
        # Restore tx coordinator state (nonce, generation, pending ops)
        tc_data = data.get('tx_coordinator')
        if tc_data and _tx_coord:
            _tx_coord.load_from_dict(tc_data)
            logger.info('STATE: restored TxCoordinator gen=%d nonce=%s',
                        _tx_coord.generation, tc_data.get('next_nonce'))
        # Restore position ledger if position was open at shutdown
        pl_data = data.get('pos_ledger')
        if pl_data:
            _pos_ledger = PositionLedger.from_dict(pl_data)
            logger.info('STATE: restored PositionLedger %s status=%s managed=%s',
                        _pos_ledger.symbol, _pos_ledger.status, _pos_ledger.managed_raw)
        # Restore active position for crash recovery (Section 5)
        # Preserves: peak price, exec_peak_usd, exit latch, cost basis, entry price
        # Does NOT re-anchor cost basis to spot — uses persisted entry price
        ap = data.get('active_position')
        if ap and isinstance(ap, dict) and ap.get('addr'):
            _restored_position = {
                'addr': ap['addr'],
                'sym': ap.get('sym', '???'),
                'entry': ap.get('entry', 0),
                'peak': ap.get('peak', 0),
                'exec_peak_usd': ap.get('exec_peak_usd', 0),
                'managed_raw': ap.get('managed_raw', 0),
                'cost_basis_usd': ap.get('cost_basis_usd', 0),
                'route': ap.get('route'),
                '_exit_latched': ap.get('exit_latched', False),
                '_partial_proceeds': ap.get('partial_proceeds', 0),
                '_sell_fails': ap.get('sell_fails', 0),
                '_total_sell_fails': ap.get('total_sell_fails', 0),
                '_save_pending': ap.get('save_pending', False),
                '_verify_only': ap.get('verify_only', False),
                '_verify_attempts': ap.get('verify_attempts', 0),
                'tick': ap.get('tick', 0),
                'c5_history': ap.get('c5_history', []),
                'bs_history': ap.get('bs_history', []),
                'last_price': ap.get('last_price', 0),
                '_restored': True,  # Flag: this position was restored from disk
            }
            logger.info('STATE: restored active position %s entry=$%.8f peak=$%.8f '
                        'managed=%d cost_basis=$%.2f exec_peak_usd=$%.2f latched=%s',
                        _restored_position['sym'], _restored_position.get('entry', 0),
                        _restored_position.get('peak', 0),
                        _restored_position.get('managed_raw', 0),
                        _restored_position.get('cost_basis_usd', 0),
                        _restored_position.get('exec_peak_usd', 0),
                        _restored_position.get('_exit_latched', False))
        return (
            {k: float(v) for k, v in data.get('cooldowns', {}).items()},
            {k: int(v) for k, v in data.get('losses', {}).items()},
            {k: int(v) for k, v in data.get('buy_fails', {}).items()},
            {k: float(v) for k, v in data.get('loss_timestamps', {}).items()},
        )
    except (FileNotFoundError, ValueError, KeyError):
        return {}, {}, {}, {}

_KEEP_POSITION = object()  # Sentinel: preserve existing active_position on disk


def _save_state(position=_KEEP_POSITION):
    """Persist cooldowns, losses, buy fail counts, position state, and tx coordinator
    to disk. Crash-consistent: write → fsync → atomic rename.

    Args:
        position: Controls active_position field:
            - _KEEP_POSITION (default): preserve whatever is on disk (no change)
            - None: explicitly clear active_position (position released)
            - dict: update active_position with this position's fields
    """
    import json as _json
    import tempfile
    data = {
        'cooldowns': EXIT_COOLDOWN,
        'losses': SESSION_LOSSES,
        'buy_fails': BUY_FAIL_COUNT,
        'loss_timestamps': _LOSS_TIMESTAMPS,
        'day_start_equity': _day_start_equity,
        'entry_paused': _entry_paused,
        'tx_coordinator': _tx_coord.to_dict() if _tx_coord else None,
        'pos_ledger': _pos_ledger.to_dict() if _pos_ledger else None,
    }
    # Persist active position state for restart recovery (Section 5)
    if position is _KEEP_POSITION:
        # Preserve existing active_position from disk — do NOT erase it
        try:
            if _STATE_FILE and os.path.exists(_STATE_FILE):
                with open(_STATE_FILE) as _f:
                    _old = _json.load(_f)
                data['active_position'] = _old.get('active_position')
            else:
                data['active_position'] = None
        except Exception:
            data['active_position'] = None
    elif position is not None:
        data['active_position'] = {
            'addr': position.get('addr'),
            'sym': position.get('sym'),
            'entry': position.get('entry'),
            'peak': position.get('peak'),
            'exec_peak_usd': position.get('exec_peak_usd', 0),
            'managed_raw': position.get('managed_raw', 0),
            'cost_basis_usd': position.get('cost_basis_usd', 0),
            'route': position.get('route'),
            'exit_latched': position.get('_exit_latched', False),
            'partial_proceeds': position.get('_partial_proceeds', 0),
            'sell_fails': position.get('_sell_fails', 0),
            'total_sell_fails': position.get('_total_sell_fails', 0),
            'save_pending': position.get('_save_pending', False),
            'verify_only': position.get('_verify_only', False),
            'verify_attempts': position.get('_verify_attempts', 0),
            'tick': position.get('tick', 0),
            'c5_history': position.get('c5_history', []),
            'bs_history': position.get('bs_history', []),
            'last_price': position.get('last_price', 0),
            'saved_at': time.time(),
        }
    else:
        # Explicitly clear — position has been released
        data['active_position'] = None
    if _STATE_DIR is None or _STATE_FILE is None:
        return True  # State dir not yet initialized — vacuously successful
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=_STATE_DIR, suffix='.tmp')
        with os.fdopen(tmp_fd, 'w') as f:
            _json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _STATE_FILE)  # Atomic on POSIX
        # fsync the directory to ensure the rename is durable
        dir_fd = os.open(_STATE_DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except Exception as e:
        logger.error('Failed to save state: %s', e)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return False

# Deferred state loading — called after wallet init when _STATE_FILE is set
EXIT_COOLDOWN = {}
SESSION_LOSSES = {}
BUY_FAIL_COUNT = {}
_LOSS_TIMESTAMPS = {}

def _load_persisted_state():
    """Load state from disk and clean up expired entries. Called after _init_state_dir()."""
    global EXIT_COOLDOWN, SESSION_LOSSES, BUY_FAIL_COUNT, _LOSS_TIMESTAMPS
    if _STATE_FILE is None:
        return
    EXIT_COOLDOWN, SESSION_LOSSES, BUY_FAIL_COUNT, _LOSS_TIMESTAMPS = _load_state()
    # Clean up expired 24h loss blocks on startup
    _now = time.time()
    for _addr in list(_LOSS_TIMESTAMPS):
        if _now - _LOSS_TIMESTAMPS[_addr] > 86400:
            SESSION_LOSSES.pop(_addr, None)
            _LOSS_TIMESTAMPS.pop(_addr, None)


# ═══════════════════════════════════════════════════════════
# FAST PRICE MONITOR — sub-second exit detection via eth_getLogs
# Runs in a background daemon thread, polls swap events at ~400ms
# Falls back silently to DexScreener-only if pool discovery or RPC fails
# ═══════════════════════════════════════════════════════════

def _discover_pool(token_addr):
    """Find the V3 pool address for a token/WETH pair.
    Strategy 1: V3 factory lookup (works for V3 tokens).
    Strategy 2: Scan recent blocks for Swap events involving this token (works for Pons tokens too).
    Returns (pool_addr, weth_is_token0, token_decimals) or None."""
    ca = Web3.to_checksum_address(token_addr)
    weth = Web3.to_checksum_address(WETH_ADDR)

    # Strategy 1: V3 Factory lookup
    try:
        factory = w3.eth.contract(address=Web3.to_checksum_address(V3_FACTORY_RH), abi=FACTORY_ABI)
        for fee in V3_FEES:
            try:
                pool_addr = factory.functions.getPool(weth, ca, fee).call()
                if pool_addr and pool_addr != Z:
                    pool_c = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_SLOT0_ABI)
                    t0 = pool_c.functions.token0().call().lower()
                    weth_is_t0 = (t0 == weth.lower())
                    tc = w3.eth.contract(address=ca, abi=E20)
                    decimals = tc.functions.decimals().call()
                    logger.info('⚡ POOL FOUND (factory): %s fee=%d weth_t0=%s dec=%d',
                                pool_addr[:10], fee, weth_is_t0, decimals)
                    return (pool_addr, weth_is_t0, decimals)
            except Exception:
                continue
    except Exception:
        pass

    # Strategy 2: Scan recent blocks for Swap events, then check which pools contain our token
    try:
        topic = '0x' + Web3.keccak(text='Swap(address,address,int256,int256,uint160,uint128,int24)').hex()
        current = w3.eth.block_number
        # Scan last 500 blocks (~40 seconds of blocks) for any swap events
        logs = w3.eth.get_logs({
            'fromBlock': max(0, current - 500),
            'toBlock': 'latest',
            'topics': [topic],
        })
        # Check each unique pool to see if it pairs our token with WETH
        # 30-second hard cutoff to prevent engine from hanging during position monitoring
        checked_pools = set()
        scan_start = time.time()
        POOL_SCAN_TIMEOUT = 30
        for log in logs:
            if time.time() - scan_start > POOL_SCAN_TIMEOUT:
                logger.warning('⚡ POOL SCAN TIMEOUT after %ds (checked %d pools) — DexScreener-only',
                               POOL_SCAN_TIMEOUT, len(checked_pools))
                return None
            pool_addr = log['address']
            if pool_addr in checked_pools:
                continue
            checked_pools.add(pool_addr)
            try:
                pool_c = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_SLOT0_ABI)
                t0 = pool_c.functions.token0().call()
                t1 = pool_c.functions.token1().call()
                # Check if this pool pairs our token with WETH
                if t0.lower() == ca.lower() and t1.lower() == weth.lower():
                    tc = w3.eth.contract(address=ca, abi=E20)
                    decimals = tc.functions.decimals().call()
                    logger.info('⚡ POOL FOUND (scan): %s weth_t0=False dec=%d', pool_addr[:10], decimals)
                    return (pool_addr, False, decimals)
                elif t1.lower() == ca.lower() and t0.lower() == weth.lower():
                    tc = w3.eth.contract(address=ca, abi=E20)
                    decimals = tc.functions.decimals().call()
                    logger.info('⚡ POOL FOUND (scan): %s weth_t0=True dec=%d', pool_addr[:10], decimals)
                    return (pool_addr, True, decimals)
            except Exception:
                continue

        logger.info('⚡ POOL NOT FOUND for %s (checked %d pools) — DexScreener-only', token_addr[:10], len(checked_pools))
        return None
    except Exception as ex:
        logger.warning('⚡ Pool discovery failed: %s', ex)
        return None


def _price_from_sqrt(sqrt_price_x96, weth_is_token0, token_decimals):
    """Calculate token USD price from sqrtPriceX96.
    V3 formula: raw = (sqrtPriceX96 / 2^96)^2 = token1_base / token0_base.
    Returns price per token in USD."""
    if sqrt_price_x96 == 0:
        return 0.0
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    if weth_is_token0:
        # token0=WETH(18dec), token1=TOKEN(Xdec)
        # raw = TOKEN_base / WETH_base
        # tokens_per_weth_human = raw * 10^(18 - token_decimals)
        # weth_per_token = 1 / tokens_per_weth_human
        tokens_per_weth = raw * (10 ** (18 - token_decimals))
        if tokens_per_weth == 0:
            return 0.0
        return (1.0 / tokens_per_weth) * ETH_USD
    else:
        # token0=TOKEN(Xdec), token1=WETH(18dec)
        # raw = WETH_base / TOKEN_base
        # weth_per_token_human = raw * 10^(token_decimals - 18)
        weth_per_token = raw * (10 ** (token_decimals - 18))
        return weth_per_token * ETH_USD


class PriceMonitor:
    """Background thread that polls eth_getLogs for swap events at ~400ms intervals.
    Detects price crashes faster than DexScreener polling (2s → 400ms).
    Falls back silently if RPC fails — main loop DexScreener polling continues regardless."""

    def __init__(self):
        self._w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={
            'headers': {'User-Agent': 'fast-monitor', 'Content-Type': 'application/json'},
            'timeout': 5  # Short timeout for fast polling
        }))
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pool_addr = None
        self._weth_is_token0 = True
        self._token_decimals = 18
        self._latest_price = 0.0
        self._entry_price = 0.0
        self._peak_price = 0.0
        self._sell_signal = threading.Event()
        self._sell_reason = ''
        self._selling = threading.Event()   # DEFECT B FIX: suppress new sell signals while sell in progress
        self._last_block = 0
        self._consecutive_errors = 0
        self._active = False

    def start(self, token_addr, entry_price, pool_info):
        """Start monitoring. pool_info = (pool_addr, weth_is_token0, token_decimals)."""
        self.stop()  # Clean up any previous run
        self._pool_addr = pool_info[0]
        self._weth_is_token0 = pool_info[1]
        self._token_decimals = pool_info[2]
        self._entry_price = entry_price
        self._peak_price = entry_price
        self._latest_price = entry_price
        self._sell_signal.clear()
        self._sell_reason = ''
        self._selling.clear()
        self._stop.clear()
        self._consecutive_errors = 0
        self._active = True
        try:
            self._last_block = self._w3.eth.block_number
        except Exception:
            self._last_block = 0
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name='fast-price')
        self._thread.start()
        logger.info('⚡ FAST MONITOR started (pool=%s, entry=$%.8f, poll=%.0fms)',
                     self._pool_addr[:10], entry_price, FAST_POLL_SEC * 1000)

    def stop(self):
        """Stop the monitoring thread."""
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=10)  # DEFECT B FIX: 10s join (was 2s — thread could outlive stop)
            logger.info('⚡ FAST MONITOR stopped')
        self._active = False
        self._thread = None

    def mark_selling(self):
        """DEFECT B FIX: Mark that a sell is in progress — suppresses new sell signals from _poll_loop."""
        self._selling.set()

    def is_active(self):
        """Check if monitor thread is running."""
        return self._active and self._thread is not None and self._thread.is_alive()

    def get_price(self):
        """Thread-safe read of latest price."""
        with self._lock:
            return self._latest_price

    def should_sell(self):
        """Thread-safe check of sell signal. Returns (triggered: bool, reason: str)."""
        if self._sell_signal.is_set():
            return True, self._sell_reason
        return False, ''

    def clear_sell_signal(self):
        """Reset sell signal after a failed sell attempt."""
        self._sell_signal.clear()
        self._sell_reason = ''

    def _poll_loop(self):
        """Background polling loop. Runs every ~400ms checking for swap events."""
        pool_cs = Web3.to_checksum_address(self._pool_addr)
        topic = '0x' + self._w3.keccak(text='Swap(address,address,int256,int256,uint160,uint128,int24)').hex()

        while not self._stop.is_set():
            try:
                current_block = self._w3.eth.block_number
                from_block = max(self._last_block + 1, current_block - MONITOR_BLOCK_RANGE)

                if from_block > current_block:
                    time.sleep(FAST_POLL_SEC)
                    continue

                # Fetch swap events from the pool
                logs = self._w3.eth.get_logs({
                    'fromBlock': from_block,
                    'toBlock': current_block,
                    'address': pool_cs,
                    'topics': [topic],
                })

                self._last_block = current_block
                self._consecutive_errors = 0  # Reset on success

                if not logs:
                    time.sleep(FAST_POLL_SEC)
                    continue

                # Process the LATEST swap event (most recent price)
                latest_log = logs[-1]
                data = latest_log['data']
                if isinstance(data, str):
                    data = bytes.fromhex(data[2:] if data.startswith('0x') else data)

                # Swap event data layout:
                # [0:32]   int256 amount0
                # [32:64]  int256 amount1
                # [64:96]  uint160 sqrtPriceX96
                # [96:128] uint128 liquidity
                # [128:160] int24 tick
                if len(data) >= 96:
                    sqrt_price = int.from_bytes(data[64:96], 'big')
                    new_price = _price_from_sqrt(sqrt_price, self._weth_is_token0, self._token_decimals)

                    if new_price > 0:
                        with self._lock:
                            self._latest_price = new_price
                            if new_price > self._peak_price:
                                self._peak_price = new_price

                        # Check fast exit conditions
                        pnl = (new_price - self._entry_price) / self._entry_price * 100
                        peak_drop = (self._peak_price - new_price) / self._peak_price * 100 if self._peak_price > 0 else 0

                        # DEFECT B FIX: suppress new sell signals while a sell is already in progress
                        if self._selling.is_set():
                            pass  # Sell in progress — do not fire another signal
                        elif pnl <= FAST_SL_THRESHOLD and not self._sell_signal.is_set():
                            self._sell_reason = f'⚡ FAST SL {pnl:+.1f}% (${new_price:.8f})'
                            self._sell_signal.set()
                            logger.warning('⚡ FAST SL TRIGGERED: %s', self._sell_reason)

                        elif peak_drop >= FAST_PEAK_DROP and not self._sell_signal.is_set():
                            self._sell_reason = f'⚡ FAST PEAK DROP -{peak_drop:.1f}% from peak (${new_price:.8f})'
                            self._sell_signal.set()
                            logger.warning('⚡ FAST PEAK DROP TRIGGERED: %s', self._sell_reason)

            except Exception as ex:
                self._consecutive_errors += 1
                if self._consecutive_errors >= 5:
                    logger.warning('⚡ FAST MONITOR: 5 consecutive errors, falling back to DexScreener-only: %s', ex)
                    self._active = False
                    return  # Thread exits, main loop continues with DexScreener
                elif self._consecutive_errors == 1:
                    logger.debug('⚡ FAST MONITOR poll error: %s', ex)

            time.sleep(FAST_POLL_SEC)


# Global monitor instance
price_monitor = PriceMonitor()


def _pons_sim(ca, wei_amount):
    """Simulate a Pons swap.
    The Pons swap contract returns EMPTY bytes from eth_call, so we CANNOT
    extract the actual token output amount.  Always returns 0 so that callers
    (like check_slippage) fall through to the liquidity-based estimation,
    which is the only reliable slippage check for Pons tokens.
    DO NOT return gas estimates here — callers interpret the return value
    as token count, and gas values produce fabricated 97%+ slippage."""
    return 0


def check_slippage(addr, eth_amt, sym, market_price, liquidity=0, quote_deploy=None, quote_dec=None):
    """Check if our position size will cause excessive slippage.
    V3: simulate swap with eth_call for exact fill price.
    Pons: TWO-AMOUNT comparison — tiny vs full swap to detect price impact.
    V4: quote_deploy = actual human-readable quote-token amount to deploy
        (not ETH-equivalent). quote_dec = quote token decimals.
    Returns (ok, slippage_pct). Takes ~600ms for Pons, ~300ms for V3."""
    if market_price <= 0:
        logger.warning('  SLIPPAGE REJECT: market_price=%.8f for %s — invalid/zero price', market_price, sym)
        return False, 100.0  # DEFECT FIX: reject unknown slippage, don't claim PASS
    route = TOKEN_ROUTE.get(addr.lower(), 'pons')
    wei = Web3.to_wei(eth_amt, 'ether')
    ca = Web3.to_checksum_address(addr)
    deploy_usd = eth_amt * ETH_USD

    try:
        if route.startswith('v4'):
            # V4: use V4 Quoter for exact fill price
            pool_data = _v4_pool_cache.get(addr.lower())
            if not pool_data:
                logger.info('  SLIPPAGE CHECK (V4): no pool data — use liq-based fallback')
                if liquidity >= 10000:
                    est_slip = deploy_usd / liquidity * 1000
                    return est_slip <= MAX_SLIPPAGE, est_slip
                return False, 100.0

            pool_key = pool_data['pool_key']
            pd_quote_dec = pool_data['quote_dec']
            c0 = pool_data['c0']
            qt_is_c0 = (pool_data['quote_addr'] == c0.lower())
            buy_zfo = qt_is_c0

            # FIX: Use actual quote-token amount when available (not ETH-equivalent).
            # quote_deploy = human-readable amount of the actual quote token to deploy.
            # For ETH routes: use wei. For SPY/USDG routes: use typed raw amounts.
            if quote_deploy is not None and pool_data['quote_sym'] != 'ETH':
                qd = quote_dec if quote_dec is not None else pd_quote_dec
                test_amt = int(quote_deploy * (10 ** qd))
                deploy_usd = quote_deploy  # USDG ≈ $1 each; SPY needs oracle
                if pool_data['quote_sym'] == 'SPY':
                    try:
                        deploy_usd = quote_deploy * (spy_bal_usd() / spy_bal()) if spy_bal() > 0 else quote_deploy * 750
                    except Exception:
                        deploy_usd = quote_deploy * 750
            elif pool_data['quote_sym'] != 'ETH':
                test_amt = int(eth_amt * (10 ** pd_quote_dec))
            else:
                test_amt = wei
            tokens_out_raw = 0
            try:
                result = v4_quoter.functions.quoteExactInputSingle(
                    (pool_key, buy_zfo, test_amt, b'')).call()
                tokens_out_raw = result[0]
            except Exception as v4_ex:
                # Quoter reverts when swap exhausts pool liquidity in active tick
                # range (error 0x6190b2b0).  Try progressively smaller amounts
                # to find the max quotable size — but do NOT extrapolate from the
                # reduced quote (AMM slippage is nonlinear).  Instead, reject if
                # full-amount quote fails — pool cannot absorb the trade.
                logger.info('  SLIPPAGE CHECK (V4): quoter reverted on full amount (%s) — pool depth insufficient',
                            str(v4_ex)[:60])
                for fraction in [0.5, 0.25, 0.1]:
                    reduced = int(test_amt * fraction)
                    if reduced <= 0:
                        break
                    try:
                        result = v4_quoter.functions.quoteExactInputSingle(
                            (pool_key, buy_zfo, reduced, b'')).call()
                        if result[0] > 0:
                            # Pool can handle reduced size but NOT full amount — reject
                            logger.warning('  SLIPPAGE CHECK (V4): quoter OK at %.0f%% size but REJECTS full — pool too thin for our position',
                                           fraction * 100)
                            return False, 100.0
                    except Exception:
                        continue

            if tokens_out_raw <= 0:
                # All quoter attempts failed — fall back to liquidity-based estimation
                if liquidity >= 10000:
                    est_slip = deploy_usd / liquidity * 1000
                    logger.info('  SLIPPAGE CHECK (V4): quoter failed, liq-based fallback: %.1f%%', est_slip)
                    return est_slip <= MAX_SLIPPAGE, est_slip
                logger.warning('  SLIPPAGE CHECK (V4): quoter returned 0, no liq fallback — REJECT')
                return False, 100.0

            v4_token = pool_data['c1'] if qt_is_c0 else pool_data['c0']
            try:
                tc = w3.eth.contract(address=Web3.to_checksum_address(v4_token), abi=E20)
                d = tc.functions.decimals().call()
            except Exception:
                d = 18
            tokens_out = tokens_out_raw / (10 ** d)
            fill_price = deploy_usd / tokens_out if tokens_out > 0 else 0
            slippage = (fill_price - market_price) / market_price * 100 if market_price > 0 else 0
            logger.info('  SLIPPAGE CHECK (V4): fill=$%.8f vs market=$%.8f = %+.1f%%',
                        fill_price, market_price, slippage)
            if slippage > MAX_SLIPPAGE:
                logger.warning('  SLIPPAGE %.1f%% > %.1f%% MAX — REJECT %s', slippage, MAX_SLIPPAGE, sym)
                return False, slippage
            return True, slippage

        elif route.startswith('v3'):
            # V3: exact simulation — eth_call returns token output amount
            fee = int(route.split('_')[1])
            weth = Web3.to_checksum_address(WETH_ADDR)
            # SwapRouter02: no deadline in params tuple
            params = (weth, ca, fee, W, wei, 0, 0)
            tx = v3r.functions.exactInputSingle(params).build_transaction(
                {'from': W, 'value': wei, 'nonce': w3.eth.get_transaction_count(W),
                 'gas': 500000, 'gasPrice': int(w3.eth.gas_price*2), 'chainId': CID})
            result = w3.eth.call(tx)
            tokens_out_raw = int.from_bytes(result[:32], 'big') if len(result) >= 32 else 0

            if tokens_out_raw <= 0:
                logger.warning('  SLIPPAGE CHECK (V3): simulation returned 0 tokens — REJECT')
                return False, 100.0

            _, d = bal(addr)
            if d == 0:
                d = 18
            tokens_out = tokens_out_raw / (10 ** d)
            fill_price = deploy_usd / tokens_out
            slippage = (fill_price - market_price) / market_price * 100

            logger.info('  SLIPPAGE CHECK (V3): fill=$%.8f vs market=$%.8f = %+.1f%%',
                        fill_price, market_price, slippage)

            if slippage > MAX_SLIPPAGE:
                logger.warning('  SLIPPAGE %.1f%% > %.1f%% MAX — REJECT %s', slippage, MAX_SLIPPAGE, sym)
                return False, slippage
            return True, slippage

        else:
            # Pons: TWO-AMOUNT simulation — compare tiny vs full swap
            # Tiny swap (0.001 ETH) = minimal slippage = "fair price"
            # Full swap (actual amount) = real price we'd pay
            # If full gives us significantly fewer tokens per ETH, that's slippage
            tiny_wei = Web3.to_wei(0.001, 'ether')
            tiny_out = _pons_sim(ca, tiny_wei)
            full_out = _pons_sim(ca, wei)

            if tiny_out <= 0 or full_out <= 0:
                # Pons eth_call returns 0x since contract upgrade — use liquidity-based estimation
                # BELL trade proved DexScreener liq is inflated vs actual pool depth
                # 10× safety multiplier + position cap (0.15% of pool) prevents BELL-type disasters
                # BELL: reported $122K liq, 12.8% round-trip slip on $277 (was 0.23% of pool)
                if liquidity >= 10000:
                    est_slip = deploy_usd / liquidity * 1000  # 10× safety multiplier
                    logger.info('  SLIPPAGE FALLBACK (liq-based, 10x safety): $%.0f into $%.0f liq → est %.1f%%',
                                deploy_usd, liquidity, est_slip)
                    if est_slip > MAX_SLIPPAGE:
                        logger.warning('  SLIPPAGE est %.1f%% > %.1f%% MAX — REJECT %s',
                                       est_slip, MAX_SLIPPAGE, sym)
                        return False, est_slip
                    logger.info('  SLIPPAGE est %.1f%% OK — proceeding (post-buy -12%% safety active)',
                                est_slip)
                    return True, est_slip
                else:
                    logger.warning('  SLIPPAGE CHECK (Pons): sim=0 + low liq ($%.0f) — REJECT %s',
                                   liquidity, sym)
                    return False, 100.0

            # Price per ETH: tokens_out / eth_in
            tiny_rate = tiny_out / 0.001  # tokens per ETH at zero slippage
            full_rate = full_out / eth_amt  # tokens per ETH at our size
            slippage = (1 - full_rate / tiny_rate) * 100

            logger.info('  SLIPPAGE CHECK (Pons): tiny=%.0f/ETH full=%.0f/ETH slip=%+.1f%%',
                        tiny_rate, full_rate, slippage)

            if slippage > MAX_SLIPPAGE:
                logger.warning('  SLIPPAGE %.1f%% > %.1f%% MAX — REJECT %s', slippage, MAX_SLIPPAGE, sym)
                return False, slippage
            return True, slippage

    except Exception as ex:
        logger.warning('  SLIPPAGE CHECK ERR: %s — REJECT (fail-safe)', ex)
        return False, 100.0  # CHANGED: fail-safe = reject on error, not proceed


def _portfolio_equity():
    """Calculate reconciled portfolio equity across ETH, WETH, SPY, USDG and managed
    position value. Avoids double-counting: partial proceeds already in ETH balance
    are not counted again via _pos_ledger.proceeds_usd."""
    # Native ETH + WETH (both in ETH terms)
    native_eth = eth_bal()
    try:
        weth_erc = w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDR), abi=E20)
        weth_raw = weth_erc.functions.balanceOf(W).call()
        weth_eth = float(Web3.from_wei(weth_raw, 'ether'))
    except Exception:
        weth_eth = 0.0
    eth_total_usd = (native_eth + weth_eth) * ETH_USD

    # SPY + USDG
    spy_usd = spy_bal_usd()
    usdg_usd = usdg_bal()

    # Managed position value (if any — valued at last known price)
    managed_usd = 0.0
    if _pos_ledger and _pos_ledger.status in (PositionLedger.ACTIVE, PositionLedger.PARTIAL, PositionLedger.EXITING):
        # Use cost_basis as conservative floor (market value requires fresh quote)
        # Partial proceeds are already in ETH balance — only count remaining basis
        remaining_basis = (_pos_ledger.cost_basis_usd or 0) - (_pos_ledger.proceeds_usd or 0)
        managed_usd = max(0, remaining_basis)

    return eth_total_usd + spy_usd + usdg_usd + managed_usd


def _check_daily_drawdown():
    """Check reconciled equity vs day start — pause entries if drawdown exceeds limit.
    Includes ETH + WETH + SPY + USDG + managed position. Once tripped, stays paused until UTC day rollover."""
    global _entry_paused, _day_start_equity
    import datetime

    try:
        current_equity = _portfolio_equity()
    except Exception:
        return  # Cannot calculate equity — leave current state unchanged

    today = datetime.datetime.utcnow().date().isoformat()

    if _day_start_equity is None or _day_start_equity.get('date') != today:
        # New UTC day or first run — set/reset baseline
        _day_start_equity = {'date': today, 'equity': current_equity}
        _entry_paused = False  # Reset pause on new day
        return

    baseline = _day_start_equity['equity']
    if baseline > 0:
        drawdown_pct = (baseline - current_equity) / baseline * 100
        if drawdown_pct >= DAILY_DRAWDOWN_LIMIT:
            if not _entry_paused:
                logger.warning('🛑 DAILY DRAWDOWN %.1f%% ≥ %.1f%% — pausing new entries (baseline $%.2f, current $%.2f)',
                               drawdown_pct, DAILY_DRAWDOWN_LIMIT, baseline, current_equity)
            _entry_paused = True
            # Do NOT clear pause — it stays until UTC day rollover


def enter():
    """Enter a pumping coin — with pre-entry momentum verification + slippage check."""
    # ═══ DAILY DRAWDOWN CIRCUIT BREAKER ═══
    _check_daily_drawdown()
    if _entry_paused:
        logger.info('SKIP entry — daily drawdown circuit breaker active')
        return None

    c = scan()
    if not c: return None

    # ═══ COOLDOWN CHECK — don't re-enter same token within cooldown period ═══
    al = c['addr'].lower()
    effective_cooldown = EXIT_COOLDOWN_SEC
    if al in EXIT_COOLDOWN:
        elapsed = time.time() - EXIT_COOLDOWN[al]
        if elapsed < effective_cooldown:
            remaining = int(effective_cooldown - elapsed)
            logger.info('SKIP %s — cooldown (%ds remaining)', c['sym'], remaining)
            return None

    # ═══ SESSION LOSS BLACKLIST — 2 losing exits = stop trading this token ═══
    if SESSION_LOSSES.get(al, 0) >= MAX_SESSION_LOSSES:
        logger.info('SKIP %s — session-blacklisted (%d losing exits)', c['sym'], SESSION_LOSSES[al])
        return None

    # ═══ BUY RETRY LIMIT — max 2 reverted buys before giving up ═══
    if BUY_FAIL_COUNT.get(al, 0) >= MAX_BUY_REVERTS:
        logger.info('SKIP %s — buy reverted %d times, session-blocked', c['sym'], BUY_FAIL_COUNT[al])
        return None

    # ═══ PRE-ENTRY MOMENTUM VERIFICATION (3 readings, 6s, no extra delay) ═══
    # Codex Ultra analysis: restore verification but zero additional delay.
    # 3 readings at 0/3/6s confirm trend + B/S. No confirmation sleep after.
    ok, readings = verify_momentum(c['addr'], c['sym'], c['c5'])
    if not ok:
        logger.info('SKIP %s — momentum not verified', c['sym'])
        return None
    logger.info('⚡ VERIFIED ENTRY: %s 5m=%+.1f%% B/S=%.2f — entering now',
                c['sym'], readings[-1], c['b']/max(c['s'],1))

    route = TOKEN_ROUTE.get(c['addr'].lower(), 'pons')
    is_spy_route = route.startswith('v3spy') or route.startswith('v4spy')
    is_v4_other = route.startswith('v4') and not route.startswith('v4eth') and not route.startswith('v4spy')

    # ═══ PRE-BUY POOL DISCOVERY — find monitor pool BEFORE buying ═══
    # Codex Ultra fix: pool discovery was blocking for 30s AFTER buy,
    # causing SL overshoots (-35% on -3% SL). Now we discover first.
    pre_pool_info = _discover_pool(c['addr'])
    if pre_pool_info:
        logger.info('PRE-BUY: Pool found for %s — fast monitor ready', c['sym'])
    else:
        logger.info('PRE-BUY: No pool for %s — DexScreener-only mode', c['sym'])

    # ═══ BALANCE CHECK — use SPY balance for SPY-paired, ETH for everything else ═══
    if is_v4_other:
        # FIX: Check ETH gas reserve even for V4-other (USDG/HIMS/NVDA) routes
        e_gas = eth_bal()
        if e_gas < GAS_RESERVE:
            logger.error('Not enough ETH for gas on V4-%s route: %.6f ETH (need %.4f)', 'other', e_gas, GAS_RESERVE)
            return None
        # V4 with non-ETH/SPY quote (USDG, HIMS, NVDA) — check that quote balance
        v4_qa, v4_qs, v4_qd, _, _ = _parse_v4_route(route)
        qt_ca = Web3.to_checksum_address(v4_qa)
        qt_c = w3.eth.contract(address=qt_ca, abi=E20)
        qt_raw = qt_c.functions.balanceOf(W).call()
        qt_human = qt_raw / (10 ** v4_qd)
        deploy_usd_est = qt_human  # Rough: most quote tokens ≈ $1 (USDG) or need price oracle
        if qt_human < 1:
            logger.error('Not enough %s for V4: %.6f', v4_qs, qt_human)
            return None
        deploy = 0  # Not used for V4 other routes
        deploy_spy = 0
    elif is_spy_route:
        # FIX: Check ETH gas reserve even for SPY-route buys
        e_gas = eth_bal()
        if e_gas < GAS_RESERVE:
            logger.error('Not enough ETH for gas on SPY route: %.6f ETH (need %.4f)', e_gas, GAS_RESERVE)
            return None
        sb = spy_bal()
        sb_usd = spy_bal_usd()
        deploy_usd = sb_usd - 5.0  # Keep $5 SPY reserve
        if deploy_usd < 10:
            logger.error('Not enough SPY: %.6f (~$%.2f)', sb, sb_usd)
            return None
        deploy_spy = sb - (5.0 / (sb_usd / sb) if sb > 0 and sb_usd > 0 else 0)  # SPY amount to deploy
    else:
        e = eth_bal()
        deploy = e - GAS_RESERVE
        if deploy < 0.002:
            logger.error('Not enough ETH: %.6f', e)
            return None

    # ═══ POSITION SIZING — deploy full available balance ═══
    # LIQ_CAP_PCT = 100.0 → no fixed liquidity cap.
    # Slippage pre-check and RT cost check constrain effective size.
    info = price(c['addr'])
    mkt_price = info['p'] if info else 0
    liq = info.get('liq', 0) if info else 0

    # ═══ SLIPPAGE PRE-CHECK (~300ms) ═══
    # FIX: All routes must run slippage pre-check, not just ETH/Pons.
    # For SPY routes, convert SPY deploy to ETH-equivalent for the check.
    # For V4-other, use estimated USD deploy / ETH_USD.
    slip_pct = None
    if is_spy_route:
        # FIX: Pass actual SPY deploy amount (not ETH-equivalent) for V4 quoter accuracy
        spy_deploy_eth_equiv = deploy_usd / ETH_USD if ETH_USD > 0 else 0.01
        slip_ok, slip_pct = check_slippage(c['addr'], spy_deploy_eth_equiv, c['sym'], mkt_price,
                                            liquidity=liq, quote_deploy=deploy_spy, quote_dec=18)
        if not slip_ok:
            logger.info('SKIP %s (SPY route) — slippage too high (%.1f%%)', c['sym'], slip_pct)
            return None
    elif is_v4_other:
        # FIX: Pass actual quote-token deploy amount (not ETH-equivalent) for V4 quoter accuracy
        v4_deploy_eth_equiv = deploy_usd_est / ETH_USD if ETH_USD > 0 else 0.01
        slip_ok, slip_pct = check_slippage(c['addr'], v4_deploy_eth_equiv, c['sym'], mkt_price,
                                            liquidity=liq, quote_deploy=qt_human * 0.95, quote_dec=v4_qd)
        if not slip_ok:
            logger.info('SKIP %s (V4-%s route) — slippage too high (%.1f%%)', c['sym'], v4_qs, slip_pct)
            return None
    elif deploy > 0:
        slip_ok, slip_pct = check_slippage(c['addr'], deploy, c['sym'], mkt_price, liquidity=liq)
        if not slip_ok:
            logger.info('SKIP %s — slippage too high (%.1f%%)', c['sym'], slip_pct)
            return None

    # ═══ ROUND-TRIP COST CHECK ═══
    # Actual modeled buy-and-sell recovery: simulate buy → simulate sell of acquired tokens.
    # RT cost = 1 - (sell_proceeds / buy_cost), including fees, impact, asymmetry.
    # Replaces trivially-equivalent 2×slip check.
    if slip_pct is not None:
        # Determine deploy amount for RT model
        if is_v4_other:
            rt_deploy = qt_human * 0.95
        elif is_spy_route:
            rt_deploy = deploy_spy
        else:
            rt_deploy = deploy if deploy > 0 else 0.001
        rt_ok, est_rt_cost, est_rt_bounded, _, _ = get_rt_cost_model(
            c['addr'], rt_deploy, 0, c['sym'], mkt_price,
            route=route, liquidity=liq)
        if not rt_ok:
            if route == 'pons':
                # Pons has no executable sell quote — reject entry.
                # Retain unresolved Pons exposure but do not open new positions.
                logger.info('SKIP %s — Pons route has no validated sell quote, entry rejected '
                            '(unsupported route — requires V3 or V4)', c['sym'])
                return None
            # RT model unavailable for non-Pons route — fall back to slippage-based estimate
            est_rt_cost = slip_pct * 2
            est_rt_bounded = est_rt_cost + EXEC_SLIPPAGE_ALLOW * 2
        # Check against expected budget (1.5% impact)
        if est_rt_cost > MAX_RT_COST_EXPECTED:
            logger.info('SKIP %s — modeled RT cost %.2f%% exceeds %.1f%% expected budget',
                        c['sym'], est_rt_cost, MAX_RT_COST_EXPECTED)
            return None
        # Check against bounded budget (includes execution slippage tolerance)
        if est_rt_bounded > MAX_RT_COST_BOUNDED:
            logger.info('SKIP %s — bounded RT cost %.2f%% exceeds %.1f%% bounded limit',
                        c['sym'], est_rt_bounded, MAX_RT_COST_BOUNDED)
            return None

    # ═══ HONEYPOT CHECK — simulate buy + sell BEFORE committing real money ═══
    # honeypot_check() handles blacklisting internally for confirmed honeypots.
    # INCONCLUSIVE results return False but do NOT blacklist — token can retry.
    if not honeypot_check(c['addr']):
        logger.warning('🚨 HONEYPOT BLOCKED: %s — cannot sell, skipping', c['sym'])
        return None

    # ═══ FINAL ENTRY PREDICATE REAPPLICATION — re-check conditions right before buy ═══
    # FIX: After momentum verification, several seconds may have passed during pool discovery,
    # balance checks, slippage checks, and honeypot simulation. Momentum could have collapsed.
    # Re-fetch current data and verify entry conditions still hold.
    final_check = price(c['addr'])
    if final_check:
        final_c5 = final_check['c5']
        final_bs = final_check['b5'] / max(final_check['s5'], 1)
        if final_c5 < MIN_5M_ENTRY:
            logger.warning('FINAL CHECK REJECT %s — 5m%% dropped to %+.1f%% (need +%.1f%%)',
                          c['sym'], final_c5, MIN_5M_ENTRY)
            return None
        if final_c5 > MAX_5M_ENTRY:
            logger.warning('FINAL CHECK REJECT %s — 5m%% spiked to %+.1f%% (max +%.1f%%)',
                          c['sym'], final_c5, MAX_5M_ENTRY)
            return None
        if final_bs < MIN_BS_ENTRY:
            logger.warning('FINAL CHECK REJECT %s — B/S dropped to %.2f (need %.2f)',
                          c['sym'], final_bs, MIN_BS_ENTRY)
            return None
        if final_c5 < MOMENTUM_DEATH:
            logger.warning('FINAL CHECK REJECT %s — momentum dead at %+.1f%%', c['sym'], final_c5)
            return None
        logger.info('FINAL CHECK OK %s — 5m=%+.1f%% B/S=%.2f — proceeding to buy', c['sym'], final_c5, final_bs)
    else:
        logger.warning('FINAL CHECK REJECT %s — price data unavailable', c['sym'])
        return None

    if route.startswith('v4'):
        # V4 route: determine quote token and amount
        v4_quote_addr, v4_quote_sym, v4_quote_dec, _, _ = _parse_v4_route(route)
        if v4_quote_sym == 'ETH':
            tokens, entry_price = v4_buy(c['addr'], deploy, c['sym'], market_price=mkt_price)
        elif v4_quote_sym == 'SPY':
            tokens, entry_price = v4_buy(c['addr'], deploy_spy, c['sym'], market_price=mkt_price)
        else:
            # Other quote tokens (USDG/HIMS/NVDA) — use position-capped qt_human
            # Finding 4: qt_human was already capped by position sizing above
            qt_deploy = qt_human * 0.95  # Keep 5% reserve
            if qt_deploy < 1:
                logger.error('Not enough %s for V4: %.6f', v4_quote_sym, qt_human)
                return None
            tokens, entry_price = v4_buy(c['addr'], qt_deploy, c['sym'], market_price=mkt_price)
    elif is_spy_route:
        tokens, entry_price = v3_buy_spy(c['addr'], deploy_spy, c['sym'], market_price=mkt_price)
    elif route.startswith('v3'):
        tokens, entry_price = v3_buy(c['addr'], deploy, c['sym'], market_price=mkt_price)
    else:
        tokens, entry_price = buy(c['addr'], deploy, c['sym'], market_price=mkt_price)
    if tokens <= 0:
        # Track reverted buys per token — stop after MAX_BUY_REVERTS
        BUY_FAIL_COUNT[al] = BUY_FAIL_COUNT.get(al, 0) + 1
        _save_state()
        logger.info('BUY FAILED for %s — attempt %d/%d', c['sym'], BUY_FAIL_COUNT[al], MAX_BUY_REVERTS)
        return None
    # Buy succeeded — reset fail counter
    BUY_FAIL_COUNT.pop(al, None)
    _save_state()

    # Compute managed raw quantity and cost basis IMMEDIATELY after buy confirmation
    # (needed by _do_sell in bad-fill recovery — was UnboundLocalError before this fix)
    try:
        _, tok_dec = bal(c['addr'])
    except Exception:
        tok_dec = 0  # Fallback — balance RPC failure after confirmed buy
    if tok_dec == 0:
        tok_dec = 18
    managed_raw = int(tokens * (10 ** tok_dec))
    cost_basis_usd = tokens * entry_price  # USD cost of the managed position

    # ═══ CREATE POSITION LEDGER IMMEDIATELY — before bad-fill check, reporting, or approvals ═══
    # Protection begins here. If bad-fill sell-back is needed, the ledger already exists.
    global _pos_ledger
    _pos_ledger = PositionLedger(
        token_addr=c['addr'], symbol=c['sym'], route=route,
        generation=_tx_coord.generation,
        managed_raw=managed_raw, cost_basis_usd=cost_basis_usd,
        entry_price=entry_price, exec_peak_usd=cost_basis_usd,
        residual_raw=managed_raw)
    _tx_coord.new_generation()  # New position = new generation
    # Persist EARLY position with ledger — crash between here and full pos_dict build
    # must still recover with exact quantity, basis, peak, and latch.
    _early_pos = {'addr': c['addr'], 'sym': c['sym'], 'entry': entry_price,
                  'peak': entry_price, 'managed_raw': managed_raw,
                  'cost_basis_usd': cost_basis_usd, 'route': route,
                  'exec_peak_usd': cost_basis_usd}
    _save_state(position=_early_pos)  # Persist ledger + position atomically
    logger.info('LEDGER: created for %s — managed=%d, cost=$%.2f, gen=%d',
                c['sym'], managed_raw, cost_basis_usd, _tx_coord.generation)

    # Post-buy sanity — is market price way below our fill?
    # ═══ FIX: Tightened from -5% to -3% (data: fills at -4% to -6% guarantee losses) ═══
    info = price(c['addr'])
    mkt = info['p'] if info else 0
    if mkt > 0 and entry_price > 0:
        diff = (mkt - entry_price) / entry_price * 100
        logger.info('Fill: $%.8f vs market: $%.8f (%+.1f%%)', entry_price, mkt, diff)
        if diff < -3:
            logger.warning('BAD FILL (%.1f%%) — aborting, selling back. BLACKLISTING %s', diff, c['sym'])
            PONS_BLACKLIST.add(c['addr'].lower())
            _save_blacklist()
            # ── Unified exit handler for bad-fill sell-back ──
            bad_fill_pos = {'addr': c['addr'], 'sym': c['sym'], 'entry': entry_price,
                            'peak': entry_price, 'tick': 0, 'c5_history': [], 'bs_history': [],
                            'managed_raw': managed_raw, 'cost_basis_usd': cost_basis_usd,
                            'route': route, 'exec_peak_usd': cost_basis_usd}
            try:
                sell_result = _do_sell(c['addr'], c['sym'], market_price=mkt,
                                       managed_raw=managed_raw, cost_basis_usd=cost_basis_usd)
            except Exception as sell_ex:
                logger.error('BAD FILL sell-back EXCEPTION for %s: %s — latching exit', c['sym'], sell_ex)
                sell_result = TradeOutcome(TradeOutcome.FAILED, error=str(sell_ex))
            exit_result = _apply_exit_outcome(bad_fill_pos, sell_result, '🚫 BAD FILL', 0)
            if exit_result.action in (ExitResult.RELEASED, ExitResult.RELEASED_UNRESOLVED):
                return None
            # RETAINED_PARTIAL or RETAINED_FAILED — return position with latch for main loop retry
            return bad_fill_pos

    value = tokens * mkt if mkt > 0 else cost_basis_usd
    update_bal(c['sym'], c['addr'], tokens, value)
    logger.info('POSITION: %s %s tokens @ $%.8f', c['sym'], f'{tokens:,.0f}', entry_price)

    # ═══ BUILD POSITION DICT IMMEDIATELY — persist before any monitor/report delay ═══
    # Critical: crash between buy and first _save_state(position=pos) loses exact
    # quantity, basis, peak, and latch. Build and persist atomically here.
    pos_dict = {
        'sym': c['sym'], 'addr': c['addr'], 'entry': entry_price,
        'tokens': tokens, 'peak': entry_price,
        'managed_raw': managed_raw, 'cost_basis_usd': cost_basis_usd,
        'route': route, 'exec_peak_usd': cost_basis_usd,
        'sc': 0, 'mc': 0, 'tc': 0, 'tick': 0,
        'c5_history': list(readings),  # Pre-load with verification readings
        'bs_history': [],  # Track B/S ratio trend
        'entry_c5': readings[-1],  # Use latest verified reading
    }
    _save_state(position=pos_dict)  # Persist position IMMEDIATELY after buy — crash-safe

    # ═══ START FAST PRICE MONITOR (using pre-discovered pool) ═══
    if pre_pool_info:
        price_monitor.start(c['addr'], entry_price, pre_pool_info)
        logger.info('FAST MONITOR: Started immediately (pre-discovered pool)')
    else:
        logger.info('FAST MONITOR: No pool found — DexScreener-only mode for %s', c['sym'])

    return pos_dict


# ── Executable quote freshness at signing boundary ──
# (single definition — see line 557 for the canonical EXEC_QUOTE_MAX_AGE = 1.0)


class ExitResult:
    """Result of applying an exit outcome. Returned by _apply_exit_outcome().
    Controls whether the position is released, retained, or kept latched."""
    RELEASED = 'released'       # Position fully closed — enter gate opens
    RETAINED_PARTIAL = 'partial'  # Partial sell — keep retrying residual
    RETAINED_FAILED = 'failed'    # Sell failed — exit stays latched
    RETAINED_VERIFY = 'verify'    # Post-sell balance RPC failed — must verify before release
    RELEASED_UNRESOLVED = 'unresolved'  # Tokens verified gone, proceeds unknown — gate opens

    __slots__ = ('action', 'net_pnl', 'proceeds', 'cost')

    def __init__(self, action, net_pnl=0, proceeds=0, cost=0):
        self.action = action
        self.net_pnl = net_pnl
        self.proceeds = proceeds
        self.cost = cost


def _apply_exit_outcome(pos, outcome, exit_label, rot):
    """Single deterministic reducer for all exit outcomes.
    Owns: ledger updates, partial proceeds, cooldowns, loss counts, state save.
    Returns ExitResult telling the caller whether to release or retain the position.

    This is the ONLY place that transitions ledger state on exit. No other code
    should call _pos_ledger.record_sell(), mark_unresolved(), or modify
    SESSION_LOSSES/EXIT_COOLDOWN for exit-related outcomes.

    Args:
        pos: Active position dict (mutated in-place for partial/failed)
        outcome: TradeOutcome from _do_sell()
        exit_label: Human-readable exit source label (for logging)
        rot: Current rotation counter

    Returns:
        ExitResult with action, net_pnl, proceeds, cost
    """
    global _pos_ledger

    if outcome.is_unresolved:
        # ═══ DELTA-CONFIRMED: _do_sell already proved managed tokens left the wallet ═══
        # Skip the verify loop — wallet may hold unrelated same-token inventory
        # that would cause the absolute balance check to misclassify as "still held".
        if getattr(outcome, 'tokens_confirmed_gone', False):
            logger.info('%s: tokens_confirmed_gone by delta — skipping verify loop for %s',
                        exit_label, pos['sym'])
            pos['_sold'] = True
            pos.pop('_exit_latched', None)
            if _pos_ledger and _pos_ledger.token_addr == pos['addr']:
                _pos_ledger.mark_unresolved()
            logger.warning('%s #%d — %s UNRESOLVED (delta-confirmed): proceeds unknown, cost $%.2f preserved',
                           exit_label, rot + 1, pos['sym'], pos.get('cost_basis_usd', 0))
            EXIT_COOLDOWN[pos['addr'].lower()] = time.time()
            _saved = False
            for _sav_try in range(3):
                if _save_state(position=None):
                    _saved = True
                    break
                if _sav_try < 2:
                    time.sleep(0.5 * (_sav_try + 1))
            if _saved:
                _pos_ledger = None
                price_monitor.stop()
            else:
                logger.error('STATE SAVE FAILED on delta-confirmed UNRESOLVED for %s — RETAINED', pos['sym'])
                pos.pop('_sold', None)
                pos['_exit_latched'] = True
                pos['_save_pending'] = True
                _save_state(position=pos)
                return ExitResult(ExitResult.RETAINED_FAILED)
            return ExitResult(ExitResult.RELEASED_UNRESOLVED)

        # Post-sell balance check failed — verify tokens are actually gone before releasing.
        # If balance check succeeds and tokens are dust/zero, release. Otherwise retain for retry.
        # NOTE: _verify_attempts only counts RPC FAILURES, not "tokens still held" checks.
        # "Tokens still held" is a valid check result, not a failure.
        try:
            r_verify, _ = bal(pos['addr'])
            # Check V4 alias address too
            v4_pd = _v4_pool_cache.get(pos['addr'].lower(), {})
            if v4_pd:
                qa = v4_pd.get('quote_addr', '')
                v4_token = v4_pd.get('c1', '') if qa == v4_pd.get('c0', '').lower() else v4_pd.get('c0', '')
                if v4_token and v4_token.lower() != pos['addr'].lower():
                    r2, _ = bal(v4_token)
                    r_verify = max(r_verify, r2)
        except Exception as verify_ex:
            verify_count = pos.get('_verify_attempts', 0) + 1
            pos['_verify_attempts'] = verify_count
            logger.warning('%s VERIFY FAILED (%d/5) for %s: %s — retaining position',
                           exit_label, verify_count, pos['sym'], verify_ex)
            if verify_count >= 5:
                # After 5 failed verification attempts, release anyway (RPC is down)
                pos['_sold'] = True
                pos.pop('_exit_latched', None)
                if _pos_ledger and _pos_ledger.token_addr == pos['addr']:
                    _pos_ledger.mark_unresolved()
                logger.error('%s UNRESOLVED after %d verify failures: releasing %s (RPC unreachable)',
                             exit_label, verify_count, pos['sym'])
                EXIT_COOLDOWN[pos['addr'].lower()] = time.time()
                _saved = False
                for _sav_try in range(3):
                    if _save_state(position=None):
                        _saved = True
                        break
                    if _sav_try < 2:
                        time.sleep(0.5 * (_sav_try + 1))
                if _saved:
                    _pos_ledger = None
                    price_monitor.stop()
                else:
                    logger.error('STATE SAVE FAILED after 3 attempts on RELEASED_UNRESOLVED (RPC-down path) — RETAINED')
                    pos.pop('_sold', None)  # Clear _sold — prevents fast-exit bypass
                    pos['_exit_latched'] = True
                    pos['_save_pending'] = True
                    _save_state(position=pos)
                    return ExitResult(ExitResult.RETAINED_FAILED)
                return ExitResult(ExitResult.RELEASED_UNRESOLVED)
            # Retain for next tick to re-verify — do NOT set _exit_latched
            # because that would trigger _do_sell() again. The sell already happened;
            # we only need to verify the balance.
            pos['_verify_only'] = True
            _vo_saved = False
            for _vo_try in range(3):
                if _save_state(position=pos):
                    _vo_saved = True
                    break
                if _vo_try < 2:
                    time.sleep(0.5 * (_vo_try + 1))
            if not _vo_saved:
                logger.error('%s VERIFY-ONLY save failed for %s — setting save_pending',
                             exit_label, pos['sym'])
                pos['_save_pending'] = True
            return ExitResult(ExitResult.RETAINED_VERIFY)

        # Verification RPC succeeded — check if tokens are gone
        _dust_threshold = pos.get('managed_raw', 0) // 100  # 1% = dust
        if _dust_threshold < 1000:
            _dust_threshold = 1000  # At least 1000 wei
        if r_verify <= _dust_threshold:
            # Tokens confirmed gone — safe to release
            pos['_sold'] = True
            pos.pop('_exit_latched', None)
            if _pos_ledger and _pos_ledger.token_addr == pos['addr']:
                _pos_ledger.mark_unresolved()
            logger.warning('%s #%d — %s UNRESOLVED (verified gone): proceeds unknown, cost $%.2f preserved',
                           exit_label, rot + 1, pos['sym'], pos.get('cost_basis_usd', 0))
            EXIT_COOLDOWN[pos['addr'].lower()] = time.time()
            _saved = False
            for _sav_try in range(3):
                if _save_state(position=None):
                    _saved = True
                    break
                if _sav_try < 2:
                    time.sleep(0.5 * (_sav_try + 1))
            if _saved:
                _pos_ledger = None
                price_monitor.stop()
            else:
                logger.error('STATE SAVE FAILED after 3 attempts on RELEASED_UNRESOLVED (verified-gone) — RETAINED')
                pos.pop('_sold', None)  # Clear _sold — prevents fast-exit bypass
                pos['_exit_latched'] = True
                pos['_save_pending'] = True
                _save_state(position=pos)
                return ExitResult(ExitResult.RETAINED_FAILED)
            return ExitResult(ExitResult.RELEASED_UNRESOLVED)
        else:
            # Tokens still present — NOT actually unresolved, treat as failed sell
            logger.warning('%s VERIFY: %s still has %d tokens (managed=%d) — NOT released, retrying',
                           exit_label, pos['sym'], r_verify, pos.get('managed_raw', 0))
            pos['_exit_latched'] = True
            pos['_sell_fails'] = pos.get('_sell_fails', 0) + 1
            _save_state(position=pos)
            return ExitResult(ExitResult.RETAINED_FAILED)

    elif outcome.is_partial:
        # Partial sell — record proceeds, update managed_raw to residual, keep exit latch
        partial_proceeds = outcome.proceeds_usd or 0
        pos['_partial_proceeds'] = pos.get('_partial_proceeds', 0) + partial_proceeds
        pos['managed_raw'] = outcome.residual_raw or 0
        pos['_exit_latched'] = True
        pos['_sell_fails'] = 0  # Partial is progress, not failure
        if _pos_ledger and _pos_ledger.token_addr == pos['addr']:
            _pos_ledger.record_sell(
                outcome.sold_raw or 0, partial_proceeds,
                tx_hash=outcome.tx_hash)
        logger.warning('%s PARTIAL for %s: $%.2f proceeds, %d raw tokens remain — retrying residual',
                       exit_label, pos['sym'], partial_proceeds, outcome.residual_raw or 0)
        _save_state(position=pos)
        return ExitResult(ExitResult.RETAINED_PARTIAL)

    elif outcome.is_resolved:
        # Full resolution — calculate PnL, update ledger, count losses
        # NOTE: Do NOT set _sold=True here — only set it AFTER durable save succeeds.
        # If save fails, _sold=True would let the main loop bypass into enter().
        pos.pop('_exit_latched', None)
        proceeds = (outcome.proceeds_usd or 0) + pos.get('_partial_proceeds', 0)
        cost = pos.get('cost_basis_usd', 0)
        net_pnl = proceeds - cost
        if _pos_ledger and _pos_ledger.token_addr == pos['addr']:
            _pos_ledger.record_sell(
                outcome.sold_raw or outcome.managed_raw or 0,
                outcome.proceeds_usd or 0,
                tx_hash=outcome.tx_hash)
            logger.info('LEDGER RESOLVED (%s): %s net_pnl=$%.2f',
                        exit_label, pos['sym'], _pos_ledger.net_pnl_usd or 0)
        logger.info('%s #%d — $%.2f (cost $%.2f, net $%+.2f)', exit_label, rot + 1, proceeds, cost, net_pnl)
        EXIT_COOLDOWN[pos['addr'].lower()] = time.time()
        if net_pnl < 0:
            SESSION_LOSSES[pos['addr'].lower()] = SESSION_LOSSES.get(pos['addr'].lower(), 0) + 1
            _LOSS_TIMESTAMPS[pos['addr'].lower()] = time.time()
            loss_count = SESSION_LOSSES[pos['addr'].lower()]
            if loss_count >= MAX_SESSION_LOSSES:
                logger.warning('SESSION BLACKLIST: %s lost %d times — no more entries this session',
                               pos['sym'], loss_count)
            else:
                logger.info('Cooldown ON for %s (losing exit — %d/%d session losses)',
                            pos['sym'], loss_count, MAX_SESSION_LOSSES)
        else:
            logger.info('Cooldown ON for %s (winning exit — 60s re-entry delay)', pos['sym'])
        # Durable release with retry — MUST persist before clearing in-memory state.
        # If save fails after retries, return RETAINED to block new entry until next tick.
        _saved = False
        for _save_attempt in range(3):
            if _save_state(position=None):
                _saved = True
                break
            if _save_attempt < 2:
                logger.warning('STATE SAVE retry %d/3 for RELEASED %s', _save_attempt + 2, pos['sym'])
                time.sleep(0.5 * (_save_attempt + 1))
        if _saved:
            pos['_sold'] = True  # Mark sold ONLY after durable save confirms release
            _pos_ledger = None  # Clear in-memory ledger ONLY after confirmed durable save
            price_monitor.stop()  # Stop stale monitor thread — position resolved
        else:
            logger.error('STATE SAVE FAILED after 3 attempts on RELEASED %s — blocking new entry (RETAINED)',
                         pos['sym'])
            pos.pop('_sold', None)  # Ensure _sold is NOT set — prevents main loop bypass
            pos['_exit_latched'] = True
            pos['_save_pending'] = True
            _save_state(position=pos)  # Best-effort: persist exit intent
            return ExitResult(ExitResult.RETAINED_FAILED)
        return ExitResult(ExitResult.RELEASED, net_pnl=net_pnl, proceeds=proceeds, cost=cost)

    else:
        # Sell failed — latch exit intent, track consecutive failures
        sell_fail_count = pos.get('_sell_fails', 0) + 1
        pos['_sell_fails'] = sell_fail_count
        pos['_exit_latched'] = True
        logger.error('%s SELL FAILED (%d times) for %s — exit latched, will retry',
                     exit_label, sell_fail_count, pos['sym'])
        _save_state(position=pos)
        return ExitResult(ExitResult.RETAINED_FAILED)


def _do_sell(addr, sym, market_price=0, managed_raw=0, cost_basis_usd=0):
    """Route sell to correct DEX (Pons, V3-WETH, V3-SPY, or V4).
    Returns TradeOutcome (structured result replacing numeric -1 sentinel).
    Post-sell: verifies tokens are actually gone — catches already-sold, partial fills, etc.

    Executable quote flow: calls get_sell_quote() for a fresh on-chain quote,
    passes it to the seller for min_out computation. For Pons (no quoter),
    the quote returns ok=False and seller falls back to market_price.

    For backward compatibility: callers can check outcome.is_resolved, outcome.is_unresolved,
    outcome.is_failed, or outcome.proceeds_usd.
    The old `e == -1` check becomes `outcome.is_unresolved`.
    The old `e > 0` check becomes `outcome.is_resolved and outcome.proceeds_usd > 0`."""
    route = TOKEN_ROUTE.get(addr.lower(), 'pons')
    # ═══ EXECUTABLE QUOTE: fresh on-chain quote for min_out computation ═══
    sell_quote = None
    if managed_raw <= 0:
        logger.warning('_do_sell %s: managed_raw=%d — resolving from wallet balance', sym, managed_raw)
    if managed_raw > 0:
        try:
            sell_quote = get_sell_quote(addr, managed_raw, sym, route=route)
            if sell_quote.ok:
                logger.info('EXEC QUOTE %s: $%.2f via %s (age=%.2fs)',
                            sym, sell_quote.proceeds_usd or 0, sell_quote.route,
                            time.time() - sell_quote.timestamp)
            else:
                fallback = 'Pons market_price fallback' if route == 'pons' else 'will re-fetch at signing'
                logger.info('EXEC QUOTE %s: unavailable (%s) — %s',
                            sym, sell_quote.error or 'unknown', fallback)
        except Exception as q_ex:
            logger.warning('EXEC QUOTE %s failed: %s — Pons fallback or re-fetch at signing', sym, q_ex)
    # ═══ PRE-SELL BALANCE SNAPSHOT — for delta-based residual calculation ═══
    # Use pre/post delta to determine how many tokens were actually sold.
    # This prevents unrelated same-token inventory from being misclassified as residual.
    pre_sell_raw = 0
    try:
        r_pre, d_pre = bal(addr)
        pre_sell_raw = r_pre
        # Also check V4 alias
        if route.startswith('v4'):
            v4_pd = _v4_pool_cache.get(addr.lower(), {})
            if v4_pd:
                v4_token = v4_pd.get('c1', '') if v4_pd.get('quote_addr', '') == v4_pd.get('c0', '').lower() else v4_pd.get('c0', '')
                if v4_token and v4_token.lower() != addr.lower():
                    r2_pre, d2_pre = bal(v4_token)
                    pre_sell_raw = max(pre_sell_raw, r2_pre)
    except Exception:
        pass  # If pre-snapshot fails, fall back to absolute balance check

    if route.startswith('v4'):
        result = v4_sell(addr, sym, market_price=market_price, managed_raw=managed_raw, sell_quote=sell_quote)
    elif route.startswith('v3spy'):
        result = v3_sell_spy(addr, sym, market_price=market_price, managed_raw=managed_raw, sell_quote=sell_quote)
    elif route.startswith('v3'):
        result = v3_sell(addr, sym, market_price=market_price, managed_raw=managed_raw, sell_quote=sell_quote)
    else:
        result = sell(addr, sym, market_price=market_price, managed_raw=managed_raw, sell_quote=sell_quote)

    # Positive proceeds — verify tokens are actually gone before marking RESOLVED
    if result > 0:
        proceeds_usd = result * ETH_USD
        # ═══ DELTA-BASED RESIDUAL: use pre/post balance change, not absolute balance ═══
        # This prevents unrelated same-token inventory from being misclassified.
        # residual_managed = managed_raw - tokens_actually_sold
        post_sell_raw = 0
        try:
            r_remaining, d_remaining = bal(addr)
            post_sell_raw = r_remaining
            # Also check V4 alias
            if route.startswith('v4'):
                v4_pd = _v4_pool_cache.get(addr.lower(), {})
                if v4_pd:
                    v4_token = v4_pd.get('c1', '') if v4_pd.get('quote_addr', '') == v4_pd.get('c0', '').lower() else v4_pd.get('c0', '')
                    if v4_token and v4_token.lower() != addr.lower():
                        r2, d2 = bal(v4_token)
                        post_sell_raw = max(post_sell_raw, r2)
        except Exception as bal_ex:
            # Balance check FAILED — fail closed: cannot certify sale without verification
            logger.warning('_do_sell: residual balance check FAILED for %s: %s — UNRESOLVED (fail closed)',
                           sym, str(bal_ex)[:60])
            return TradeOutcome(TradeOutcome.UNRESOLVED,
                                proceeds_usd=result * ETH_USD, proceeds_raw=result,
                                token_addr=addr, managed_raw=managed_raw,
                                cost_basis_usd=cost_basis_usd, route=route,
                                error=f'residual balance check failed: {str(bal_ex)[:60]}')
        # Delta-based: how many tokens actually left the wallet
        if pre_sell_raw > 0:
            tokens_sold = max(0, pre_sell_raw - post_sell_raw)
            residual_raw = max(0, managed_raw - tokens_sold) if managed_raw > 0 else 0
            if tokens_sold > managed_raw:
                # Sold more than managed — shouldn't happen, but cap to 0 residual
                logger.warning('_do_sell: sold %d > managed %d for %s — possible over-sell',
                               tokens_sold, managed_raw, sym)
                residual_raw = 0
        else:
            # Pre-snapshot unavailable — fall back to absolute but cap to managed
            residual_raw = min(post_sell_raw, managed_raw) if managed_raw > 0 else post_sell_raw
            logger.info('_do_sell: no pre-sell snapshot — using capped absolute balance for residual')
        sold_raw = max(0, managed_raw - residual_raw) if managed_raw > 0 else 0
        if residual_raw > 0:
            # Partial sell: got proceeds but tokens remain — use explicit PARTIAL status
            logger.warning('_do_sell: got $%.2f but %d raw tokens REMAIN for %s — PARTIAL (not resolved)',
                          proceeds_usd, residual_raw, sym)
            return TradeOutcome(TradeOutcome.PARTIAL,
                                proceeds_usd=proceeds_usd, proceeds_raw=result,
                                token_addr=addr, managed_raw=managed_raw,
                                sold_raw=sold_raw, residual_raw=residual_raw,
                                cost_basis_usd=cost_basis_usd, route=route,
                                quote_sym='ETH')
        return TradeOutcome(TradeOutcome.RESOLVED,
                            proceeds_usd=proceeds_usd, proceeds_raw=result,
                            token_addr=addr, managed_raw=managed_raw,
                            sold_raw=managed_raw, residual_raw=0,
                            cost_basis_usd=cost_basis_usd, route=route, quote_sym='ETH')

    # Sell returned 0 — verify tokens are actually still held
    # Catches: already-sold tokens (v4 addr mismatch), tx that succeeded but returned 0
    # Dust threshold: 1% of managed_raw or 1 raw unit, whichever is larger
    dust_threshold_raw = max(1, int(managed_raw * 0.01)) if managed_raw > 0 else 1
    if result <= 0:
        try:
            r_check, d_check = bal(addr)
            # Also check V4 pool token address (may differ from DexScreener addr)
            if route.startswith('v4'):
                v4_pd = _v4_pool_cache.get(addr.lower(), {})
                if v4_pd:
                    qa = v4_pd.get('quote_addr', '')
                    v4_token = v4_pd.get('c1', '') if qa == v4_pd.get('c0', '').lower() else v4_pd.get('c0', '')
                    if v4_token and v4_token.lower() != addr.lower():
                        r2, d2 = bal(v4_token)
                        r_check = max(r_check, r2)
                elif not v4_pd:
                    # V4 route but no cache data — fail closed, don't assume tokens are gone
                    logger.warning('_do_sell: V4 route for %s but no cache data — assuming tokens still held', sym)
                    r_check = managed_raw  # Assume full position still held
            # ═══ DELTA-BASED: did enough tokens leave the wallet? ═══
            # Prevents double-sell when wallet holds unrelated same-token inventory.
            tokens_gone = False
            if pre_sell_raw > 0 and managed_raw > 0:
                tokens_sold = max(0, pre_sell_raw - r_check)
                if tokens_sold >= managed_raw * 99 // 100:
                    tokens_gone = True
                    logger.info('_do_sell: delta check: %d tokens left wallet (managed=%d) — sale confirmed by delta',
                                tokens_sold, managed_raw)
            # Also check absolute: tokens below dust threshold (backward compat / no pre-snapshot)
            if r_check < dust_threshold_raw:
                tokens_gone = True
            if tokens_gone:
                tokens_left = r_check / (10 ** d_check) if d_check > 0 else r_check
                logger.warning('_do_sell: sell returned 0 but tokens confirmed gone for %s (r_check=%d, pre=%d, managed=%d) — '
                               'position UNRESOLVED: proceeds unknown, cost basis preserved',
                               sym, r_check, pre_sell_raw, managed_raw)
                return TradeOutcome(TradeOutcome.UNRESOLVED,
                                    token_addr=addr, managed_raw=managed_raw,
                                    cost_basis_usd=cost_basis_usd, route=route,
                                    tokens_confirmed_gone=True,
                                    error='tokens gone but proceeds unattributed')
        except Exception:
            # Fail closed — assume tokens still held if we can't check
            logger.error('_do_sell: balance check failed for %s — assuming tokens still held', sym)

    return TradeOutcome(TradeOutcome.FAILED,
                        token_addr=addr, managed_raw=managed_raw,
                        cost_basis_usd=cost_basis_usd, route=route,
                        error='sell returned 0, tokens still held')


def main():
    global _pos_ledger
    _enforce_single_instance()
    logger.info('=' * 50)
    logger.info('ROTATION ENGINE v6.2 — CODEX ULTRA OPTIMIZED (verified entry + SL fix + single instance)')
    logger.info('Enter: 5m +%.0f%%–+%.0f%% B/S>%.2f | Exit: TP(+%.0f%%)/momentum/SL(-%.0f%%)',
                MIN_5M_ENTRY, MAX_5M_ENTRY, MIN_BS_ENTRY, HARD_TP, HARD_SL)
    e_usd = eth_bal() * ETH_USD
    s_usd = spy_bal_usd()
    logger.info('Wallet: %s | $%.2f ETH + ~$%.0f SPY | Target: $%.0f', W, e_usd, s_usd, COST_BASIS)
    logger.info('=' * 50)

    rot = 0

    pos = None

    # ═══ RESTART RECOVERY — restore position from persisted state (Section 5) ═══
    # Uses exact persisted values: cost basis, peak, latch, entry price.
    # Does NOT re-anchor cost basis to spot price. Does NOT reset peak.
    if _restored_position is not None:
        rp = _restored_position
        # Verify tokens are actually still in wallet before trusting the restored position
        try:
            r_check, d_check = bal(rp['addr'])
            if r_check > 0 and rp.get('managed_raw', 0) > 0:
                pos = dict(rp)  # Copy to avoid mutating the global
                # Unconditionally restore route — persisted route takes priority over
                # stale cache data that may have populated TOKEN_ROUTE
                if rp.get('route'):
                    TOKEN_ROUTE[rp['addr'].lower()] = rp['route']
                # Reconcile managed_raw against actual balance — use the smaller
                if r_check < rp['managed_raw']:
                    logger.warning('RESTART RECOVERY: balance %d < managed %d for %s — using actual balance',
                                   r_check, rp['managed_raw'], rp['sym'])
                    pos['managed_raw'] = r_check
                logger.info('RESTART RECOVERY: restored position %s — entry=$%.8f peak=$%.8f '
                            'cost=$%.2f managed=%d exec_peak=$%.2f latched=%s',
                            rp['sym'], rp.get('entry', 0), rp.get('peak', 0),
                            rp.get('cost_basis_usd', 0), pos['managed_raw'],
                            rp.get('exec_peak_usd', 0), rp.get('_exit_latched', False))
                # Start fast monitor for restored position — use persisted entry price,
                # NOT current spot. start() sets peak = entry_price, so pass entry.
                # Then override peak with the persisted peak to preserve protection.
                pool_info = _discover_pool(rp['addr'])
                if pool_info:
                    info = price(rp['addr'])
                    if info and info['p'] > 0:
                        restored_entry = rp.get('entry', info['p'])
                        price_monitor.start(rp['addr'], restored_entry, pool_info)
                        # Override peak with persisted peak — do NOT reset to spot
                        persisted_peak = rp.get('peak', restored_entry)
                        if persisted_peak > restored_entry:
                            price_monitor._peak_price = persisted_peak
                            logger.info('RESTART RECOVERY: monitor peak restored to $%.8f (not spot $%.8f)',
                                        persisted_peak, info['p'])
            else:
                logger.info('RESTART RECOVERY: %s has no tokens (balance=%d) — position resolved while stopped',
                            rp['sym'], r_check)
        except Exception as re_ex:
            # Fail closed: cannot verify token ownership without balance RPC.
            # Do NOT fall through to heuristic scan — that could adopt unrelated tokens.
            # Keep restored position with latch so main loop retries sell after RPC recovers.
            logger.error('RESTART RECOVERY: balance check failed for %s: %s — FAIL CLOSED (keeping position latched)',
                         rp.get('sym', '?'), str(re_ex)[:60])
            pos = dict(rp)
            pos['_exit_latched'] = True  # Force sell attempt when RPC recovers
            # Unconditionally restore TOKEN_ROUTE — persisted route must override any stale cache
            if rp.get('route'):
                TOKEN_ROUTE[rp['addr'].lower()] = rp['route']

    # Detect existing positions on startup — scan Pons whitelist + V4 cached tokens
    # This is the legacy heuristic scan. Only runs if restart recovery didn't find a position.
    if pos is None:
        # Finding 6 (R2): Check Pons whitelist + V4 cached tokens + TOKEN_ROUTE entries
        # Build (dex_addr, v4_on_chain_addr) pairs so we can check both for balance
        startup_checks = []  # [(check_addr, dex_addr_for_route)]
        for addr in PONS_WHITELIST:
            startup_checks.append((addr, addr))
        for v4_al, v4_pd in _v4_pool_cache.items():
            # Only recover if this quote token is currently enabled
            qa = v4_pd.get('quote_addr', '')
            qs = v4_pd.get('quote_sym', '?')
            if qs not in ('ETH', '?') and not QUOTE_ENABLED.get(qa, False):
                continue
            v4_token = v4_pd['c1'] if v4_pd['quote_addr'] == v4_pd['c0'].lower() else v4_pd['c0']
            startup_checks.append((v4_token, v4_al))  # Check V4 on-chain addr, route via dex addr
            if v4_al.lower() != v4_token.lower():
                startup_checks.append((v4_al, v4_al))  # Also check DexScreener address
        for check_addr, dex_addr in startup_checks:
            # Skip blacklisted tokens (e.g. IPUNK honeypot — tokens stuck, unsellable)
            if check_addr.lower() in PONS_BLACKLIST or dex_addr.lower() in PONS_BLACKLIST:
                continue
            try:
                r, d = bal(check_addr)
                tamt = r / (10 ** d)
                if tamt > 100:  # Startup heuristic: ignore airdrop dust (no managed_raw available yet)
                    info = price(check_addr) or price(dex_addr)
                    if info and info['p'] > 0:
                        sym = info.get('sym', '?')
                        # Use cached route if available — don't default to Pons for V4 tokens
                        route = TOKEN_ROUTE.get(dex_addr.lower(), TOKEN_ROUTE.get(check_addr.lower(), ''))
                        if route:
                            logger.info('DETECTED existing position: %s (%s tokens @ $%.8f) route=%s',
                                        sym, f'{tamt:,.0f}', info['p'], route)
                        else:
                            # No known route — skip to avoid sending to wrong seller
                            logger.warning('DETECTED tokens: %s (%s tokens @ $%.8f) but NO ROUTE — skipping (manual sell needed)',
                                           sym, f'{tamt:,.0f}', info['p'])
                            continue
                        pos = {'addr': dex_addr, 'sym': sym, 'entry': info['p'], 'peak': info['p'],
                               'tick': 0, 'c5_history': [], 'bs_history': []}
                        # Start fast monitor for existing position
                        pool_info = _discover_pool(dex_addr) or _discover_pool(check_addr)
                        if pool_info:
                            price_monitor.start(dex_addr, info['p'], pool_info)
                        break
            except Exception:
                pass

    if not pos:
        # Wait for a pump to enter
        pos = enter()
        while not pos:
            logger.info('Waiting for a pump... (scanning every %ds)', SCAN_INTERVAL)
            time.sleep(SCAN_INTERVAL)
            pos = enter()

    while True:
        try:
            # ═══ FAST EXIT CHECK — sub-second, from monitor thread ═══
            # DEFECT B FIX: skip if position already sold (but NOT if save still pending)
            if pos.get('_sold') and not pos.get('_save_pending'):
                pos = None
                while not pos:
                    time.sleep(SCAN_INTERVAL)
                    pos = enter()
                continue
            if price_monitor.is_active() and not pos.get('_save_pending') and not pos.get('_verify_only'):
                should, reason = price_monitor.should_sell()
                if should:
                    logger.warning('⚡ FAST EXIT: %s on %s — selling NOW', reason, pos['sym'])
                    # Mark selling to suppress duplicate signals — do NOT stop() here
                    # (stop() joins worker for up to 10s, blocking protective submission)
                    price_monitor.mark_selling()
                    fast_price = price_monitor.get_price() if price_monitor.get_price() > 0 else pos.get('last_price', 0)
                    try:
                        outcome = _do_sell(pos['addr'], pos['sym'], market_price=fast_price,
                                           managed_raw=pos.get('managed_raw', 0),
                                           cost_basis_usd=pos.get('cost_basis_usd', 0))
                    except Exception as sell_ex:
                        logger.error('⚡ FAST EXIT _do_sell EXCEPTION for %s: %s', pos['sym'], sell_ex)
                        outcome = TradeOutcome(TradeOutcome.FAILED, error=str(sell_ex))
                    # ── Unified exit outcome handler ──
                    exit_result = _apply_exit_outcome(pos, outcome, '⚡ FAST EXIT', rot)
                    if exit_result.action in (ExitResult.RELEASED, ExitResult.RELEASED_UNRESOLVED):
                        price_monitor.clear_sell_signal()
                        rot += 1
                        pos = None
                        while not pos:
                            time.sleep(SCAN_INTERVAL)
                            pos = enter()
                        continue
                    # RETAINED_PARTIAL / RETAINED_FAILED — skip rest of this tick to
                    # prevent the main exit path from issuing a second sell immediately
                    time.sleep(POLL_SEC)
                    continue

            # ═══ PRE-PRICE: SOLD / SAVE-PENDING / VERIFY-ONLY gates ═══
            # These fire EVERY tick — even when price data is unavailable.
            # Must come before price() to prevent bypasses via no-price path.

            # If position was already sold on a prior tick, skip straight to enter()
            if pos.get('_sold') and not pos.get('_save_pending'):
                logger.info('Position %s already sold — skipping to next entry', pos['sym'])
                pos = None
                while not pos:
                    time.sleep(SCAN_INTERVAL)
                    pos = enter()
                continue
            # If a prior release-save failed, retry NOW — block new exit/entry until resolved
            if pos.get('_save_pending'):
                _sp_saved = False
                for _sp_try in range(3):
                    if _save_state(position=None):
                        _sp_saved = True
                        break
                    if _sp_try < 2:
                        time.sleep(0.5 * (_sp_try + 1))
                if _sp_saved:
                    logger.info('SAVE-PENDING resolved for %s — releasing', pos['sym'])
                    pos['_sold'] = True
                    pos.pop('_save_pending', None)
                    _pos_ledger = None
                    price_monitor.stop()
                    pos = None
                    while not pos:
                        time.sleep(SCAN_INTERVAL)
                        pos = enter()
                    continue
                else:
                    logger.error('SAVE-PENDING still failing for %s — blocking entry', pos['sym'])
                    time.sleep(POLL_SEC)
                    continue  # Don't enter, don't sell — retry next tick
            # VERIFY-ONLY: post-sell balance check (sell already happened, don't re-sell)
            if pos.get('_verify_only'):
                # Narrow try — only RPC bal() calls, not reducer/persistence
                _vo_bal = None
                try:
                    _vo_bal, _ = bal(pos['addr'])
                    # Check V4 alias address too (mirrors reducer V4 alias logic)
                    _vo_v4pd = _v4_pool_cache.get(pos['addr'].lower(), {})
                    if _vo_v4pd:
                        _vo_qa = _vo_v4pd.get('quote_addr', '')
                        _vo_v4tok = _vo_v4pd.get('c1', '') if _vo_qa == _vo_v4pd.get('c0', '').lower() else _vo_v4pd.get('c0', '')
                        if _vo_v4tok and _vo_v4tok.lower() != pos['addr'].lower():
                            _vo_r2, _ = bal(_vo_v4tok)
                            _vo_bal = max(_vo_bal, _vo_r2)
                except Exception as vo_ex:
                    _vo_count = pos.get('_verify_attempts', 0) + 1
                    pos['_verify_attempts'] = _vo_count
                    logger.warning('VERIFY-ONLY: bal() RPC failed (%d/5) for %s: %s',
                                   _vo_count, pos['sym'], vo_ex)
                    if _vo_count >= 5:
                        logger.error('VERIFY-ONLY: exhausted — releasing %s unverified', pos['sym'])
                        pos.pop('_verify_only', None)
                        outcome = TradeOutcome(TradeOutcome.UNRESOLVED,
                                                managed_raw=pos.get('managed_raw', 0),
                                                error='verify exhausted after 5 RPC failures')
                        exit_result = _apply_exit_outcome(pos, outcome, 'VERIFY-ONLY EXHAUSTED', rot)
                        if exit_result.action in (ExitResult.RELEASED, ExitResult.RELEASED_UNRESOLVED):
                            rot += 1
                            pos = None
                            while not pos:
                                time.sleep(SCAN_INTERVAL)
                                pos = enter()
                            continue
                        # RETAINED from exhausted VERIFY-ONLY — do NOT fall through to _do_sell
                        time.sleep(POLL_SEC)
                        continue
                    _save_state(position=pos)
                    time.sleep(POLL_SEC)
                    continue  # Retry verification next tick

                # RPC succeeded — reset verify attempts counter (only RPC failures count)
                pos.pop('_verify_attempts', None)
                _vo_dust = max(1000, pos.get('managed_raw', 0) // 100)
                if _vo_bal <= _vo_dust:
                    logger.info('VERIFY-ONLY: %s tokens gone (bal=%d) — releasing', pos['sym'], _vo_bal)
                    pos.pop('_verify_only', None)
                    outcome = TradeOutcome(TradeOutcome.UNRESOLVED,
                                            managed_raw=pos.get('managed_raw', 0),
                                            tokens_confirmed_gone=True,
                                            error='verified gone post-sell')
                    exit_result = _apply_exit_outcome(pos, outcome, 'VERIFY-ONLY', rot)
                    if exit_result.action in (ExitResult.RELEASED, ExitResult.RELEASED_UNRESOLVED):
                        rot += 1
                        pos = None
                        while not pos:
                            time.sleep(SCAN_INTERVAL)
                            pos = enter()
                        continue
                    # RETAINED from VERIFY-ONLY (e.g. save failed) — do NOT fall through to _do_sell
                    time.sleep(POLL_SEC)
                    continue
                else:
                    logger.warning('VERIFY-ONLY: %s still has %d tokens — sell failed, latching',
                                   pos['sym'], _vo_bal)
                    pos.pop('_verify_only', None)
                    pos['_exit_latched'] = True
                    pos['_sell_fails'] = pos.get('_sell_fails', 0) + 1
                    _save_state(position=pos)
                    # Fall through to price() → EXIT 0 latched sell

            info = price(pos['addr'])
            if not info or info['p'] <= 0:
                # If exit is latched, attempt sell even without price data
                # But NOT if save is pending — must retry persistence first
                if pos.get('_exit_latched') and not pos.get('_save_pending') and not pos.get('_verify_only'):
                    logger.warning('Price unavailable but exit latched for %s — attempting sell anyway', pos['sym'])
                    try:
                        sr = _do_sell(pos['addr'], pos['sym'],
                                       managed_raw=pos.get('managed_raw', 0),
                                       cost_basis_usd=pos.get('cost_basis_usd', 0))
                    except Exception as sell_ex:
                        logger.error('LATCHED EXIT _do_sell EXCEPTION for %s: %s', pos['sym'], sell_ex)
                        sr = TradeOutcome(TradeOutcome.FAILED, error=str(sell_ex))
                    # ── Unified exit outcome handler ──
                    exit_result = _apply_exit_outcome(pos, sr, '🔁 LATCHED EXIT', rot)
                    if exit_result.action in (ExitResult.RELEASED, ExitResult.RELEASED_UNRESOLVED):
                        rot += 1
                        pos = None
                        while not pos:
                            time.sleep(SCAN_INTERVAL)
                            pos = enter()
                        continue
                    # RETAINED_PARTIAL / RETAINED_FAILED — fall through to sleep+retry
                time.sleep(POLL_SEC)
                continue

            p = info['p']
            c5 = info['c5']
            b5 = info['b5']
            s5 = info['s5']
            bs_ratio = b5 / max(s5, 1)

            # ═══ EXECUTABLE PnL — use actual sell quote, not chart price ═══
            chart_pnl = (p - pos['entry']) / pos['entry'] * 100  # display only — NOT for decisions
            managed_raw = pos.get('managed_raw', 0)
            cost_basis_usd = pos.get('cost_basis_usd', 0)
            sq = None
            quote_fresh = False
            if managed_raw > 0 and cost_basis_usd > 0:
                sq = get_sell_quote(pos['addr'], managed_raw, pos['sym'],
                                    route=pos.get('route'))
            if sq and sq.ok and cost_basis_usd > 0:
                quote_age = time.time() - sq.timestamp
                quote_fresh = quote_age <= QUOTE_MAX_AGE_S
                pnl = (sq.proceeds_usd - cost_basis_usd) / cost_basis_usd * 100
                if quote_fresh:
                    # Fresh executable quote — use for decisions
                    pos['_last_exec_pnl'] = pnl
                    pos['_last_exec_proceeds'] = sq.proceeds_usd
                    pos['_last_exec_ts'] = sq.timestamp
                    pos['_last_sell_quote'] = sq  # Carry for seller integration
                else:
                    # Quote obtained but already past freshness bound — display only
                    logger.debug('STALE QUOTE for %s (age %.1fs > %.1fs) — display only',
                                 pos['sym'], quote_age, QUOTE_MAX_AGE_S)
            # Check if we have a fresh-enough cached quote for decisions
            cached_ts = pos.get('_last_exec_ts', 0)
            cached_age = time.time() - cached_ts if cached_ts > 0 else 9999
            have_decision_pnl = False
            if sq and sq.ok and quote_fresh:
                # Fresh quote just obtained — use it
                have_decision_pnl = True
            elif cached_age <= QUOTE_MAX_AGE_S and pos.get('_last_exec_pnl') is not None:
                # Cached quote still within freshness bound — use it
                pnl = pos['_last_exec_pnl']
                have_decision_pnl = True
            else:
                # No valid executable quote — display chart PnL only,
                # do NOT feed into SL/TP/trail decisions.
                pnl = chart_pnl  # Display value only — exit decisions check have_decision_pnl
                have_decision_pnl = False
            pos['last_price'] = p  # Track for fast monitor exit sell

            # Update executable peak — ONLY from fresh quotes
            if sq and sq.ok and quote_fresh:
                exec_value = sq.proceeds_usd
                if exec_value > pos.get('exec_peak_usd', 0):
                    pos['exec_peak_usd'] = exec_value
            if p > pos['peak']:
                pos['peak'] = p

            # Track momentum + B/S history
            pos['c5_history'].append(c5)
            if len(pos['c5_history']) > MOMENTUM_HISTORY:
                pos['c5_history'] = pos['c5_history'][-MOMENTUM_HISTORY:]
            pos['bs_history'].append(bs_ratio)
            if len(pos['bs_history']) > MOMENTUM_HISTORY:
                pos['bs_history'] = pos['bs_history'][-MOMENTUM_HISTORY:]
            pos['tick'] += 1

            do_exit = False
            exit_reason = ''
            warmed = pos['tick'] >= WARMUP_TICKS  # With WARMUP_TICKS=0, always True

            # ═══ EXIT 0: LATCHED EXIT — retry previous failed sell (highest precedence) ═══
            if pos.get('_exit_latched'):
                exit_reason = '🔁 LATCHED EXIT (retrying previous failed sell)'
                do_exit = True

            # ═══ EXIT 1: HARD STOP LOSS (-3%) — requires executable PnL ═══
            if not do_exit and have_decision_pnl and pnl <= -HARD_SL:
                exit_reason = f'⛔ HARD SL {pnl:+.1f}% (executable)'
                do_exit = True

            # ═══ EXIT 2: HARD TAKE-PROFIT (+25%) — requires executable PnL ═══
            if not do_exit and have_decision_pnl and pnl >= HARD_TP:
                exit_reason = f'💰 TAKE PROFIT {pnl:+.1f}% ≥ {HARD_TP:+.1f}% (executable) — selling 100%'
                do_exit = True
                logger.info('🎯 HARD TP HIT on %s at exec PnL=%+.1f%% — FULL EXIT', pos['sym'], pnl)

            # ═══ EXIT 3: UNIFIED TRAILING STOP (6% from executable-value peak) ═══
            # Only fires with fresh executable quote data
            if not do_exit and have_decision_pnl:
                exec_peak = pos.get('exec_peak_usd', 0)
                current_exec = sq.proceeds_usd if (sq and sq.ok and quote_fresh) else pos.get('_last_exec_proceeds', 0)
                if exec_peak > 0 and current_exec > 0:
                    peak_drop = (exec_peak - current_exec) / exec_peak * 100
                    if peak_drop >= TRAIL_UNIFIED:
                        exit_reason = f'📉 TRAIL STOP {peak_drop:.1f}% drop from exec peak (threshold {TRAIL_UNIFIED}%)'
                        do_exit = True

            # ═══ EXIT 4: SLOWING — 30% momentum drop from previous reading ═══
            hist = pos['c5_history']
            if not do_exit and warmed and len(hist) >= 2:
                prev = hist[-2]
                curr = hist[-1]
                drop_pct = (prev - curr) / prev * 100 if prev > 0 else 0
                if drop_pct >= SLOWING_DROP_PCT:
                    exit_reason = f'🔻 SLOWING {prev:+.1f}%→{curr:+.1f}% (drop {drop_pct:.0f}%)'
                    do_exit = True

            # ═══ EXIT 5: MOMENTUM DEATH (5m below +5%) ═══
            if not do_exit and warmed and c5 < MOMENTUM_DEATH:
                exit_reason = f'💀 MOMENTUM DEAD 5m={c5:+.1f}%'
                do_exit = True

            # ═══ EXIT 6: SELLERS DOMINANT (B/S < 0.7) ═══
            bsh = pos['bs_history']
            if not do_exit and warmed and len(bsh) >= 1:
                if bsh[-1] < SELLER_DOM_EXIT:
                    exit_reason = f'🔴 SELLERS DOMINANT B/S={b5}/{s5} ratio={bsh[-1]:.2f}'
                    do_exit = True

            # ═══ EXECUTE EXIT ═══
            if do_exit:
                logger.warning('%s on %s PnL=%+.1f%%. Selling...', exit_reason, pos['sym'], pnl)
                # Mark selling + clear signal — do NOT stop() before sell submission
                price_monitor.mark_selling()
                price_monitor.clear_sell_signal()
                try:
                    outcome = _do_sell(pos['addr'], pos['sym'], market_price=p,
                                       managed_raw=pos.get('managed_raw', 0),
                                       cost_basis_usd=pos.get('cost_basis_usd', 0))
                except Exception as sell_ex:
                    logger.error('EXIT _do_sell EXCEPTION for %s: %s', pos['sym'], sell_ex)
                    outcome = TradeOutcome(TradeOutcome.FAILED, error=str(sell_ex))
                # ── Unified exit outcome handler ──
                exit_result = _apply_exit_outcome(pos, outcome, exit_reason, rot)
                if exit_result.action in (ExitResult.RELEASED, ExitResult.RELEASED_UNRESOLVED):
                    rot += 1
                    if exit_result.proceeds >= COST_BASIS:
                        logger.info('🏆 PAST $%.0f! $%.2f — KEEP COMPOUNDING', COST_BASIS, exit_result.proceeds)
                    pos = None
                    while not pos:
                        time.sleep(SCAN_INTERVAL)
                        pos = enter()
                    continue
                elif exit_result.action == ExitResult.RETAINED_PARTIAL:
                    # Partial — stay in loop, will retry via latch
                    continue
                else:
                    # RETAINED_FAILED — handle consecutive failure escalation
                    sell_fail_count = pos.get('_sell_fails', 0)
                    if sell_fail_count >= 5:
                        # Verify balance before clearing — don't orphan live tokens
                        _managed_raw_ref = pos.get('managed_raw', 0)
                        _dust_threshold = max(1, int(_managed_raw_ref * 0.01)) if _managed_raw_ref > 0 else 1
                        try:
                            r_check, d_check = bal(pos['addr'])
                            pos_route = TOKEN_ROUTE.get(pos['addr'].lower(), '')
                            if r_check < _dust_threshold and pos_route.startswith('v4'):
                                v4_pd = _v4_pool_cache.get(pos['addr'].lower(), {})
                                if v4_pd:
                                    qa = v4_pd.get('quote_addr', '')
                                    v4_token = v4_pd.get('c1', '') if qa == v4_pd.get('c0', '').lower() else v4_pd.get('c0', '')
                                    if v4_token and v4_token.lower() != pos['addr'].lower():
                                        r2, d2 = bal(v4_token)
                                        r_check = max(r_check, r2)
                        except Exception:
                            r_check = _managed_raw_ref
                        if r_check < _dust_threshold:
                            tokens_left = r_check / (10 ** d_check) if d_check > 0 else r_check
                            logger.error('SELL FAILED %d TIMES for %s — tokens gone/dust (%.0f left), clearing',
                                         sell_fail_count, pos['sym'], tokens_left)
                            EXIT_COOLDOWN[pos['addr'].lower()] = time.time()
                            _dust_saved = False
                            for _ds_try in range(3):
                                if _save_state(position=None):
                                    _dust_saved = True
                                    break
                                if _ds_try < 2:
                                    time.sleep(0.5 * (_ds_try + 1))
                            if _dust_saved:
                                _pos_ledger = None
                                price_monitor.stop()
                                pos = None
                                while not pos:
                                    time.sleep(SCAN_INTERVAL)
                                    pos = enter()
                                continue
                            else:
                                logger.error('STATE SAVE FAILED (dust-clear) — retrying next tick')
                                pos['_exit_latched'] = True
                                pos['_save_pending'] = True
                                _save_state(position=pos)
                                continue
                        else:
                            total_fails = pos.get('_total_sell_fails', 0) + sell_fail_count
                            pos['_total_sell_fails'] = total_fails
                            if total_fails >= 15:
                                tokens_left = r_check / (10 ** d_check) if d_check > 0 else r_check
                                logger.error('HONEYPOT ABANDON: %s — %d total sell failures, %.0f tokens STUCK. BLACKLISTING.',
                                             pos['sym'], total_fails, tokens_left)
                                PONS_BLACKLIST.add(pos['addr'].lower())
                                _save_blacklist()
                                EXIT_COOLDOWN[pos['addr'].lower()] = time.time()
                                _hp_saved = False
                                for _hp_try in range(3):
                                    if _save_state(position=None):
                                        _hp_saved = True
                                        break
                                    if _hp_try < 2:
                                        time.sleep(0.5 * (_hp_try + 1))
                                if _hp_saved:
                                    _pos_ledger = None
                                    price_monitor.stop()
                                    pos = None
                                    while not pos:
                                        time.sleep(SCAN_INTERVAL)
                                        pos = enter()
                                    continue
                                else:
                                    logger.error('STATE SAVE FAILED (honeypot-abandon) — retrying next tick')
                                    pos['_exit_latched'] = True
                                    pos['_save_pending'] = True
                                    _save_state(position=pos)
                                    continue
                            logger.error('SELL FAILED %d TIMES for %s — tokens STILL IN WALLET — retrying in 60s (total=%d)',
                                         sell_fail_count, pos['sym'], total_fails)
                            pos['_sell_fails'] = 0
                            time.sleep(60)
                            continue

            # Status every 15 ticks (~30s) — also persist position for crash recovery
            if pos['tick'] % 15 == 0:
                r, d = bal(pos['addr'])
                tb = r/(10**d)
                tv = tb * p
                e = eth_bal()
                tot = tv + e * ETH_USD
                peak_drop = (pos['peak'] - p) / pos['peak'] * 100 if pos['peak'] > 0 else 0
                update_bal(pos['sym'], pos['addr'], tb, tv)
                _save_state(position=pos)  # Persist position state every ~30s for crash recovery
                logger.info('R%d %s $%.8f PnL=%+.1f%% 5m=%+.1f%% B/S=%d/%d Peak=-%.1f%% Trail=%.0f%% Val=$%.2f Tot=$%.2f',
                            rot, pos['sym'], p, pnl, c5, b5, s5, peak_drop, TRAIL_UNIFIED, tv, tot)

        except Exception as ex:
            logger.error('ERR: %s', ex)

        time.sleep(POLL_SEC)


_lock_fd = None  # Held for process lifetime

def _enforce_single_instance():
    """Prevent multiple engine instances from trading the same wallet.
    Uses an exclusive OS file lock (flock) scoped to wallet+chain.
    The lock is held for the process lifetime — released automatically on exit.
    Concurrent starts produce one owner; newcomer refuses without touching incumbent."""
    import fcntl
    global _lock_fd
    _lock_file = os.path.join(_STATE_DIR, 'rh_engine.lock')
    try:
        _lock_fd = open(_lock_file, 'w')
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock — write our PID for diagnostics
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        # Keep _lock_fd open — lock released when process exits
    except (IOError, OSError):
        # Lock held by another process — refuse to start
        logger.error('REFUSING TO START: another engine instance holds the lock at %s. '
                     'Stop it first before starting a new instance.', _lock_file)
        sys.exit(1)

def shadow(duration_s=600):
    """Read-only shadow mode: collect quotes, measure latency, log decisions.
    Does NOT sign, broadcast, approve, or change wallet state.
    Shares production decision code but without send_raw_transaction.
    Runs for duration_s seconds (default 600 = 10 minutes), then exits."""
    logger.info('=' * 50)
    logger.info('SHADOW MODE — read-only quote collection, NO trading (%.0fs)', duration_s)
    logger.info('=' * 50)
    import datetime
    shadow_log = os.path.join(_STATE_DIR, 'shadow_log.jsonl')
    shadow_start = time.time()

    while time.time() - shadow_start < duration_s:
        try:
            t0 = time.time()
            # Scan candidates using production logic
            candidates = scan()
            scan_ms = (time.time() - t0) * 1000

            for c in (candidates if isinstance(candidates, list) else [candidates] if candidates else []):
                if c is None:
                    continue
                sym = c.get('sym', '?')
                addr = c.get('addr', '')
                info = price(addr) if addr else None
                if not info:
                    continue

                # Collect entry-decision data
                t1 = time.time()
                mkt_price = info.get('p', 0)
                c5 = info.get('c5', 0)
                bs = info.get('b5', 0) / max(info.get('s5', 1), 1)
                liq = info.get('liq', 0)

                # Slippage check (read-only quoter call)
                route = TOKEN_ROUTE.get(addr.lower(), 'pons')
                slip_ok, slip_pct = False, 100.0
                try:
                    e_bal = eth_bal()
                    deploy_est = e_bal - GAS_RESERVE if e_bal > GAS_RESERVE else 0.001
                    slip_ok, slip_pct = check_slippage(addr, deploy_est, sym, mkt_price, liquidity=liq)
                except Exception as ex:
                    slip_pct = -1  # Slippage check failed

                decision_ms = (time.time() - t1) * 1000

                # Entry predicate evaluation
                entry_pass = (
                    MIN_5M_ENTRY <= c5 <= MAX_5M_ENTRY
                    and bs >= MIN_BS_ENTRY
                    and liq >= MIN_LIQ
                    and slip_ok
                    and slip_pct * 2 <= MAX_RT_COST_EXPECTED
                )

                rejection = ''
                if c5 < MIN_5M_ENTRY: rejection = f'5m={c5:.1f}<{MIN_5M_ENTRY}'
                elif c5 > MAX_5M_ENTRY: rejection = f'5m={c5:.1f}>{MAX_5M_ENTRY}'
                elif bs < MIN_BS_ENTRY: rejection = f'bs={bs:.2f}<{MIN_BS_ENTRY}'
                elif liq < MIN_LIQ: rejection = f'liq=${liq:.0f}<${MIN_LIQ}'
                elif not slip_ok: rejection = f'slippage={slip_pct:.1f}%'
                elif slip_pct * 2 > MAX_RT_COST_EXPECTED: rejection = f'rt_cost={slip_pct*2:.1f}%>{MAX_RT_COST_EXPECTED}'

                # Log shadow observation
                obs = {
                    'ts': datetime.datetime.utcnow().isoformat(),
                    'token': sym,
                    'addr': addr,
                    'route': route,
                    'price': mkt_price,
                    'c5': c5,
                    'bs_ratio': round(bs, 3),
                    'liq': liq,
                    'slip_pct': round(slip_pct, 2),
                    'slip_ok': slip_ok,
                    'entry_pass': entry_pass,
                    'rejection': rejection,
                    'scan_ms': round(scan_ms, 1),
                    'decision_ms': round(decision_ms, 1),
                }
                logger.info('SHADOW: %s c5=%+.1f%% bs=%.2f liq=$%.0f slip=%.1f%% → %s %s',
                           sym, c5, bs, liq, slip_pct,
                           'PASS' if entry_pass else 'REJECT',
                           rejection)
                try:
                    with open(shadow_log, 'a') as f:
                        f.write(json.dumps(obs) + '\n')
                except Exception:
                    pass

        except Exception as ex:
            logger.error('SHADOW ERR: %s', ex)

        time.sleep(SCAN_INTERVAL)

    elapsed = time.time() - shadow_start
    logger.info('SHADOW MODE complete — ran %.0fs, log at %s', elapsed, shadow_log)


# ═══ Wallet-changing RPC deny list for helpers ═══
_DENIED_RPC_METHODS = frozenset([
    'eth_sendTransaction', 'eth_sendRawTransaction',
    'eth_sign', 'personal_sign', 'eth_signTransaction',
    'eth_signTypedData', 'eth_signTypedData_v4',
])


if __name__ == '__main__':
    if '--status' in sys.argv:
        _init_wallet()
        e = eth_bal()
        sb = spy_bal()
        sb_usd = spy_bal_usd()
        ub = usdg_bal()
        print(f'ETH:  {e:.6f} (${e*ETH_USD:.2f})')
        print(f'SPY:  {sb:.6f} (~${sb_usd:.2f})')
        print(f'USDG: {ub:.2f}')
        print(f'Total: ~${e*ETH_USD + sb_usd + ub:.2f}')
    elif '--shadow' in sys.argv:
        # Shadow mode — credential-free, read-only provider enforced
        _init_shadow_wallet()
        dur_idx = sys.argv.index('--shadow') + 1
        dur = int(sys.argv[dur_idx]) if dur_idx < len(sys.argv) and sys.argv[dur_idx].isdigit() else 600
        shadow(duration_s=dur)
    else:
        _init_wallet()
        main()
