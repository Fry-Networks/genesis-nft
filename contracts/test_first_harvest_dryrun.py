"""First Harvest localnet dry-run — simulates the exact mainnet deployment.

Deploys EventVesting on localnet with production parameters:
  - 35,000,000 FRY (35T microunits) total pool
  - 8 participants with proportional allocations
  - vesting_start = 1743206400 (2026-03-29T00:00:00Z)
  - 12-month linear vesting, no cliff

Usage: python3 test_first_harvest_dryrun.py
"""

import base64
import math
import os
import sys

from algosdk import abi, account, encoding, logic, transaction
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
)
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

# ── Constants ────────────────────────────────────────────────────────────────

ALGOD_URL = "http://100.69.195.100:4001"
KMD_URL = "http://100.69.195.100:4002"
TOKEN = "a" * 64
CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))

# First Harvest parameters
VESTING_START = 1743206400   # 2026-03-29T00:00:00Z
VESTING_DURATION = 31536000  # 365 days
CLIFF_END = 0                # no cliff
TOTAL_POOL = 35_000_000_000_000  # 35M FRY in microunits

# 8 qualified participants — points from The First Harvest event
PARTICIPANT_POINTS = [1000, 112, 101, 86, 48, 40, 30, 27]
TOTAL_POINTS = sum(PARTICIPANT_POINTS)  # 1444

overall_pass = True


def fail(msg):
    global overall_pass
    overall_pass = False
    print(f"  [FAIL] {msg}")


def sp_with_fee(client, inner_count=0):
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = max(sp.min_fee, 1000) * (1 + inner_count)
    return sp


def box_name_alloc(addr):
    return b"a" + encoding.decode_address(addr)


def box_name_claimed(addr):
    return b"c" + encoding.decode_address(addr)


def boxes_for_wallet(addr):
    return [(0, box_name_alloc(addr)), (0, box_name_claimed(addr))]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONNECTION AND ACCOUNT SETUP
# ══════════════════════════════════════════════════════════════════════════════

print("=" * 60)
print("  FIRST HARVEST DRY-RUN — Setup")
print("=" * 60)

algod_client = algod.AlgodClient(TOKEN, ALGOD_URL)
kmd_client = KMDClient(TOKEN, KMD_URL)

status = algod_client.status()
print(f"  Localnet connected — round {status['last-round']}")

# Admin from KMD
wallets = kmd_client.list_wallets()
default_wallet = next(w for w in wallets if w["name"] == "unencrypted-default-wallet")
wallet_handle = kmd_client.init_wallet_handle(default_wallet["id"], "")
keys = kmd_client.list_keys(wallet_handle)
admin_address = keys[0]
admin_private_key = kmd_client.export_key(wallet_handle, "", admin_address)
admin_signer = AccountTransactionSigner(admin_private_key)
print(f"  Admin: {admin_address[:16]}...")

# Create test FRY ASA
sp = algod_client.suggested_params()
asa_txn = transaction.AssetCreateTxn(
    sender=admin_address, sp=sp,
    total=100_000_000_000_000_000,  # 100 quadrillion (plenty of headroom)
    decimals=6,
    default_frozen=False,
    unit_name="tFRY",
    asset_name="Test FRY (First Harvest Dry-Run)",
)
signed = asa_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
fry_asa_id = transaction.wait_for_confirmation(algod_client, txid, 4)["asset-index"]
print(f"  Test FRY ASA: {fry_asa_id}")

# Create 8 participant accounts
participants = []
print(f"  Creating 8 participant accounts...")
for i, pts in enumerate(PARTICIPANT_POINTS):
    pk, addr = account.generate_account()
    signer = AccountTransactionSigner(pk)
    participants.append({"pk": pk, "addr": addr, "signer": signer, "points": pts})

    # Fund with 10 ALGO
    sp = algod_client.suggested_params()
    fund_txn = transaction.PaymentTxn(admin_address, sp, addr, 10_000_000)
    signed_txn = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed_txn)
    transaction.wait_for_confirmation(algod_client, txid, 4)

    # Opt into FRY ASA
    sp = algod_client.suggested_params()
    opt_txn = transaction.AssetTransferTxn(addr, sp, addr, 0, fry_asa_id)
    signed_txn = opt_txn.sign(pk)
    txid = algod_client.send_transaction(signed_txn)
    transaction.wait_for_confirmation(algod_client, txid, 4)

print(f"  8 accounts funded and opted into FRY")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — CALCULATE ALLOCATIONS
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Allocation Calculation")
print("=" * 60)

allocations = []
for p in participants:
    alloc = math.floor(p["points"] / TOTAL_POINTS * TOTAL_POOL)
    allocations.append(alloc)
    p["allocation"] = alloc

sum_allocations = sum(allocations)
dust = TOTAL_POOL - sum_allocations

print(f"  Total points:      {TOTAL_POINTS}")
print(f"  Total pool:        {TOTAL_POOL:,} microFRY ({TOTAL_POOL / 1e6:,.0f} FRY)")
print(f"  Sum of allocations:{sum_allocations:,} microFRY")
print(f"  Dust remainder:    {dust:,} microFRY ({dust / 1e6:.6f} FRY)")
print()
print(f"  {'#':<4} {'Points':<8} {'Allocation (micro)':<26} {'Allocation (FRY)':<18}")
print(f"  {'—'*4} {'—'*8} {'—'*26} {'—'*18}")
for i, p in enumerate(participants):
    print(f"  {i+1:<4} {p['points']:<8} {p['allocation']:>24,}   {p['allocation']/1e6:>14,.6f}")

assert sum_allocations <= TOTAL_POOL, f"Sum {sum_allocations} > pool {TOTAL_POOL}"
print(f"\n  [OK] Sum ({sum_allocations:,}) <= pool ({TOTAL_POOL:,})")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — DEPLOY EVENT VESTING CONTRACT
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Deploy Contract")
print("=" * 60)

with open(os.path.join(CONTRACTS_DIR, "EventVesting.approval.teal")) as f:
    approval_teal = f.read()
with open(os.path.join(CONTRACTS_DIR, "EventVesting.clear.teal")) as f:
    clear_teal = f.read()

approval_compiled = algod_client.compile(approval_teal)
clear_compiled = algod_client.compile(clear_teal)
approval_prog = base64.b64decode(approval_compiled["result"])
clear_prog = base64.b64decode(clear_compiled["result"])

create_method = abi.Method.from_signature("create(uint64,uint64,uint64,uint64)void")
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=0,
    method=create_method,
    sender=admin_address,
    sp=sp_with_fee(algod_client),
    signer=admin_signer,
    method_args=[fry_asa_id, VESTING_START, VESTING_DURATION, CLIFF_END],
    global_schema=transaction.StateSchema(num_uints=9, num_byte_slices=1),
    local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
    extra_pages=3,
    approval_program=approval_prog,
    clear_program=clear_prog,
)
result = atc.execute(algod_client, 4)
app_id = result.abi_results[0].tx_info["application-index"]
app_addr = logic.get_application_address(app_id)
print(f"  Contract deployed: app_id={app_id}")
print(f"  Contract address:  {app_addr}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — FUND CONTRACT
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Fund Contract")
print("=" * 60)

# Send ALGO for MBR (base + ASA opt-in + 8 participants * 2 boxes each)
# Base MBR: 100,000 + ASA opt-in: 100,000 + 16 boxes * 18,900 each = ~502,400
# Add generous buffer: 2 ALGO total
algo_fund = 2_000_000
sp = algod_client.suggested_params()
fund_txn = transaction.PaymentTxn(admin_address, sp, app_addr, algo_fund)
signed_txn = fund_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed_txn)
transaction.wait_for_confirmation(algod_client, txid, 4)
print(f"  Funded with {algo_fund / 1e6:.1f} ALGO")

# Opt contract into FRY ASA
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=app_id,
    method=abi.Method.from_signature("admin_opt_in(uint64)void"),
    sender=admin_address,
    sp=sp_with_fee(algod_client, 1),
    signer=admin_signer,
    method_args=[fry_asa_id],
    foreign_assets=[fry_asa_id],
)
atc.execute(algod_client, 4)
print(f"  Contract opted into FRY ASA {fry_asa_id}")

# Transfer 35T microFRY to contract
sp = algod_client.suggested_params()
asa_fund_txn = transaction.AssetTransferTxn(
    admin_address, sp, app_addr, TOTAL_POOL, fry_asa_id
)
signed_txn = asa_fund_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed_txn)
transaction.wait_for_confirmation(algod_client, txid, 4)
print(f"  Transferred {TOTAL_POOL:,} microFRY ({TOTAL_POOL / 1e6:,.0f} FRY) to contract")

# Verify balance
app_info = algod_client.account_asset_info(app_addr, fry_asa_id)
contract_balance = app_info["asset-holding"]["amount"]
print(f"  Contract FRY balance: {contract_balance:,} microFRY")
assert contract_balance == TOTAL_POOL, f"Balance mismatch: {contract_balance} != {TOTAL_POOL}"
print(f"  [OK] Balance matches pool")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — REGISTER 8 ALLOCATIONS
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Register Allocations")
print("=" * 60)

register_method = abi.Method.from_signature("register_allocation(address,uint64)void")

for i, p in enumerate(participants):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=register_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[p["addr"], p["allocation"]],
        boxes=boxes_for_wallet(p["addr"]),
    )
    atc.execute(algod_client, 4)
    print(f"  Registered #{i+1}: {p['addr'][:12]}... → {p['allocation']:,} ({p['points']} pts)")

print(f"  [OK] All 8 allocations registered")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — FINALIZE
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Finalize")
print("=" * 60)

atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=app_id,
    method=abi.Method.from_signature("finalize()void"),
    sender=admin_address,
    sp=sp_with_fee(algod_client),
    signer=admin_signer,
    method_args=[],
    foreign_assets=[fry_asa_id],
)
atc.execute(algod_client, 4)
print(f"  [OK] Finalized — allocations locked, claiming enabled")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — READ BACK ALL ALLOCATIONS
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Verify Allocations")
print("=" * 60)

get_alloc_method = abi.Method.from_signature("get_allocation(address)(uint64,uint64,uint64)")

print(f"  {'#':<4} {'Points':<8} {'Expected':>24} {'On-Chain':>24} {'Match':<6}")
print(f"  {'—'*4} {'—'*8} {'—'*24} {'—'*24} {'—'*6}")

for i, p in enumerate(participants):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=get_alloc_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[p["addr"]],
        boxes=boxes_for_wallet(p["addr"]),
    )
    result = atc.execute(algod_client, 4)
    alloc_val, claimed_val, claimable_val = result.abi_results[0].return_value
    p["on_chain_alloc"] = alloc_val
    p["on_chain_claimed"] = claimed_val
    p["on_chain_claimable"] = claimable_val

    match = "OK" if alloc_val == p["allocation"] else "MISMATCH"
    if match != "OK":
        fail(f"Allocation mismatch for participant #{i+1}")
    print(f"  {i+1:<4} {p['points']:<8} {p['allocation']:>24,} {alloc_val:>24,} {match:<6}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — VESTING STATUS
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Vesting Status")
print("=" * 60)

atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=app_id,
    method=abi.Method.from_signature("get_vesting_status()(uint64,uint64,uint64,uint64,uint64,uint64)"),
    sender=admin_address,
    sp=sp_with_fee(algod_client),
    signer=admin_signer,
    method_args=[],
)
result = atc.execute(algod_client, 4)
total_alloc, total_claimed, count, v_start, v_duration, v_cliff = result.abi_results[0].return_value

print(f"  total_allocated:   {total_alloc:,} ({total_alloc / 1e6:,.0f} FRY)")
print(f"  total_claimed:     {total_claimed:,}")
print(f"  allocations_count: {count}")
print(f"  vesting_start:     {v_start} (2026-03-29T00:00:00Z)")
print(f"  vesting_duration:  {v_duration} ({v_duration // 86400} days)")
print(f"  cliff_end:         {v_cliff}")

assert total_alloc == sum_allocations, f"total_allocated {total_alloc} != sum {sum_allocations}"
assert count == 8, f"count {count} != 8"
assert v_start == VESTING_START, f"start {v_start} != {VESTING_START}"
assert v_duration == VESTING_DURATION, f"duration {v_duration} != {VESTING_DURATION}"
assert v_cliff == CLIFF_END, f"cliff {v_cliff} != {CLIFF_END}"
print(f"  [OK] All status fields match expected values")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — TEST CLAIM (Highest-allocation participant)
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN — Test Claim")
print("=" * 60)

claimer = participants[0]  # 1000 pts, highest allocation
print(f"  Claimer:     {claimer['addr'][:16]}...")
print(f"  Allocation:  {claimer['allocation']:,} microFRY ({claimer['allocation'] / 1e6:,.6f} FRY)")

# Get chain timestamp to calculate expected vested fraction
sp = algod_client.suggested_params()
dummy = transaction.PaymentTxn(admin_address, sp, admin_address, 0)
signed_txn = dummy.sign(admin_private_key)
txid = algod_client.send_transaction(signed_txn)
result = transaction.wait_for_confirmation(algod_client, txid, 4)
block = algod_client.block_info(result["confirmed-round"])
chain_now = block["block"]["ts"]

elapsed = max(0, chain_now - VESTING_START)
vested_pct = min(100.0, elapsed / VESTING_DURATION * 100)
expected_vested = min(claimer["allocation"], claimer["allocation"] * elapsed // VESTING_DURATION) if elapsed > 0 else 0
print(f"  Chain time:  {chain_now}")
print(f"  Elapsed:     {elapsed} seconds ({elapsed / 86400:.1f} days)")
print(f"  Vested %:    {vested_pct:.2f}%")
print(f"  Expected ~:  {expected_vested:,} microFRY ({expected_vested / 1e6:,.2f} FRY)")

# Execute claim
claim_method = abi.Method.from_signature("claim()uint64")
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=app_id,
    method=claim_method,
    sender=claimer["addr"],
    sp=sp_with_fee(algod_client, 1),
    signer=claimer["signer"],
    method_args=[],
    foreign_assets=[fry_asa_id],
    boxes=boxes_for_wallet(claimer["addr"]),
)
claim_result = atc.execute(algod_client, 4)
claimed_amount = claim_result.abi_results[0].return_value

print(f"  Claimed:     {claimed_amount:,} microFRY ({claimed_amount / 1e6:,.6f} FRY)")

# Verify claimed amount is reasonable
if claimed_amount == 0:
    fail("Claimed amount is 0 — vesting math may be wrong")
elif claimed_amount > claimer["allocation"]:
    fail(f"Claimed {claimed_amount} > allocation {claimer['allocation']}")
else:
    actual_pct = claimed_amount / claimer["allocation"] * 100
    print(f"  Actual %:    {actual_pct:.2f}%")
    # Allow 1% tolerance from expected
    if abs(actual_pct - vested_pct) > 1.0:
        fail(f"Claimed {actual_pct:.2f}% but expected ~{vested_pct:.2f}%")
    else:
        print(f"  [OK] Claim amount matches expected vested fraction")

# Verify allocation readback shows updated claimed
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=app_id,
    method=get_alloc_method,
    sender=admin_address,
    sp=sp_with_fee(algod_client),
    signer=admin_signer,
    method_args=[claimer["addr"]],
    boxes=boxes_for_wallet(claimer["addr"]),
)
result = atc.execute(algod_client, 4)
alloc_after, claimed_after, claimable_after = result.abi_results[0].return_value
print(f"  Post-claim:  alloc={alloc_after:,} claimed={claimed_after:,} claimable={claimable_after:,}")

if claimed_after != claimed_amount:
    fail(f"On-chain claimed ({claimed_after}) != returned ({claimed_amount})")
else:
    print(f"  [OK] On-chain claimed matches return value")


# Verify claimer's ASA balance increased
claimer_asa_info = algod_client.account_asset_info(claimer["addr"], fry_asa_id)
claimer_balance = claimer_asa_info["asset-holding"]["amount"]
if claimer_balance != claimed_amount:
    fail(f"Claimer ASA balance ({claimer_balance}) != claimed ({claimed_amount})")
else:
    print(f"  [OK] Claimer ASA balance matches claimed amount")


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  FIRST HARVEST DRY-RUN SUMMARY")
print("=" * 60)

print(f"  Contract App ID:     {app_id}")
print(f"  Test ASA ID:         {fry_asa_id}")
print(f"  Vesting Start:       2026-03-29T00:00:00Z ({VESTING_START})")
print(f"  Vesting Duration:    {VESTING_DURATION} seconds ({VESTING_DURATION // 86400} days)")
print(f"  Cliff:               None (linear from start)")
print(f"  Total Allocated:     {sum_allocations:,} microFRY ({sum_allocations / 1e6:,.0f} FRY)")
print(f"  Dust Remainder:      {dust:,} microFRY ({dust / 1e6:.6f} FRY)")
print(f"  Participants:        8")
print(f"  Finalized:           YES")
print(f"  Test Claim Amount:   {claimed_amount:,} microFRY ({claimed_amount / 1e6:,.6f} FRY)")
print(f"  Test Claim Wallet:   {claimer['addr'][:20]}...")
print(f"  Vested % at claim:   {vested_pct:.2f}%")
print(f"  Overall:             {'PASS' if overall_pass else 'FAIL'}")
print("=" * 60)

if not overall_pass:
    sys.exit(1)
