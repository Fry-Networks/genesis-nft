"""Security test suite for Genesis NFT contracts on Algorand localnet.

Runs ~35 negative/attack tests against freshly deployed GenesisNFT, FeeRouter,
and DistributionPool. Each test verifies that access control, input validation,
and ownership checks correctly reject unauthorized actions.

Usage: python3 test_security.py
"""

import base64
import os
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
ZERO_ADDR = encoding.encode_address(bytes(32))

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
        _results.append(("FAIL", name, description, str(e)[:200]))
    except Exception as e:
        print(f"  [FAIL] {name} — {description}: unexpected error: {type(e).__name__}: {str(e)[:150]}")
        _results.append(("FAIL", name, description, f"{type(e).__name__}: {str(e)[:150]}"))


def skip_test(name, description, reason):
    print(f"  [SKIP] {name} — {description}: {reason}")
    _results.append(("SKIP", name, description, reason))


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


# ══════════════════════════════════════════════════════════════════════════════
# SETUP: Connect, create accounts, deploy contracts, mint NFT, start epoch
# ══════════════════════════════════════════════════════════════════════════════

print("=" * 60)
print("  SECURITY TEST SUITE — SETUP")
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
automation_pk, automation_address = account.generate_account()
automation_signer = AccountTransactionSigner(automation_pk)
print(f"  User1 (holder):   {user1_address[:10]}...")
print(f"  User2 (attacker): {user2_address[:10]}...")
print(f"  Automation:       {automation_address[:10]}...")

# Fund users with 100 ALGO each
sp = algod_client.suggested_params()
for addr in (user1_address, user2_address, automation_address):
    fund_txn = transaction.PaymentTxn(admin_address, sp, addr, 100_000_000)
    signed = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)
print("  Funded users with 100 ALGO each")

# Create test USDC and FRY
sp = algod_client.suggested_params()
usdc_txn = transaction.AssetCreateTxn(
    sender=admin_address, sp=sp, total=10_000_000_000_000, decimals=6,
    default_frozen=False, unit_name="USDC", asset_name="Test USDC",
)
signed = usdc_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
usdc_asa_id = transaction.wait_for_confirmation(algod_client, txid, 4)["asset-index"]

sp = algod_client.suggested_params()
fry_txn = transaction.AssetCreateTxn(
    sender=admin_address, sp=sp, total=10_000_000_000_000_000, decimals=6,
    default_frozen=False, unit_name="FRY", asset_name="Test FRY",
)
signed = fry_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
fry_asa_id = transaction.wait_for_confirmation(algod_client, txid, 4)["asset-index"]
print(f"  USDC ASA={usdc_asa_id}, FRY ASA={fry_asa_id}")


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


# Opt users into USDC and FRY, fund them with USDC
for addr, pk in [(user1_address, user1_pk), (user2_address, user2_pk)]:
    opt_in(addr, pk, usdc_asa_id)
    opt_in(addr, pk, fry_asa_id)
send_asa(admin_address, admin_private_key, user1_address, 1_000_000_000, usdc_asa_id)  # 1000 USDC
send_asa(admin_address, admin_private_key, user2_address, 1_000_000_000, usdc_asa_id)  # 1000 USDC
print("  Users opted into USDC/FRY, each funded with 1000 USDC")


def deploy_contract(name, create_sig, method_args, num_uints, num_byte_slices):
    with open(os.path.join(CONTRACTS_DIR, f"{name}.approval.teal")) as f:
        approval_teal = f.read()
    with open(os.path.join(CONTRACTS_DIR, f"{name}.clear.teal")) as f:
        clear_teal = f.read()
    approval_compiled = algod_client.compile(approval_teal)
    clear_compiled = algod_client.compile(clear_teal)
    approval_prog = base64.b64decode(approval_compiled["result"])
    clear_prog = base64.b64decode(clear_compiled["result"])

    create_method = abi.Method.from_signature(create_sig)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=create_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=method_args,
        global_schema=transaction.StateSchema(num_uints=num_uints, num_byte_slices=num_byte_slices),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=3,
        approval_program=approval_prog,
        clear_program=clear_prog,
    )
    result = atc.execute(algod_client, 4)
    app_id = result.abi_results[0].tx_info["application-index"]
    app_addr = logic.get_application_address(app_id)
    return app_id, app_addr


# Deploy GenesisNFT
nft_app_id, nft_app_addr = deploy_contract(
    "GenesisNFT",
    "create(address,address,uint64,uint64,uint64,string,uint64)void",
    [admin_address, admin_address, 175_000_000, 1000, usdc_asa_id, "ipfs://QmTest/", 500],
    num_uints=6, num_byte_slices=3,
)
# Fund with 30 ALGO
sp = algod_client.suggested_params()
fund_txn = transaction.PaymentTxn(admin_address, sp, nft_app_addr, 30_000_000)
signed = fund_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
transaction.wait_for_confirmation(algod_client, txid, 4)
# Opt NFT contract into USDC
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=nft_app_id, method=abi.Method.from_signature("admin_opt_in(uint64)void"),
    sender=admin_address, sp=sp_with_fee(algod_client, 1), signer=admin_signer,
    method_args=[usdc_asa_id], foreign_assets=[usdc_asa_id],
)
atc.execute(algod_client, 4)
print(f"  GenesisNFT: app={nft_app_id}")

# Deploy FeeRouter
fr_app_id, fr_app_addr = deploy_contract(
    "FeeRouter",
    "create(address,address,address,uint64)void",
    [admin_address, admin_address, admin_address, 1000],
    num_uints=3, num_byte_slices=3,
)
print(f"  FeeRouter: app={fr_app_id}")

# Deploy DistributionPool
dp_app_id, dp_app_addr = deploy_contract(
    "DistributionPool",
    "create(uint64,uint64,uint64)void",
    [fry_asa_id, nft_app_id, 1000],
    num_uints=8, num_byte_slices=2,
)
# Fund with 5 ALGO
sp = algod_client.suggested_params()
fund_txn = transaction.PaymentTxn(admin_address, sp, dp_app_addr, 5_000_000)
signed = fund_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
transaction.wait_for_confirmation(algod_client, txid, 4)
# Opt DP into FRY
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=dp_app_id, method=abi.Method.from_signature("admin_opt_in(uint64)void"),
    sender=admin_address, sp=sp_with_fee(algod_client, 1), signer=admin_signer,
    method_args=[fry_asa_id], foreign_assets=[fry_asa_id],
)
atc.execute(algod_client, 4)
# Fund DP with 10M FRY for epoch
send_asa(admin_address, admin_private_key, dp_app_addr, 20_000_000, fry_asa_id)  # extra buffer
print(f"  DistributionPool: app={dp_app_id}, funded with 20M microFRY")

# Set automation address on DistributionPool
m_admin_set_automation = abi.Method.from_signature("admin_set_automation(address)void")
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=dp_app_id, method=m_admin_set_automation, sender=admin_address,
    sp=sp_with_fee(algod_client), signer=admin_signer,
    method_args=[automation_address],
)
atc.execute(algod_client, 4)
print(f"  Automation address set on DistributionPool")

# User1 mints NFT #1
token_id_1_bytes = (1).to_bytes(8, "big")
user1_addr_bytes = encoding.decode_address(user1_address)
mint_method = abi.Method.from_signature("mint(axfer)uint64")
payment_txn = transaction.AssetTransferTxn(
    sender=user1_address, sp=algod_client.suggested_params(),
    receiver=nft_app_addr, amt=175_000_000, index=usdc_asa_id,
)
atc = AtomicTransactionComposer()
atc.add_method_call(
    app_id=nft_app_id, method=mint_method, sender=user1_address,
    sp=sp_with_fee(algod_client, 1), signer=user1_signer,
    method_args=[TransactionWithSigner(payment_txn, user1_signer)],
    foreign_assets=[usdc_asa_id], accounts=[admin_address],
    boxes=[(0, b"o" + token_id_1_bytes), (0, b"b" + user1_addr_bytes)],
)
atc.execute(algod_client, 4)
print("  User1 minted NFT #1")

# Cache common method objects
m_owner_of = abi.Method.from_signature("arc72_ownerOf(uint64)address")
m_balance_of = abi.Method.from_signature("arc72_balanceOf(address)uint64")
m_transfer_from = abi.Method.from_signature("arc72_transferFrom(address,address,uint64)void")
m_approve = abi.Method.from_signature("arc72_approve(address,uint64)void")
m_set_approval_for_all = abi.Method.from_signature("arc72_setApprovalForAll(address,bool)void")
m_supports_interface = abi.Method.from_signature("supportsInterface(byte[4])bool")
m_admin_pause = abi.Method.from_signature("admin_pause(uint64)void")
m_admin_set_base_uri = abi.Method.from_signature("admin_set_base_uri(string)void")
m_admin_set_treasury_nft = abi.Method.from_signature("admin_set_treasury(address)void")
m_admin_set_mint_price = abi.Method.from_signature("admin_set_mint_price(uint64)void")
m_admin_withdraw = abi.Method.from_signature("admin_withdraw(uint64,uint64,address)void")
m_start_epoch = abi.Method.from_signature("start_epoch()uint64")
m_claim = abi.Method.from_signature("claim(uint64)uint64")
m_cancel_epoch = abi.Method.from_signature("cancel_epoch()void")
m_admin_set_nft_app_id = abi.Method.from_signature("admin_set_nft_app_id(uint64)void")
m_admin_withdraw_algo = abi.Method.from_signature("admin_withdraw_algo(uint64,address)void")
m_emergency_withdraw_fry = abi.Method.from_signature("admin_emergency_withdraw_fry(uint64,address)void")
m_fr_admin_set_pool_bps = abi.Method.from_signature("admin_set_pool_bps(uint64)void")
m_fr_admin_set_treasury = abi.Method.from_signature("admin_set_treasury(address)void")
m_fr_admin_set_pool = abi.Method.from_signature("admin_set_pool(address)void")

user2_addr_bytes = encoding.decode_address(user2_address)

print("  Setup complete.\n")

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 1 — GenesisNFT access control (8 tests, all must FAIL)
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 60)
print("  CATEGORY 1 — GenesisNFT access control")
print("=" * 60)


def _nft_admin_call_as_user2(method, args):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=method, sender=user2_address,
        sp=sp_with_fee(algod_client, 1), signer=user2_signer,
        method_args=args,
    )
    atc.execute(algod_client, 4)


run_test("test_nonadmin_pause", "user2 calls admin_pause → reverts",
         lambda: assert_fails(lambda: _nft_admin_call_as_user2(m_admin_pause, [1])))
run_test("test_nonadmin_unpause", "user2 calls admin_pause(0) → reverts",
         lambda: assert_fails(lambda: _nft_admin_call_as_user2(m_admin_pause, [0])))
run_test("test_nonadmin_set_base_uri", "user2 calls admin_set_base_uri → reverts",
         lambda: assert_fails(lambda: _nft_admin_call_as_user2(m_admin_set_base_uri, ["hack://"])))
run_test("test_nonadmin_set_treasury", "user2 calls admin_set_treasury → reverts",
         lambda: assert_fails(lambda: _nft_admin_call_as_user2(m_admin_set_treasury_nft, [user2_address])))
run_test("test_nonadmin_set_mint_price", "user2 calls admin_set_mint_price(0) → reverts",
         lambda: assert_fails(lambda: _nft_admin_call_as_user2(m_admin_set_mint_price, [0])))


def _nft_admin_withdraw_as_user2():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_admin_withdraw, sender=user2_address,
        sp=sp_with_fee(algod_client, 1), signer=user2_signer,
        method_args=[usdc_asa_id, 1, user2_address],
        foreign_assets=[usdc_asa_id],
    )
    atc.execute(algod_client, 4)


run_test("test_nonadmin_withdraw", "user2 calls admin_withdraw → reverts",
         lambda: assert_fails(_nft_admin_withdraw_as_user2))


def _nft_update_as_user2():
    with open(os.path.join(CONTRACTS_DIR, "GenesisNFT.approval.teal")) as f:
        approval_teal = f.read()
    with open(os.path.join(CONTRACTS_DIR, "GenesisNFT.clear.teal")) as f:
        clear_teal = f.read()
    approval_compiled = algod_client.compile(approval_teal)
    clear_compiled = algod_client.compile(clear_teal)
    sp = sp_with_fee(algod_client)
    t = transaction.ApplicationUpdateTxn(
        sender=user2_address, sp=sp, index=nft_app_id,
        approval_program=base64.b64decode(approval_compiled["result"]),
        clear_program=base64.b64decode(clear_compiled["result"]),
    )
    signed = t.sign(user2_pk)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)


def _nft_delete_as_user2():
    sp = sp_with_fee(algod_client)
    t = transaction.ApplicationDeleteTxn(sender=user2_address, sp=sp, index=nft_app_id)
    signed = t.sign(user2_pk)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)


run_test("test_nonadmin_update", "user2 tries ApplicationUpdate → reverts",
         lambda: assert_fails(_nft_update_as_user2))
run_test("test_nonadmin_delete", "user2 tries ApplicationDelete → reverts",
         lambda: assert_fails(_nft_delete_as_user2))


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 2 — Mint attacks (5 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 2 — Mint attacks")
print("=" * 60)


def _admin_pause_call(val):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_admin_pause, sender=admin_address,
        sp=sp_with_fee(algod_client), signer=admin_signer, method_args=[val],
    )
    atc.execute(algod_client, 4)


def _build_mint_call(signer_addr, signer, payment_txn, payment_signer, token_id_to_be):
    """Build and execute a mint with given payment + app call sender."""
    tid_bytes = token_id_to_be.to_bytes(8, "big")
    addr_bytes = encoding.decode_address(signer_addr)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=mint_method, sender=signer_addr,
        sp=sp_with_fee(algod_client, 1), signer=signer,
        method_args=[TransactionWithSigner(payment_txn, payment_signer)],
        foreign_assets=[usdc_asa_id], accounts=[admin_address],
        boxes=[(0, b"o" + tid_bytes), (0, b"b" + addr_bytes)],
    )
    atc.execute(algod_client, 4)


def test_mint_while_paused():
    _admin_pause_call(1)
    try:
        pay = transaction.AssetTransferTxn(
            sender=user2_address, sp=algod_client.suggested_params(),
            receiver=nft_app_addr, amt=175_000_000, index=usdc_asa_id,
        )
        assert_fails(lambda: _build_mint_call(user2_address, user2_signer, pay, user2_signer, 2))
    finally:
        _admin_pause_call(0)  # always unpause


def test_mint_wrong_asset():
    pay = transaction.AssetTransferTxn(
        sender=user2_address, sp=algod_client.suggested_params(),
        receiver=nft_app_addr, amt=175_000_000, index=fry_asa_id,  # FRY instead of USDC
    )
    assert_fails(lambda: _build_mint_call(user2_address, user2_signer, pay, user2_signer, 2))


def test_mint_underpayment():
    pay = transaction.AssetTransferTxn(
        sender=user2_address, sp=algod_client.suggested_params(),
        receiver=nft_app_addr, amt=174_999_999, index=usdc_asa_id,
    )
    assert_fails(lambda: _build_mint_call(user2_address, user2_signer, pay, user2_signer, 2))


def test_mint_overpayment():
    pay = transaction.AssetTransferTxn(
        sender=user2_address, sp=algod_client.suggested_params(),
        receiver=nft_app_addr, amt=175_000_001, index=usdc_asa_id,
    )
    assert_fails(lambda: _build_mint_call(user2_address, user2_signer, pay, user2_signer, 2))


def test_mint_delegated_payment():
    # user2 pays, but user1 is the app-call sender — GN-4 blocks this
    pay = transaction.AssetTransferTxn(
        sender=user2_address, sp=algod_client.suggested_params(),
        receiver=nft_app_addr, amt=175_000_000, index=usdc_asa_id,
    )
    assert_fails(lambda: _build_mint_call(user1_address, user1_signer, pay, user2_signer, 2))


run_test("test_mint_while_paused", "mint while paused → reverts", test_mint_while_paused)
run_test("test_mint_wrong_asset", "mint with FRY not USDC → reverts", test_mint_wrong_asset)
run_test("test_mint_underpayment", "mint with 174999999 USDC → reverts", test_mint_underpayment)
run_test("test_mint_overpayment", "mint with 175000001 USDC → reverts (GN-3)", test_mint_overpayment)
run_test("test_mint_delegated_payment", "payment from user2, call from user1 → reverts (GN-4)", test_mint_delegated_payment)
skip_test("test_mint_max_supply", "mint beyond max supply", "would require minting 1000 NFTs")


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 3 — Transfer attacks (4 tests) — user1 owns NFT #1 throughout
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 3 — Transfer attacks")
print("=" * 60)


def _build_transfer(sender_addr, signer, from_addr, to_addr, token_id):
    """Build and execute arc72_transferFrom."""
    tid_bytes = token_id.to_bytes(8, "big")
    from_bytes = encoding.decode_address(from_addr)
    to_bytes = encoding.decode_address(to_addr) if to_addr != ZERO_ADDR else bytes(32)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_transfer_from, sender=sender_addr,
        sp=sp_with_fee(algod_client), signer=signer,
        method_args=[from_addr, to_addr, token_id],
        boxes=[
            (0, b"o" + tid_bytes),
            (0, b"a" + tid_bytes),
            (0, b"b" + from_bytes),
            (0, b"b" + to_bytes),
        ],
    )
    atc.execute(algod_client, 4)


def test_transfer_to_zero():
    # Build directly since assert_fails + special bytes handling
    tid_bytes = (1).to_bytes(8, "big")
    from_bytes = encoding.decode_address(user1_address)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_transfer_from, sender=user1_address,
        sp=sp_with_fee(algod_client), signer=user1_signer,
        method_args=[user1_address, ZERO_ADDR, 1],
        boxes=[
            (0, b"o" + tid_bytes),
            (0, b"a" + tid_bytes),
            (0, b"b" + from_bytes),
            (0, b"b" + bytes(32)),
        ],
    )
    assert_fails(lambda: atc.execute(algod_client, 4))


def test_transfer_not_owner():
    # user2 has no approval, tries to transferFrom user1 to user2
    assert_fails(lambda: _build_transfer(user2_address, user2_signer, user1_address, user2_address, 1))


def test_transfer_spoof_from():
    # user2 claims from_=user2 but doesn't own token 1
    assert_fails(lambda: _build_transfer(user2_address, user2_signer, user2_address, user2_address, 1))


def test_transfer_after_approval_cleared():
    # user1 approves user2, user1 self-transfers (clears approval), user2 tries → fails
    # Step 1: user1 approves user2 for token 1
    tid_bytes = (1).to_bytes(8, "big")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_approve, sender=user1_address,
        sp=sp_with_fee(algod_client), signer=user1_signer,
        method_args=[user2_address, 1],
        boxes=[(0, b"o" + tid_bytes), (0, b"a" + tid_bytes)],
    )
    atc.execute(algod_client, 4)
    # Step 2: user1 self-transfers (clears approval per contract logic)
    _build_transfer(user1_address, user1_signer, user1_address, user1_address, 1)
    # Step 3: user2 now tries to use the cleared approval → must fail
    assert_fails(lambda: _build_transfer(user2_address, user2_signer, user1_address, user2_address, 1))


run_test("test_transfer_to_zero", "user1 transfer to zero address → reverts (GN-1)", test_transfer_to_zero)
run_test("test_transfer_not_owner", "user2 transferFrom user1 w/o auth → reverts", test_transfer_not_owner)
run_test("test_transfer_spoof_from", "user2 spoof from_=user2, owns none → reverts", test_transfer_spoof_from)
run_test("test_transfer_after_approval_cleared", "approval cleared on transfer → reverts", test_transfer_after_approval_cleared)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 4 — Transfer happy paths (3 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 4 — Transfer happy paths")
print("=" * 60)


def test_approve_and_transfer():
    # user1 approves user2
    tid_bytes = (1).to_bytes(8, "big")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_approve, sender=user1_address,
        sp=sp_with_fee(algod_client), signer=user1_signer,
        method_args=[user2_address, 1],
        boxes=[(0, b"o" + tid_bytes), (0, b"a" + tid_bytes)],
    )
    atc.execute(algod_client, 4)
    # user2 transfers user1→user2
    _build_transfer(user2_address, user2_signer, user1_address, user2_address, 1)
    # Verify user2 owns it now
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_owner_of, sender=user2_address,
        sp=sp_with_fee(algod_client), signer=user2_signer,
        method_args=[1], boxes=[(0, b"o" + tid_bytes)],
    )
    owner = atc.execute(algod_client, 4).abi_results[0].return_value
    assert owner == user2_address, f"Expected owner=user2, got {owner}"
    # Transfer back to user1
    _build_transfer(user2_address, user2_signer, user2_address, user1_address, 1)


def test_operator_approve_and_transfer():
    # user1 setApprovalForAll(user2, True)
    op_key = encoding.decode_address(user1_address) + encoding.decode_address(user2_address)
    tid_bytes = (1).to_bytes(8, "big")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_set_approval_for_all, sender=user1_address,
        sp=sp_with_fee(algod_client), signer=user1_signer,
        method_args=[user2_address, True],
        boxes=[(0, op_key)],
    )
    atc.execute(algod_client, 4)
    # user2 transfers user1→user2
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_transfer_from, sender=user2_address,
        sp=sp_with_fee(algod_client), signer=user2_signer,
        method_args=[user1_address, user2_address, 1],
        boxes=[
            (0, b"o" + tid_bytes), (0, b"a" + tid_bytes),
            (0, b"b" + encoding.decode_address(user1_address)),
            (0, b"b" + encoding.decode_address(user2_address)),
            (0, op_key),
        ],
    )
    atc.execute(algod_client, 4)
    # Verify user2 owns it
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_owner_of, sender=user2_address,
        sp=sp_with_fee(algod_client), signer=user2_signer,
        method_args=[1], boxes=[(0, b"o" + tid_bytes)],
    )
    owner = atc.execute(algod_client, 4).abi_results[0].return_value
    assert owner == user2_address, f"Expected owner=user2, got {owner}"
    # user2 transfers back to user1
    _build_transfer(user2_address, user2_signer, user2_address, user1_address, 1)
    # user1 revokes operator approval
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_set_approval_for_all, sender=user1_address,
        sp=sp_with_fee(algod_client), signer=user1_signer,
        method_args=[user2_address, False],
        boxes=[(0, op_key)],
    )
    atc.execute(algod_client, 4)
    # user2 tries to transfer without operator approval → must fail
    assert_fails(lambda: _build_transfer(user2_address, user2_signer, user1_address, user2_address, 1))


def test_balance_of():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_balance_of, sender=user1_address,
        sp=sp_with_fee(algod_client), signer=user1_signer,
        method_args=[user1_address],
        boxes=[(0, b"b" + encoding.decode_address(user1_address))],
    )
    bal = atc.execute(algod_client, 4).abi_results[0].return_value
    assert bal == 1, f"Expected user1 balance=1, got {bal}"


run_test("test_approve_and_transfer", "approve+transfer+transfer-back", test_approve_and_transfer)
run_test("test_operator_approve_and_transfer", "operator approve/transfer/revoke", test_operator_approve_and_transfer)
run_test("test_balance_of", "user1 balanceOf == 1", test_balance_of)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 5 — DistributionPool access control (4 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 5 — DistributionPool access control")
print("=" * 60)


def _dp_call_as_user2(method, args, foreign_assets=None, foreign_apps=None):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=method, sender=user2_address,
        sp=sp_with_fee(algod_client, 1), signer=user2_signer,
        method_args=args,
        foreign_assets=foreign_assets or [],
        foreign_apps=foreign_apps or [],
    )
    atc.execute(algod_client, 4)


run_test("test_nonadmin_start_epoch", "user2 calls start_epoch → reverts",
         lambda: assert_fails(lambda: _dp_call_as_user2(m_start_epoch, [], foreign_assets=[fry_asa_id])))


# For cancel_epoch test: admin first starts epoch, then user2 tries cancel, then admin cancels cleanup
def test_nonadmin_cancel_epoch():
    # Automation starts epoch
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=m_start_epoch, sender=automation_address,
        sp=sp_with_fee(algod_client), signer=automation_signer,
        method_args=[], foreign_assets=[fry_asa_id],
    )
    atc.execute(algod_client, 4)
    try:
        # user2 cancel → fail
        assert_fails(lambda: _dp_call_as_user2(m_cancel_epoch, []))
    finally:
        # admin cancels cleanup
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=dp_app_id, method=m_cancel_epoch, sender=admin_address,
            sp=sp_with_fee(algod_client), signer=admin_signer, method_args=[],
        )
        atc.execute(algod_client, 4)


run_test("test_nonadmin_cancel_epoch", "user2 calls cancel_epoch → reverts", test_nonadmin_cancel_epoch)
run_test("test_nonadmin_set_nft_app_id", "user2 calls admin_set_nft_app_id → reverts",
         lambda: assert_fails(lambda: _dp_call_as_user2(m_admin_set_nft_app_id, [9999])))
run_test("test_nonadmin_withdraw_algo", "user2 calls admin_withdraw_algo → reverts",
         lambda: assert_fails(lambda: _dp_call_as_user2(m_admin_withdraw_algo, [1000, user2_address])))


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 6 — Claim attacks (8 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 6 — Claim attacks")
print("=" * 60)


def _claim_call(sender_addr, signer, token_id, epoch=1):
    """Build and execute claim as the given signer."""
    tid_bytes = token_id.to_bytes(8, "big")
    epoch_bytes = epoch.to_bytes(8, "big")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=m_claim, sender=sender_addr,
        sp=sp_with_fee(algod_client, 2), signer=signer,
        method_args=[token_id],
        foreign_apps=[nft_app_id],
        foreign_assets=[fry_asa_id],
        boxes=[
            (nft_app_id, b"o" + tid_bytes),
            (0, b"c" + epoch_bytes + tid_bytes),
        ],
    )
    atc.execute(algod_client, 4)


def _start_epoch_as_automation():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=m_start_epoch, sender=automation_address,
        sp=sp_with_fee(algod_client), signer=automation_signer,
        method_args=[], foreign_assets=[fry_asa_id],
    )
    return atc.execute(algod_client, 4).abi_results[0].return_value


def _cancel_epoch_as_admin():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=m_cancel_epoch, sender=admin_address,
        sp=sp_with_fee(algod_client), signer=admin_signer, method_args=[],
    )
    atc.execute(algod_client, 4)


# First: test claim when no epoch is active (epoch was just cancelled in Category 5)
run_test("test_claim_no_active_epoch", "claim with no active epoch → reverts",
         lambda: assert_fails(lambda: _claim_call(user1_address, user1_signer, 1, epoch=1)))

# Tests that need active epoch: start a fresh epoch first
epoch_num = _start_epoch_as_automation()  # per_nft = 10000
print(f"  [info] started epoch {epoch_num} for claim tests")

run_test("test_claim_invalid_token_zero", "claim(0) → reverts",
         lambda: assert_fails(lambda: _claim_call(user1_address, user1_signer, 0, epoch=epoch_num)))
run_test("test_claim_invalid_token_over_supply", "claim(1001) → reverts",
         lambda: assert_fails(lambda: _claim_call(user1_address, user1_signer, 1001, epoch=epoch_num)))
run_test("test_claim_not_owner", "user2 claims token 1 (not owner) → reverts",
         lambda: assert_fails(lambda: _claim_call(user2_address, user2_signer, 1, epoch=epoch_num)))


def test_claim_double():
    # First claim by user1 should succeed
    _claim_call(user1_address, user1_signer, 1, epoch=epoch_num)
    # Second claim by user1 should fail
    assert_fails(lambda: _claim_call(user1_address, user1_signer, 1, epoch=epoch_num))


run_test("test_claim_double", "double-claim same token → reverts", test_claim_double)

# start_epoch attacks
run_test("test_start_epoch_while_active", "start_epoch while active → reverts",
         lambda: assert_fails(lambda: _start_epoch_as_automation()))

# Cancel epoch before the next two tests (need inactive state)
_cancel_epoch_as_admin()

def test_start_epoch_zero_balance():
    # Drain all FRY from pool via emergency withdraw
    pool_bal = algod_client.account_asset_info(dp_app_addr, fry_asa_id)["asset-holding"]["amount"]
    if pool_bal > 0:
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=dp_app_id, method=m_emergency_withdraw_fry, sender=admin_address,
            sp=sp_with_fee(algod_client, 1), signer=admin_signer,
            method_args=[pool_bal, admin_address], foreign_assets=[fry_asa_id],
        )
        atc.execute(algod_client, 4)
    # Now start_epoch should fail with zero balance
    assert_fails(lambda: _start_epoch_as_automation())
    # Re-fund pool for subsequent tests
    send_asa(admin_address, admin_private_key, dp_app_addr, 20_000_000, fry_asa_id)

run_test("test_start_epoch_zero_balance", "start_epoch() with zero FRY balance \u2192 reverts",
         test_start_epoch_zero_balance)

def test_admin_cannot_start_epoch():
    # Admin tries to call start_epoch directly - should fail (only automation)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=m_start_epoch, sender=admin_address,
        sp=sp_with_fee(algod_client), signer=admin_signer,
        method_args=[], foreign_assets=[fry_asa_id],
    )
    atc.execute(algod_client, 4)

run_test("test_admin_cannot_start_epoch", "admin calls start_epoch \u2192 reverts (only automation)",
         lambda: assert_fails(test_admin_cannot_start_epoch))


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 7 — FeeRouter access control (3 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 7 — FeeRouter access control")
print("=" * 60)


def _fr_call_as_user2(method, args):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fr_app_id, method=method, sender=user2_address,
        sp=sp_with_fee(algod_client), signer=user2_signer, method_args=args,
    )
    atc.execute(algod_client, 4)


run_test("test_fr_nonadmin_set_pool_bps", "user2 calls admin_set_pool_bps → reverts",
         lambda: assert_fails(lambda: _fr_call_as_user2(m_fr_admin_set_pool_bps, [5000])))
run_test("test_fr_nonadmin_set_treasury", "user2 calls admin_set_treasury → reverts",
         lambda: assert_fails(lambda: _fr_call_as_user2(m_fr_admin_set_treasury, [user2_address])))
run_test("test_fr_nonadmin_set_pool", "user2 calls admin_set_pool → reverts",
         lambda: assert_fails(lambda: _fr_call_as_user2(m_fr_admin_set_pool, [user2_address])))


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 8 — FeeRouter bounds (2 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 8 — FeeRouter bounds")
print("=" * 60)


def _fr_set_pool_bps_as_admin(bps):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fr_app_id, method=m_fr_admin_set_pool_bps, sender=admin_address,
        sp=sp_with_fee(algod_client), signer=admin_signer, method_args=[bps],
    )
    atc.execute(algod_client, 4)


run_test("test_pool_bps_too_high", "admin_set_pool_bps(5001) → reverts",
         lambda: assert_fails(lambda: _fr_set_pool_bps_as_admin(5001)))
run_test("test_pool_bps_zero", "admin_set_pool_bps(0) → reverts",
         lambda: assert_fails(lambda: _fr_set_pool_bps_as_admin(0)))


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 9 — Claim happy path (2 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 9 — Claim happy paths")
print("=" * 60)


def test_claim_correct_amount():
    # Read pool balance to calculate expected per_nft dynamically
    pool_bal = algod_client.account_asset_info(dp_app_addr, fry_asa_id)["asset-holding"]["amount"]
    expected_per_nft = pool_bal // 1000
    ep = _start_epoch_as_automation()
    # Snapshot balance
    before = algod_client.account_asset_info(user1_address, fry_asa_id)["asset-holding"]["amount"]
    _claim_call(user1_address, user1_signer, 1, epoch=ep)
    after = algod_client.account_asset_info(user1_address, fry_asa_id)["asset-holding"]["amount"]
    delta = after - before
    assert delta == expected_per_nft, f"Expected delta={expected_per_nft}, got {delta}"


run_test("test_claim_correct_amount", "claim delivers epoch_per_nft microFRY", test_claim_correct_amount)
skip_test("test_epoch_auto_complete", "auto-complete on full-supply claim",
          "total_supply=1000 but only 1 NFT minted in test environment")


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 10 — supportsInterface (2 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 10 — supportsInterface")
print("=" * 60)


def _call_supports_interface(interface_id_bytes):
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_supports_interface, sender=user1_address,
        sp=sp_with_fee(algod_client), signer=user1_signer,
        method_args=[list(interface_id_bytes)],  # byte[4] as list of 4 ints
    )
    return atc.execute(algod_client, 4).abi_results[0].return_value


def test_supports_interface_arc72():
    result = _call_supports_interface(bytes.fromhex("53f02a40"))
    assert result is True, f"Expected True for 0x53f02a40, got {result}"


def test_supports_interface_unknown():
    result = _call_supports_interface(bytes.fromhex("deadbeef"))
    assert result is False, f"Expected False for 0xdeadbeef, got {result}"


run_test("test_supports_interface_arc72", "supportsInterface(0x53f02a40) → True", test_supports_interface_arc72)
run_test("test_supports_interface_unknown", "supportsInterface(0xdeadbeef) → False", test_supports_interface_unknown)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 11 — Emergency FRY withdrawal (3 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 11 — Emergency FRY withdrawal")
print("=" * 60)

run_test("test_nonadmin_emergency_withdraw_fry", "user2 calls emergency withdraw → reverts",
         lambda: assert_fails(lambda: _dp_call_as_user2(m_emergency_withdraw_fry, [1_000_000, user2_address], foreign_assets=[fry_asa_id])))


def test_admin_emergency_withdraw_fry():
    # Snapshot admin FRY balance before
    before = algod_client.account_asset_info(admin_address, fry_asa_id)["asset-holding"]["amount"]
    # Admin withdraws 1M microFRY from pool
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=m_emergency_withdraw_fry, sender=admin_address,
        sp=sp_with_fee(algod_client, 1), signer=admin_signer,
        method_args=[1_000_000, admin_address],
        foreign_assets=[fry_asa_id],
    )
    atc.execute(algod_client, 4)
    after = algod_client.account_asset_info(admin_address, fry_asa_id)["asset-holding"]["amount"]
    delta = after - before
    assert delta == 1_000_000, f"Expected delta=1000000, got {delta}"


run_test("test_admin_emergency_withdraw_fry", "admin withdraws 1M FRY → succeeds", test_admin_emergency_withdraw_fry)

def test_emergency_withdraw_fry_exceeds_balance():
    def _do():
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=dp_app_id, method=m_emergency_withdraw_fry, sender=admin_address,
            sp=sp_with_fee(algod_client, 1), signer=admin_signer,
            method_args=[999_999_999_999, admin_address],
            foreign_assets=[fry_asa_id],
        )
        atc.execute(algod_client, 4)
    assert_fails(_do)


run_test("test_emergency_withdraw_fry_exceeds_balance", "withdraw >> balance → reverts",
         test_emergency_withdraw_fry_exceeds_balance)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 12 — Admin rotation & new methods (5 tests)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  CATEGORY 12 — Admin rotation & new methods")
print("=" * 60)

m_admin_set_admin = abi.Method.from_signature("admin_set_admin(address)void")
m_admin_withdraw_algo_nft = abi.Method.from_signature("admin_withdraw_algo(uint64,address)void")


def test_nonadmin_set_admin_genesis():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_admin_set_admin, sender=user2_address,
        sp=sp_with_fee(algod_client), signer=user2_signer,
        method_args=[user2_address],
    )
    atc.execute(algod_client, 4)


def test_nonadmin_set_admin_router():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fr_app_id, method=m_admin_set_admin, sender=user2_address,
        sp=sp_with_fee(algod_client), signer=user2_signer,
        method_args=[user2_address],
    )
    atc.execute(algod_client, 4)


def test_nonadmin_set_admin_pool():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dp_app_id, method=m_admin_set_admin, sender=user2_address,
        sp=sp_with_fee(algod_client), signer=user2_signer,
        method_args=[user2_address],
    )
    atc.execute(algod_client, 4)


def test_admin_withdraw_algo_genesis():
    # GenesisNFT was funded with 30 ALGO. Withdraw a small amount to verify it works.
    before = algod_client.account_info(admin_address)["amount"]
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_admin_withdraw_algo_nft, sender=admin_address,
        sp=sp_with_fee(algod_client, 1), signer=admin_signer,
        method_args=[100_000, admin_address],
    )
    atc.execute(algod_client, 4)
    after = algod_client.account_info(admin_address)["amount"]
    # Admin should have gained ~100000 microALGO minus fees
    delta = after - before
    assert delta > 0, f"Expected positive delta, got {delta}"


def test_pause_invalid_value():
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=nft_app_id, method=m_admin_pause, sender=admin_address,
        sp=sp_with_fee(algod_client), signer=admin_signer,
        method_args=[2],
    )
    atc.execute(algod_client, 4)


run_test("test_nonadmin_set_admin_genesis", "user2 calls admin_set_admin on GenesisNFT → reverts",
         lambda: assert_fails(test_nonadmin_set_admin_genesis))
run_test("test_nonadmin_set_admin_router", "user2 calls admin_set_admin on FeeRouter → reverts",
         lambda: assert_fails(test_nonadmin_set_admin_router))
run_test("test_nonadmin_set_admin_pool", "user2 calls admin_set_admin on DistributionPool → reverts",
         lambda: assert_fails(test_nonadmin_set_admin_pool))
run_test("test_admin_withdraw_algo_genesis", "admin withdraws ALGO from GenesisNFT → succeeds",
         test_admin_withdraw_algo_genesis)
run_test("test_pause_invalid_value", "admin calls admin_pause(2) → reverts",
         lambda: assert_fails(test_pause_invalid_value))


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  SECURITY TEST RESULTS")
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
