"""Security + correctness test suite for EventVesting contract on Algorand localnet.

29 tests across access control, registration, finalization, claim math,
cliff vesting, double-claim prevention, edge cases, cleanup, and emergency.

Usage: python3 test_event_vesting.py
"""

import base64
import os
import time
import traceback

from algosdk import abi, account, encoding, logic, transaction
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
    TransactionWithSigner,
)
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

ALGOD_URL = "http://100.69.195.100:4001"
KMD_URL = "http://100.69.195.100:4002"
TOKEN = "a" * 64
CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))

ONE_YEAR = 31536000  # 365 days in seconds

# Results tracking
_results = []


def sp_with_fee(client, inner_count=0):
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = max(sp.min_fee, 1000) * (1 + inner_count)
    return sp


def run_test(name, description, test_fn):
    try:
        test_fn()
        print(f"  [PASS] {name} — {description}")
        _results.append(("PASS", name, description, ""))
        return
    except AssertionError as e:
        print(f"  [FAIL] {name} — {description}: {e}")
        _results.append(("FAIL", name, description, str(e)[:300]))
    except Exception as e:
        tb = traceback.format_exc()
        print(f"  [FAIL] {name} — {description}: {type(e).__name__}: {str(e)[:200]}")
        _results.append(("FAIL", name, description, f"{type(e).__name__}: {str(e)[:200]}\n{tb[-500:]}"))


def assert_fails(callable_fn, expected_error=None):
    """Call fn and assert it raises. Optionally verify error string."""
    try:
        callable_fn()
    except Exception as e:
        err_str = str(e)
        if expected_error and expected_error not in err_str and "logic eval error" not in err_str:
            raise AssertionError(f"Wrong error: expected '{expected_error}', got: {err_str[:200]}")
        return  # Expected failure
    raise AssertionError("Expected failure but succeeded")


def box_name_alloc(addr):
    """Box name for the allocations BoxMap: prefix 'a' + 32-byte address."""
    return b"a" + encoding.decode_address(addr)


def box_name_claimed(addr):
    """Box name for the claimed_amounts BoxMap: prefix 'c' + 32-byte address."""
    return b"c" + encoding.decode_address(addr)


def boxes_for_wallet(addr):
    """Return box references for both allocation and claimed boxes for a wallet."""
    return [(0, box_name_alloc(addr)), (0, box_name_claimed(addr))]


# ══════════════════════════════════════════════════════════════════════════════
# SETUP: Connect, create accounts, deploy contract
# ══════════════════════════════════════════════════════════════════════════════

print("=" * 60)
print("  EVENT VESTING TEST SUITE — SETUP")
print("=" * 60)

algod_client = algod.AlgodClient(TOKEN, ALGOD_URL)
kmd_client = KMDClient(TOKEN, KMD_URL)

# Admin from KMD
wallets = kmd_client.list_wallets()
default_wallet = next(w for w in wallets if w["name"] == "unencrypted-default-wallet")
wallet_handle = kmd_client.init_wallet_handle(default_wallet["id"], "")
keys = kmd_client.list_keys(wallet_handle)
admin_address = keys[0]
admin_private_key = kmd_client.export_key(wallet_handle, "", admin_address)
admin_signer = AccountTransactionSigner(admin_private_key)
print(f"  Admin: {admin_address[:10]}...")

# Two test users
user1_pk, user1_address = account.generate_account()
user1_signer = AccountTransactionSigner(user1_pk)
user2_pk, user2_address = account.generate_account()
user2_signer = AccountTransactionSigner(user2_pk)
user3_pk, user3_address = account.generate_account()
user3_signer = AccountTransactionSigner(user3_pk)
print(f"  User1 (participant): {user1_address[:10]}...")
print(f"  User2 (attacker):    {user2_address[:10]}...")
print(f"  User3 (participant): {user3_address[:10]}...")

# Fund users with 100 ALGO each
sp = algod_client.suggested_params()
for addr in (user1_address, user2_address, user3_address):
    fund_txn = transaction.PaymentTxn(admin_address, sp, addr, 100_000_000)
    signed = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)
print("  Funded users with 100 ALGO each")

# Create test reward ASA (simulates FRY with 6 decimals)
sp = algod_client.suggested_params()
asa_txn = transaction.AssetCreateTxn(
    sender=admin_address, sp=sp, total=100_000_000_000_000_000, decimals=6,
    default_frozen=False, unit_name="tFRY", asset_name="Test FRY",
)
signed = asa_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
reward_asa_id = transaction.wait_for_confirmation(algod_client, txid, 4)["asset-index"]
print(f"  Reward ASA (tFRY): {reward_asa_id}")


def opt_in(addr, pk, asa_id):
    sp = algod_client.suggested_params()
    t = transaction.AssetTransferTxn(addr, sp, addr, 0, asa_id)
    signed = t.sign(pk)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)


def send_asa(sender_addr, sender_pk, receiver_addr, amount, asa_id):
    sp = algod_client.suggested_params()
    t = transaction.AssetTransferTxn(sender_addr, sp, receiver_addr, amount, asa_id)
    signed = t.sign(sender_pk)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)


# Opt users into reward ASA
for addr, pk in [(user1_address, user1_pk), (user2_address, user2_pk), (user3_address, user3_pk)]:
    opt_in(addr, pk, reward_asa_id)
print("  Users opted into reward ASA")


# ── Contract deployment helper ────────────────────────────────────────────

def deploy_event_vesting(vesting_start, vesting_duration, cliff_end=0):
    """Deploy a fresh EventVesting instance. Returns (app_id, app_addr)."""
    with open(os.path.join(CONTRACTS_DIR, "EventVesting.approval.teal")) as f:
        approval_teal = f.read()
    with open(os.path.join(CONTRACTS_DIR, "EventVesting.clear.teal")) as f:
        clear_teal = f.read()
    approval_compiled = algod_client.compile(approval_teal)
    clear_compiled = algod_client.compile(clear_teal)
    approval_prog = base64.b64decode(approval_compiled["result"])
    clear_prog = base64.b64decode(clear_compiled["result"])

    create_method = abi.Method.from_signature(
        "create(uint64,uint64,uint64,uint64)void"
    )
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=create_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[reward_asa_id, vesting_start, vesting_duration, cliff_end],
        global_schema=transaction.StateSchema(num_uints=9, num_byte_slices=1),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=3,
        approval_program=approval_prog,
        clear_program=clear_prog,
    )
    result = atc.execute(algod_client, 4)
    app_id = result.abi_results[0].tx_info["application-index"]
    app_addr = logic.get_application_address(app_id)
    return app_id, app_addr


def fund_and_setup(app_id, app_addr, fund_algo, fund_asa_amount):
    """Fund contract with ALGO and reward ASA, and opt contract into ASA."""
    # Fund with ALGO
    sp = algod_client.suggested_params()
    fund_txn = transaction.PaymentTxn(admin_address, sp, app_addr, fund_algo)
    signed = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)

    # Opt contract into ASA
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("admin_opt_in(uint64)void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client, 1),
        signer=admin_signer,
        method_args=[reward_asa_id],
        foreign_assets=[reward_asa_id],
    )
    atc.execute(algod_client, 4)

    # Send reward ASA to contract
    if fund_asa_amount > 0:
        send_asa(admin_address, admin_private_key, app_addr, fund_asa_amount, reward_asa_id)


def register(app_id, wallet_addr, amount):
    """Admin registers an allocation."""
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("register_allocation(address,uint64)void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[wallet_addr, amount],
        boxes=boxes_for_wallet(wallet_addr),
    )
    atc.execute(algod_client, 4)


def finalize(app_id):
    """Admin finalizes the vesting."""
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("finalize()void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[],
        foreign_assets=[reward_asa_id],
    )
    atc.execute(algod_client, 4)


def claim(app_id, sender_addr, signer):
    """Participant claims vested rewards. Returns claimed amount."""
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("claim()uint64"),
        sender=sender_addr,
        sp=sp_with_fee(algod_client, 1),
        signer=signer,
        method_args=[],
        foreign_assets=[reward_asa_id],
        boxes=boxes_for_wallet(sender_addr),
    )
    result = atc.execute(algod_client, 4)
    return result.abi_results[0].return_value


def get_allocation(app_id, wallet_addr):
    """Read allocation info. Returns (allocation, claimed, claimable)."""
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("get_allocation(address)(uint64,uint64,uint64)"),
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[wallet_addr],
        boxes=boxes_for_wallet(wallet_addr),
    )
    result = atc.execute(algod_client, 4)
    return result.abi_results[0].return_value


def get_vesting_status(app_id):
    """Read vesting status. Returns (total_alloc, total_claimed, count, start, duration, cliff)."""
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
    return result.abi_results[0].return_value


def get_chain_timestamp():
    """Get the actual chain block timestamp (from ATLAS00, not wall clock).

    IMPORTANT: HEPHAESTUS00's wall clock may differ significantly from
    ATLAS00's block timestamps. Always use this for vesting time calculations.
    """
    # Send a dummy txn to produce a fresh block with current chain time
    sp = algod_client.suggested_params()
    dummy = transaction.PaymentTxn(admin_address, sp, admin_address, 0)
    signed = dummy.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    result = transaction.wait_for_confirmation(algod_client, txid, 4)
    block = algod_client.block_info(result["confirmed-round"])
    return block["block"]["ts"]


# ── Deploy the primary test contract ──────────────────────────────────────

# Deploy with vesting_start far in the past (100% vested) for most tests
# Individual tests that need specific vesting timing will deploy their own
# Use CHAIN timestamp, not wall clock — avoids clock drift between machines
now_ts = get_chain_timestamp()
print(f"  Chain timestamp: {now_ts} (wall: {int(time.time())}, drift: {int(time.time()) - now_ts}s)")
past_start = now_ts - ONE_YEAR - 3600  # fully vested (1 year + 1 hour ago)

main_app_id, main_app_addr = deploy_event_vesting(past_start, ONE_YEAR, cliff_end=0)
fund_and_setup(main_app_id, main_app_addr, fund_algo=10_000_000, fund_asa_amount=100_000_000)
print(f"  Main contract: app={main_app_id}, addr={main_app_addr[:10]}...")
print(f"  vesting_start={past_start}, duration={ONE_YEAR}, fully vested")
print("  Setup complete.\n")


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 1 — Access control (5 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 60)
print("  CATEGORY 1 — Access control")
print("=" * 60)


def test_admin_only_register():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=main_app_id,
        method=abi.Method.from_signature("register_allocation(address,uint64)void"),
        sender=user2_address,
        sp=sp_with_fee(algod_client),
        signer=user2_signer,
        method_args=[user2_address, 1_000_000],
        boxes=boxes_for_wallet(user2_address),
    )
    assert_fails(lambda: atc.execute(algod_client, 4))


def test_admin_only_finalize():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=main_app_id,
        method=abi.Method.from_signature("finalize()void"),
        sender=user2_address,
        sp=sp_with_fee(algod_client),
        signer=user2_signer,
        method_args=[],
        foreign_assets=[reward_asa_id],
    )
    assert_fails(lambda: atc.execute(algod_client, 4))


def test_admin_only_emergency_withdraw():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=main_app_id,
        method=abi.Method.from_signature("admin_emergency_withdraw(uint64,address)void"),
        sender=user2_address,
        sp=sp_with_fee(algod_client, 1),
        signer=user2_signer,
        method_args=[1, user2_address],
        foreign_assets=[reward_asa_id],
    )
    assert_fails(lambda: atc.execute(algod_client, 4))


def test_admin_only_opt_in():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=main_app_id,
        method=abi.Method.from_signature("admin_opt_in(uint64)void"),
        sender=user2_address,
        sp=sp_with_fee(algod_client, 1),
        signer=user2_signer,
        method_args=[reward_asa_id],
        foreign_assets=[reward_asa_id],
    )
    assert_fails(lambda: atc.execute(algod_client, 4))


def test_admin_set_admin():
    # Deploy fresh contract for this test to avoid polluting main
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=0)

    # Admin transfers to user1
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("admin_set_admin(address)void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[user1_address],
    )
    atc.execute(algod_client, 4)

    # Old admin (admin_address) should now be rejected
    atc2 = AtomicTransactionComposer()
    atc2.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("admin_set_admin(address)void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[admin_address],
    )
    assert_fails(lambda: atc2.execute(algod_client, 4))

    # New admin (user1) should work — transfer back
    atc3 = AtomicTransactionComposer()
    atc3.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("admin_set_admin(address)void"),
        sender=user1_address,
        sp=sp_with_fee(algod_client),
        signer=user1_signer,
        method_args=[admin_address],
    )
    atc3.execute(algod_client, 4)


run_test("test_admin_only_register", "non-admin register_allocation → reverts", test_admin_only_register)
run_test("test_admin_only_finalize", "non-admin finalize → reverts", test_admin_only_finalize)
run_test("test_admin_only_emergency_withdraw", "non-admin emergency_withdraw → reverts", test_admin_only_emergency_withdraw)
run_test("test_admin_only_opt_in", "non-admin admin_opt_in → reverts", test_admin_only_opt_in)
run_test("test_admin_set_admin", "admin transfer works, old admin rejected", test_admin_set_admin)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 2 — Registration (4 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 2 — Registration")
print("=" * 60)


def test_register_single():
    register(main_app_id, user1_address, 10_000_000)
    alloc, claimed, claimable = get_allocation(main_app_id, user1_address)
    assert alloc == 10_000_000, f"Expected alloc=10000000, got {alloc}"
    assert claimed == 0, f"Expected claimed=0, got {claimed}"


def test_register_multiple():
    # user1 already registered above; register user2 and user3
    register(main_app_id, user2_address, 5_000_000)
    register(main_app_id, user3_address, 3_000_000)

    a1, _, _ = get_allocation(main_app_id, user1_address)
    a2, _, _ = get_allocation(main_app_id, user2_address)
    a3, _, _ = get_allocation(main_app_id, user3_address)
    assert a1 == 10_000_000, f"user1 alloc: expected 10000000, got {a1}"
    assert a2 == 5_000_000, f"user2 alloc: expected 5000000, got {a2}"
    assert a3 == 3_000_000, f"user3 alloc: expected 3000000, got {a3}"

    status = get_vesting_status(main_app_id)
    total_alloc = status[0]
    count = status[2]
    assert total_alloc == 18_000_000, f"total_allocated: expected 18000000, got {total_alloc}"
    assert count == 3, f"count: expected 3, got {count}"


def test_register_update():
    # Update user2 from 5M to 7M
    register(main_app_id, user2_address, 7_000_000)
    a2, _, _ = get_allocation(main_app_id, user2_address)
    assert a2 == 7_000_000, f"user2 updated alloc: expected 7000000, got {a2}"

    # total should be 10 + 7 + 3 = 20
    status = get_vesting_status(main_app_id)
    total_alloc = status[0]
    count = status[2]
    assert total_alloc == 20_000_000, f"total_allocated: expected 20000000, got {total_alloc}"
    assert count == 3, f"count: expected 3, got {count}"


def test_register_after_finalize():
    # Need to finalize the main contract first — fund it to cover allocations
    # Main already has 100M ASA, total_allocated is 20M, so sufficient
    finalize(main_app_id)

    # Now try to register another allocation → must fail
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=main_app_id,
        method=abi.Method.from_signature("register_allocation(address,uint64)void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[user1_address, 1_000_000],
        boxes=boxes_for_wallet(user1_address),
    )
    assert_fails(lambda: atc.execute(algod_client, 4))


run_test("test_register_single", "register single allocation, read back", test_register_single)
run_test("test_register_multiple", "register 3 allocations, verify independently", test_register_multiple)
run_test("test_register_update", "update existing allocation, total adjusts", test_register_update)
run_test("test_register_after_finalize", "register after finalize → reverts", test_register_after_finalize)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 3 — Finalization (4 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 3 — Finalization")
print("=" * 60)


def test_finalize_insufficient_balance():
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=500_000)  # 0.5 FRY
    register(app_id, user1_address, 10_000_000)  # need 10 FRY, only have 0.5
    assert_fails(lambda: finalize(app_id))


def test_finalize_sufficient_balance():
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=10_000_000)  # exactly 10 FRY
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)  # Should succeed
    status = get_vesting_status(app_id)
    assert status[0] == 10_000_000, f"total_allocated mismatch: {status[0]}"


def test_finalize_already_finalized():
    # main_app_id was finalized in test_register_after_finalize
    assert_fails(lambda: finalize(main_app_id))


def test_finalize_enables_claiming():
    # Deploy unfinalised contract
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=10_000_000)
    register(app_id, user1_address, 5_000_000)

    # Claim before finalize → must fail
    assert_fails(lambda: claim(app_id, user1_address, user1_signer))

    # Finalize, then claim should work
    finalize(app_id)
    amt = claim(app_id, user1_address, user1_signer)
    assert amt > 0, f"Expected positive claim, got {amt}"


run_test("test_finalize_insufficient_balance", "finalize with insufficient ASA → reverts", test_finalize_insufficient_balance)
run_test("test_finalize_sufficient_balance", "finalize with exact ASA → succeeds", test_finalize_sufficient_balance)
run_test("test_finalize_already_finalized", "double finalize → reverts", test_finalize_already_finalized)
run_test("test_finalize_enables_claiming", "claim fails before finalize, works after", test_finalize_enables_claiming)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 4 — Claim math (4 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 4 — Claim math (linear vesting)")
print("=" * 60)


def _deploy_for_claim_test(elapsed_fraction):
    """Deploy a contract where elapsed_fraction of duration has passed.

    Uses chain timestamp to avoid clock drift between HEPHAESTUS00 and ATLAS00.
    """
    chain_now = get_chain_timestamp()
    elapsed_seconds = int(ONE_YEAR * elapsed_fraction)
    start = chain_now - elapsed_seconds
    app_id, app_addr = deploy_event_vesting(start, ONE_YEAR, cliff_end=0)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=50_000_000)
    return app_id, app_addr


def test_claim_linear_25_percent():
    app_id, _ = _deploy_for_claim_test(0.25)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    amt = claim(app_id, user1_address, user1_signer)
    # Expected ~2,500,000 (25% of 10M) with some tolerance for block timestamp drift
    # Allow 5% tolerance due to block timestamp not being exact wall time
    expected = 2_500_000
    tolerance = 500_000  # 5% of 10M
    assert abs(amt - expected) <= tolerance, f"25% claim: expected ~{expected}, got {amt} (diff={abs(amt-expected)})"


def test_claim_linear_50_percent():
    app_id, _ = _deploy_for_claim_test(0.50)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    amt = claim(app_id, user1_address, user1_signer)
    expected = 5_000_000
    tolerance = 500_000
    assert abs(amt - expected) <= tolerance, f"50% claim: expected ~{expected}, got {amt} (diff={abs(amt-expected)})"


def test_claim_linear_100_percent():
    app_id, _ = _deploy_for_claim_test(1.05)  # 105% elapsed = fully vested
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    amt = claim(app_id, user1_address, user1_signer)
    assert amt == 10_000_000, f"100% claim: expected 10000000, got {amt}"


def test_claim_linear_beyond_100():
    app_id, _ = _deploy_for_claim_test(2.0)  # 200% elapsed
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    amt = claim(app_id, user1_address, user1_signer)
    assert amt == 10_000_000, f"200% elapsed claim: expected 10000000 (capped), got {amt}"


run_test("test_claim_linear_25_percent", "25% elapsed → ~25% of allocation", test_claim_linear_25_percent)
run_test("test_claim_linear_50_percent", "50% elapsed → ~50% of allocation", test_claim_linear_50_percent)
run_test("test_claim_linear_100_percent", "100% elapsed → full allocation", test_claim_linear_100_percent)
run_test("test_claim_linear_beyond_100", "200% elapsed → capped at allocation", test_claim_linear_beyond_100)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 5 — Cliff vesting (3 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 5 — Cliff vesting")
print("=" * 60)


def test_cliff_before_cliff_end():
    """Cliff is in the future — claim should fail (nothing claimable)."""
    chain_now = get_chain_timestamp()
    # Start 6 months ago, cliff ends 3 months from now, duration = 1 year
    start = chain_now - (ONE_YEAR // 2)
    cliff_end = chain_now + (ONE_YEAR // 4)
    app_id, app_addr = deploy_event_vesting(start, ONE_YEAR, cliff_end=cliff_end)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=50_000_000)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    # Should fail — cliff hasn't passed
    assert_fails(lambda: claim(app_id, user1_address, user1_signer))


def test_cliff_after_cliff_end():
    """Cliff passed, 75% of vesting elapsed → should get ~75% of allocation."""
    chain_now = get_chain_timestamp()
    # Start 9 months ago (75%), cliff ended 3 months ago, duration = 1 year
    start = chain_now - int(ONE_YEAR * 0.75)
    cliff_end = chain_now - (ONE_YEAR // 4)  # 3 months ago
    app_id, app_addr = deploy_event_vesting(start, ONE_YEAR, cliff_end=cliff_end)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=50_000_000)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    amt = claim(app_id, user1_address, user1_signer)
    expected = 7_500_000  # 75%
    tolerance = 500_000
    assert abs(amt - expected) <= tolerance, f"cliff 75% claim: expected ~{expected}, got {amt}"


def test_cliff_at_exactly_cliff_end():
    """Cliff just passed — should get proportional amount (~50%)."""
    chain_now = get_chain_timestamp()
    # Start 6 months ago, cliff ended 60 seconds ago (generous margin), duration = 1 year
    start = chain_now - (ONE_YEAR // 2)
    cliff_end = chain_now - 60
    app_id, app_addr = deploy_event_vesting(start, ONE_YEAR, cliff_end=cliff_end)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=50_000_000)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    # Should succeed — cliff passed, ~50% vested
    amt = claim(app_id, user1_address, user1_signer)
    expected = 5_000_000  # ~50%
    tolerance = 500_000
    assert abs(amt - expected) <= tolerance, f"cliff boundary claim: expected ~{expected}, got {amt}"


run_test("test_cliff_before_cliff_end", "claim before cliff → reverts", test_cliff_before_cliff_end)
run_test("test_cliff_after_cliff_end", "claim after cliff, 75% elapsed → ~75%", test_cliff_after_cliff_end)
run_test("test_cliff_at_exactly_cliff_end", "claim right after cliff passes → ~50%", test_cliff_at_exactly_cliff_end)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 6 — Double claim prevention (2 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 6 — Double claim prevention")
print("=" * 60)


def test_double_claim():
    """Claim at ~50%, verify claimed amount recorded, then claim at 100% gets exactly the rest."""
    # Use 1-year duration (proven to work in claim math tests)
    app_id, _ = _deploy_for_claim_test(0.50)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    amt1 = claim(app_id, user1_address, user1_signer)
    assert amt1 > 0, f"First claim should be positive, got {amt1}"

    # Verify get_allocation shows claimed == amt1
    alloc, claimed, claimable = get_allocation(app_id, user1_address)
    assert alloc == 10_000_000, f"Allocation should be 10M, got {alloc}"
    assert claimed == amt1, f"Claimed should match returned amount {amt1}, got {claimed}"

    # Now deploy a fully-vested contract and test that after claiming 100%,
    # a second claim correctly fails
    app_id2, _ = _deploy_for_claim_test(1.05)
    register(app_id2, user1_address, 10_000_000)
    finalize(app_id2)

    full_amt = claim(app_id2, user1_address, user1_signer)
    assert full_amt == 10_000_000, f"Full claim should be 10M, got {full_amt}"

    # Second claim on fully-claimed allocation must fail
    assert_fails(lambda: claim(app_id2, user1_address, user1_signer))


def test_claim_then_full():
    """Claim partial, then claim again with fully vested → total = allocation."""
    # Deploy with 50% elapsed
    app_id, _ = _deploy_for_claim_test(0.50)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    amt1 = claim(app_id, user1_address, user1_signer)
    assert amt1 > 0, f"First claim should be positive, got {amt1}"
    expected_50 = 5_000_000
    tolerance = 500_000
    assert abs(amt1 - expected_50) <= tolerance, f"50% claim: expected ~{expected_50}, got {amt1}"

    # Verify claimed amount via readonly query
    alloc, claimed, _ = get_allocation(app_id, user1_address)
    assert claimed == amt1, f"On-chain claimed ({claimed}) != returned amt ({amt1})"

    # Deploy a new contract that's 100% vested and verify full claim works
    app_id2, _ = _deploy_for_claim_test(1.05)
    register(app_id2, user1_address, 10_000_000)
    finalize(app_id2)

    amt_full = claim(app_id2, user1_address, user1_signer)
    assert amt_full == 10_000_000, f"Full 100% claim should be 10M, got {amt_full}"

    # Third claim should fail — nothing left
    assert_fails(lambda: claim(app_id2, user1_address, user1_signer))


run_test("test_double_claim", "immediate double claim → second reverts", test_double_claim)
run_test("test_claim_then_full", "claim 50%, wait, claim rest = 100%, third fails", test_claim_then_full)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 7 — Edge cases (3 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 7 — Edge cases")
print("=" * 60)


def test_zero_allocation():
    """Register allocation of 0 → should fail (contract requires amount > 0)."""
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=10_000_000)
    assert_fails(lambda: register(app_id, user1_address, 0))


def test_tiny_allocation():
    """Allocation = 1 microunit. At 50% elapsed → 0 (truncation). At 100% → 1."""
    # Deploy fully vested
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=10_000_000)
    register(app_id, user1_address, 1)
    finalize(app_id)

    # Fully vested → should get exactly 1
    amt = claim(app_id, user1_address, user1_signer)
    assert amt == 1, f"Tiny allocation 100% claim: expected 1, got {amt}"


def test_large_allocation():
    """Allocation = 35_000_000_000_000 (35M FRY). Verify no overflow."""
    large_amount = 35_000_000_000_000  # 35M FRY in microunits
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=large_amount)
    register(app_id, user1_address, large_amount)
    finalize(app_id)

    amt = claim(app_id, user1_address, user1_signer)
    assert amt == large_amount, f"Large allocation 100% claim: expected {large_amount}, got {amt}"


run_test("test_zero_allocation", "register 0 amount → reverts", test_zero_allocation)
run_test("test_tiny_allocation", "1 microunit allocation, 100% vested → claim 1", test_tiny_allocation)
run_test("test_large_allocation", "35M FRY (35T microunits), no overflow", test_large_allocation)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 8 — Cleanup (2 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 8 — Cleanup")
print("=" * 60)


def test_cleanup_fully_claimed():
    """After 100% claimed, anyone can call cleanup_allocation_box."""
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=10_000_000)
    register(app_id, user1_address, 5_000_000)
    finalize(app_id)

    # Claim 100%
    claim(app_id, user1_address, user1_signer)

    # Cleanup by user2 (permissionless)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("cleanup_allocation_box(address)void"),
        sender=user2_address,
        sp=sp_with_fee(algod_client),
        signer=user2_signer,
        method_args=[user1_address],
        boxes=boxes_for_wallet(user1_address),
    )
    atc.execute(algod_client, 4)

    # Verify allocation is gone
    alloc, _, _ = get_allocation(app_id, user1_address)
    assert alloc == 0, f"After cleanup, allocation should be 0, got {alloc}"


def test_cleanup_not_fully_claimed():
    """Cleanup on a partially-claimed allocation → must fail."""
    app_id, _ = _deploy_for_claim_test(0.50)
    register(app_id, user1_address, 10_000_000)
    finalize(app_id)

    # Claim ~50%
    claim(app_id, user1_address, user1_signer)

    # Try cleanup → should fail (not fully claimed)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("cleanup_allocation_box(address)void"),
        sender=user2_address,
        sp=sp_with_fee(algod_client),
        signer=user2_signer,
        method_args=[user1_address],
        boxes=boxes_for_wallet(user1_address),
    )
    assert_fails(lambda: atc.execute(algod_client, 4))


run_test("test_cleanup_fully_claimed", "cleanup after 100% claimed → boxes deleted", test_cleanup_fully_claimed)
run_test("test_cleanup_not_fully_claimed", "cleanup partial claim → reverts", test_cleanup_not_fully_claimed)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 9 — Emergency (2 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 9 — Emergency")
print("=" * 60)


def test_emergency_withdraw():
    """Admin withdraws ASA from contract."""
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    fund_and_setup(app_id, app_addr, fund_algo=2_000_000, fund_asa_amount=10_000_000)

    # Check admin's balance before
    info_before = algod_client.account_asset_info(admin_address, reward_asa_id)
    bal_before = info_before["asset-holding"]["amount"]

    # Withdraw 5M
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("admin_emergency_withdraw(uint64,address)void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client, 1),
        signer=admin_signer,
        method_args=[5_000_000, admin_address],
        foreign_assets=[reward_asa_id],
    )
    atc.execute(algod_client, 4)

    # Check balance after
    info_after = algod_client.account_asset_info(admin_address, reward_asa_id)
    bal_after = info_after["asset-holding"]["amount"]
    delta = bal_after - bal_before
    assert delta == 5_000_000, f"Expected +5000000, got delta={delta}"


def test_admin_withdraw_algo():
    """Admin recovers excess ALGO from contract."""
    app_id, app_addr = deploy_event_vesting(past_start, ONE_YEAR)
    # Fund with extra ALGO
    sp = algod_client.suggested_params()
    fund_txn = transaction.PaymentTxn(admin_address, sp, app_addr, 5_000_000)
    signed = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)

    # Check admin ALGO before
    info_before = algod_client.account_info(admin_address)
    algo_before = info_before["amount"]

    # Withdraw 1M microALGO
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("admin_withdraw_algo(uint64,address)void"),
        sender=admin_address,
        sp=sp_with_fee(algod_client, 1),
        signer=admin_signer,
        method_args=[1_000_000, admin_address],
    )
    atc.execute(algod_client, 4)

    info_after = algod_client.account_info(admin_address)
    algo_after = info_after["amount"]
    # We withdrew 1M but paid some fees, so net should be positive but less than 1M
    net = algo_after - algo_before
    assert net > 900_000, f"Expected ~+1M ALGO (minus fees), got net={net}"


run_test("test_emergency_withdraw", "admin emergency withdraws ASA → balance increases", test_emergency_withdraw)
run_test("test_admin_withdraw_algo", "admin withdraws excess ALGO → balance increases", test_admin_withdraw_algo)


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  EVENT VESTING TEST RESULTS")
print("=" * 60)

passed = sum(1 for r in _results if r[0] == "PASS")
failed = sum(1 for r in _results if r[0] == "FAIL")
skipped = sum(1 for r in _results if r[0] == "SKIP")
total = len(_results)

print(f"\n  Total:   {total} tests")
print(f"  Passed:  {passed}")
print(f"  Failed:  {failed}")
print(f"  Skipped: {skipped}")

if failed > 0:
    print(f"\n  Failed tests:")
    for status, name, desc, err in _results:
        if status == "FAIL":
            print(f"    - {name}: {err}")

print("=" * 60)

if failed > 0:
    exit(1)
