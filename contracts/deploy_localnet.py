"""Deploy and test Genesis NFT system on Algorand localnet.

Deploys GenesisNFT, FeeRouter, and DistributionPool, then runs
8 integration tests: mint, ownership, fees, and distribution.

Usage: python3 deploy_localnet.py
"""

import base64
import json
import os
import sys
import traceback

from algosdk import abi, account, encoding, logic, transaction
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
    TransactionWithSigner,
)
from algosdk.box_reference import BoxReference
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

# ── Globals ───────────────────────────────────────────────────────────────────

ALGOD_URL = "http://100.69.195.100:4001"
KMD_URL = "http://100.69.195.100:4002"
TOKEN = "a" * 64

CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Test results tracker
results = {}


def record(name, passed, detail=""):
    results[name] = (passed, detail)
    mark = "\u2705" if passed else "\u274c"
    print(f"  {mark} {name}" + (f" \u2014 {detail}" if detail else ""))


def sp_with_fee(client, inner_count=0):
    """Get suggested params with fee pooling for inner transactions."""
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = max(sp.min_fee, 1000) * (1 + inner_count)
    return sp


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONNECTION AND ACCOUNT SETUP
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  SECTION 1 \u2014 Connection & Account Setup")
print("=" * 60)

algod_client = algod.AlgodClient(TOKEN, ALGOD_URL)
kmd_client = KMDClient(TOKEN, KMD_URL)

status = algod_client.status()
print(f"  Localnet connected \u2014 round {status['last-round']}")

# Get dev account from KMD
wallets = kmd_client.list_wallets()
default_wallet = next(w for w in wallets if w["name"] == "unencrypted-default-wallet")
wallet_handle = kmd_client.init_wallet_handle(default_wallet["id"], "")
keys = kmd_client.list_keys(wallet_handle)
admin_address = keys[0]
admin_private_key = kmd_client.export_key(wallet_handle, "", admin_address)
admin_signer = AccountTransactionSigner(admin_private_key)

admin_info = algod_client.account_info(admin_address)
print(f"  Admin:  {admin_address}")
print(f"  Balance: {admin_info['amount'] / 1_000_000:.2f} ALGO")

# Create minter account
minter_private_key, minter_address = account.generate_account()
minter_signer = AccountTransactionSigner(minter_private_key)

# Fund minter with 1000 ALGO
sp = algod_client.suggested_params()
fund_txn = transaction.PaymentTxn(admin_address, sp, minter_address, 1_000_000_000)
signed = fund_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
transaction.wait_for_confirmation(algod_client, txid, 4)

minter_info = algod_client.account_info(minter_address)
print(f"  Minter: {minter_address}")
print(f"  Balance: {minter_info['amount'] / 1_000_000:.2f} ALGO")


# ══════��═══════════════════���════════════════════════════════��══════════════════
# SECTION 2 — CREATE TEST TOKENS
# ══��════════════════════���══════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  SECTION 2 \u2014 Create Test Tokens")
print("=" * 60)

# Create test USDC
sp = algod_client.suggested_params()
usdc_txn = transaction.AssetCreateTxn(
    sender=admin_address,
    sp=sp,
    total=10_000_000_000_000,
    decimals=6,
    default_frozen=False,
    unit_name="USDC",
    asset_name="Test USDC",
)
signed = usdc_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
result = transaction.wait_for_confirmation(algod_client, txid, 4)
usdc_asa_id = result["asset-index"]
print(f"  Test USDC created: ASA {usdc_asa_id}")

# Create test FRY
sp = algod_client.suggested_params()
fry_txn = transaction.AssetCreateTxn(
    sender=admin_address,
    sp=sp,
    total=10_000_000_000_000_000,
    decimals=6,
    default_frozen=False,
    unit_name="FRY",
    asset_name="Test FRY",
)
signed = fry_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
result = transaction.wait_for_confirmation(algod_client, txid, 4)
fry_asa_id = result["asset-index"]
print(f"  Test FRY created:  ASA {fry_asa_id}")

# Opt minter into USDC
sp = algod_client.suggested_params()
opt_txn = transaction.AssetTransferTxn(minter_address, sp, minter_address, 0, usdc_asa_id)
signed = opt_txn.sign(minter_private_key)
txid = algod_client.send_transaction(signed)
transaction.wait_for_confirmation(algod_client, txid, 4)

# Send minter 10,000 USDC
sp = algod_client.suggested_params()
send_txn = transaction.AssetTransferTxn(admin_address, sp, minter_address, 10_000_000_000, usdc_asa_id)
signed = send_txn.sign(admin_private_key)
txid = algod_client.send_transaction(signed)
transaction.wait_for_confirmation(algod_client, txid, 4)

minter_usdc = algod_client.account_asset_info(minter_address, usdc_asa_id)
print(f"  Minter USDC balance: {minter_usdc['asset-holding']['amount'] / 1_000_000:.2f}")


# ═════════════════════════��═══════════════════════════════��════════════════════
# SECTION 3 — DEPLOY GENESIS NFT
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  SECTION 3 \u2014 Deploy GenesisNFT")
print("=" * 60)

genesis_nft_app_id = None
genesis_nft_app_addr = None

try:
    # Read and compile TEAL
    with open(os.path.join(CONTRACTS_DIR, "GenesisNFT.approval.teal")) as f:
        approval_teal = f.read()
    with open(os.path.join(CONTRACTS_DIR, "GenesisNFT.clear.teal")) as f:
        clear_teal = f.read()

    approval_compiled = algod_client.compile(approval_teal)
    clear_compiled = algod_client.compile(clear_teal)
    approval_program = base64.b64decode(approval_compiled["result"])
    clear_program = base64.b64decode(clear_compiled["result"])

    print(f"  Compiled: approval={len(approval_program)} bytes, clear={len(clear_program)} bytes")

    # Deploy via ATC
    create_method = abi.Method.from_signature(
        "create(address,address,uint64,uint64,uint64,string,uint64)void"
    )

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=create_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[admin_address, admin_address, 175_000_000, 1000, usdc_asa_id, "ipfs://QmTest/", 500],
        global_schema=transaction.StateSchema(num_uints=6, num_byte_slices=3),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=3,
        approval_program=approval_program,
        clear_program=clear_program,
    )
    atc_result = atc.execute(algod_client, 4)
    genesis_nft_app_id = atc_result.abi_results[0].tx_info["application-index"]
    genesis_nft_app_addr = logic.get_application_address(genesis_nft_app_id)

    print(f"  GenesisNFT deployed: App ID {genesis_nft_app_id}")
    print(f"  App address: {genesis_nft_app_addr}")

    # Fund contract with 30 ALGO
    sp = algod_client.suggested_params()
    fund_txn = transaction.PaymentTxn(admin_address, sp, genesis_nft_app_addr, 30_000_000)
    signed = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)
    print("  Funded with 30 ALGO")

    # Opt contract into USDC
    opt_method = abi.Method.from_signature("admin_opt_in(uint64)void")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=genesis_nft_app_id,
        method=opt_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client, 1),
        signer=admin_signer,
        method_args=[usdc_asa_id],
        foreign_assets=[usdc_asa_id],
    )
    atc.execute(algod_client, 4)
    print("  Opted into USDC")

except Exception:
    traceback.print_exc()
    print("  FAILED to deploy GenesisNFT")


# ══════��════════════════════���═══════════════════════════��══════════════════════
# SECTION 4 — DEPLOY FEE ROUTER
# ═══���═══════��════════════════════════════��═════════════════════════════════════

print("\n" + "=" * 60)
print("  SECTION 4 \u2014 Deploy FeeRouter")
print("=" * 60)

fee_router_app_id = None
fee_router_app_addr = None

try:
    with open(os.path.join(CONTRACTS_DIR, "FeeRouter.approval.teal")) as f:
        approval_teal = f.read()
    with open(os.path.join(CONTRACTS_DIR, "FeeRouter.clear.teal")) as f:
        clear_teal = f.read()

    approval_compiled = algod_client.compile(approval_teal)
    clear_compiled = algod_client.compile(clear_teal)
    approval_program = base64.b64decode(approval_compiled["result"])
    clear_program = base64.b64decode(clear_compiled["result"])

    print(f"  Compiled: approval={len(approval_program)} bytes, clear={len(clear_program)} bytes")

    create_method = abi.Method.from_signature("create(address,address,address,uint64)void")

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=create_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[admin_address, admin_address, admin_address, 1000],
        global_schema=transaction.StateSchema(num_uints=3, num_byte_slices=3),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=3,
        approval_program=approval_program,
        clear_program=clear_program,
    )
    atc_result = atc.execute(algod_client, 4)
    fee_router_app_id = atc_result.abi_results[0].tx_info["application-index"]
    fee_router_app_addr = logic.get_application_address(fee_router_app_id)

    print(f"  FeeRouter deployed: App ID {fee_router_app_id}")
    print(f"  App address: {fee_router_app_addr}")

    # Fund with 5 ALGO
    sp = algod_client.suggested_params()
    fund_txn = transaction.PaymentTxn(admin_address, sp, fee_router_app_addr, 5_000_000)
    signed = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)
    print("  Funded with 5 ALGO")

    # Opt into USDC
    opt_method = abi.Method.from_signature("admin_opt_in(uint64)void")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fee_router_app_id,
        method=opt_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client, 1),
        signer=admin_signer,
        method_args=[usdc_asa_id],
        foreign_assets=[usdc_asa_id],
    )
    atc.execute(algod_client, 4)
    print("  Opted into USDC")

    # Opt into FRY
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fee_router_app_id,
        method=opt_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client, 1),
        signer=admin_signer,
        method_args=[fry_asa_id],
        foreign_assets=[fry_asa_id],
    )
    atc.execute(algod_client, 4)
    print("  Opted into FRY")

except Exception:
    traceback.print_exc()
    print("  FAILED to deploy FeeRouter")


# ═══════════════════════���══════════════════════════════════════════════════════
# SECTION 5 — DEPLOY DISTRIBUTION POOL
# ══════════��══════════════════════════���══════════════════════════���═════════════

print("\n" + "=" * 60)
print("  SECTION 5 \u2014 Deploy DistributionPool")
print("=" * 60)

dist_pool_app_id = None
dist_pool_app_addr = None

try:
    with open(os.path.join(CONTRACTS_DIR, "DistributionPool.approval.teal")) as f:
        approval_teal = f.read()
    with open(os.path.join(CONTRACTS_DIR, "DistributionPool.clear.teal")) as f:
        clear_teal = f.read()

    approval_compiled = algod_client.compile(approval_teal)
    clear_compiled = algod_client.compile(clear_teal)
    approval_program = base64.b64decode(approval_compiled["result"])
    clear_program = base64.b64decode(clear_compiled["result"])

    print(f"  Compiled: approval={len(approval_program)} bytes, clear={len(clear_program)} bytes")

    create_method = abi.Method.from_signature("create(uint64,uint64,uint64)void")

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=create_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[fry_asa_id, genesis_nft_app_id, 1000],
        global_schema=transaction.StateSchema(num_uints=8, num_byte_slices=1),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=3,
        approval_program=approval_program,
        clear_program=clear_program,
    )
    atc_result = atc.execute(algod_client, 4)
    dist_pool_app_id = atc_result.abi_results[0].tx_info["application-index"]
    dist_pool_app_addr = logic.get_application_address(dist_pool_app_id)

    print(f"  DistributionPool deployed: App ID {dist_pool_app_id}")
    print(f"  App address: {dist_pool_app_addr}")

    # Fund with 5 ALGO
    sp = algod_client.suggested_params()
    fund_txn = transaction.PaymentTxn(admin_address, sp, dist_pool_app_addr, 5_000_000)
    signed = fund_txn.sign(admin_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)
    print("  Funded with 5 ALGO")

    # Opt into FRY
    opt_method = abi.Method.from_signature("admin_opt_in(uint64)void")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dist_pool_app_id,
        method=opt_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client, 1),
        signer=admin_signer,
        method_args=[fry_asa_id],
        foreign_assets=[fry_asa_id],
    )
    atc.execute(algod_client, 4)
    print("  Opted into FRY")

    # Update FeeRouter pool address to DistributionPool
    set_pool_method = abi.Method.from_signature("admin_set_pool(address)void")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fee_router_app_id,
        method=set_pool_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[dist_pool_app_addr],
    )
    atc.execute(algod_client, 4)
    print(f"  FeeRouter pool updated to DistributionPool address")

except Exception:
    traceback.print_exc()
    print("  FAILED to deploy DistributionPool")


# ═════════════════════════════════════════════════════════════════��════════════
# SECTION 6 — TEST: MINT NFT
# ═════��══════════════════════��═════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  SECTION 6 \u2014 Test: Mint NFT")
print("=" * 60)

try:
    if not genesis_nft_app_id:
        raise RuntimeError("GenesisNFT not deployed")

    # Box references for mint: owners[1] and balances[minter]
    token_id_bytes = (1).to_bytes(8, "big")
    minter_addr_bytes = encoding.decode_address(minter_address)

    # app_index=0 means "the app being called" (not the literal app ID)
    owners_box = BoxReference(0, b"o" + token_id_bytes)
    balances_box = BoxReference(0, b"b" + minter_addr_bytes)

    # Build mint transaction
    mint_method = abi.Method.from_signature("mint(axfer)uint64")

    sp_mint = sp_with_fee(algod_client, 1)  # 1 inner txn (USDC forward)

    # USDC payment to app
    payment_txn = transaction.AssetTransferTxn(
        sender=minter_address,
        sp=algod_client.suggested_params(),
        receiver=genesis_nft_app_addr,
        amt=175_000_000,
        index=usdc_asa_id,
    )

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=genesis_nft_app_id,
        method=mint_method,
        sender=minter_address,
        sp=sp_mint,
        signer=minter_signer,
        method_args=[TransactionWithSigner(payment_txn, minter_signer)],
        foreign_assets=[usdc_asa_id],
        accounts=[admin_address],  # treasury
        boxes=[owners_box, balances_box],
    )
    atc_result = atc.execute(algod_client, 4)
    minted_token_id = atc_result.abi_results[0].return_value
    print(f"  Minted NFT #{minted_token_id}")

    # Verify ownership
    owner_of_method = abi.Method.from_signature("arc72_ownerOf(uint64)address")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=genesis_nft_app_id,
        method=owner_of_method,
        sender=minter_address,
        sp=sp_with_fee(algod_client),
        signer=minter_signer,
        method_args=[1],
        boxes=[owners_box],
    )
    atc_result = atc.execute(algod_client, 4)
    owner = atc_result.abi_results[0].return_value
    record("Mint NFT #1", owner == minter_address, f"owner={owner[:8]}...")

    # Verify total supply
    supply_method = abi.Method.from_signature("arc72_totalSupply()uint64")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=genesis_nft_app_id,
        method=supply_method,
        sender=minter_address,
        sp=sp_with_fee(algod_client),
        signer=minter_signer,
    )
    atc_result = atc.execute(algod_client, 4)
    total = atc_result.abi_results[0].return_value
    print(f"  Total supply: {total}")

    # Verify balance
    balance_method = abi.Method.from_signature("arc72_balanceOf(address)uint64")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=genesis_nft_app_id,
        method=balance_method,
        sender=minter_address,
        sp=sp_with_fee(algod_client),
        signer=minter_signer,
        method_args=[minter_address],
        boxes=[balances_box],
    )
    atc_result = atc.execute(algod_client, 4)
    bal = atc_result.abi_results[0].return_value
    print(f"  Minter balance: {bal}")

    # Verify token URI
    uri_method = abi.Method.from_signature("arc72_tokenURI(uint64)string")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=genesis_nft_app_id,
        method=uri_method,
        sender=minter_address,
        sp=sp_with_fee(algod_client),
        signer=minter_signer,
        method_args=[1],
        boxes=[owners_box],
    )
    atc_result = atc.execute(algod_client, 4)
    uri = atc_result.abi_results[0].return_value
    record("Token URI", uri == "ipfs://QmTest/1.json", f"uri={uri}")

    # Check treasury received USDC
    admin_usdc = algod_client.account_asset_info(admin_address, usdc_asa_id)
    treasury_usdc = admin_usdc["asset-holding"]["amount"]
    # Admin started with 10T, sent 10K to minter, should have gotten 175 back
    record(
        "Mint revenue to treasury",
        treasury_usdc > 10_000_000_000_000 - 10_000_000_000,
        f"treasury USDC={treasury_usdc / 1_000_000:.2f}",
    )

except Exception as e:
    err_msg = str(e)
    # Extract just the error reason, not the full transaction dump
    if "logic eval error:" in err_msg:
        reason = err_msg[err_msg.index("logic eval error:") :]
        reason = reason.split(".")[0] if "." in reason else reason[:200]
    else:
        reason = err_msg[:300]
    print(f"  ERROR: {reason}")
    traceback.print_exc()
    record("Mint NFT #1", False, "exception")
    record("Token URI", False, "skipped")
    record("Mint revenue to treasury", False, "skipped")


# ═════════════════════════════════════════════���════════════════════════════════
# SECTION 7 — TEST: FEE ROUTING
# ═════════════════════���════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  SECTION 7 \u2014 Test: Fee Routing")
print("=" * 60)

# --- FRY fee routing ---
try:
    if not fee_router_app_id or not dist_pool_app_addr:
        raise RuntimeError("FeeRouter or DistributionPool not deployed")

    route_asa_method = abi.Method.from_signature("route_asa_fee(axfer)void")

    fry_payment = transaction.AssetTransferTxn(
        sender=admin_address,
        sp=algod_client.suggested_params(),
        receiver=fee_router_app_addr,
        amt=100_000_000,  # 100 FRY
        index=fry_asa_id,
    )

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fee_router_app_id,
        method=route_asa_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client, 2),  # up to 2 inner txns
        signer=admin_signer,
        method_args=[TransactionWithSigner(fry_payment, admin_signer)],
        foreign_assets=[fry_asa_id],
        accounts=[admin_address, dist_pool_app_addr],  # treasury + pool
    )
    atc.execute(algod_client, 4)

    # Check pool FRY balance (should be ~10 FRY = 10%)
    pool_fry = algod_client.account_asset_info(dist_pool_app_addr, fry_asa_id)
    pool_fry_bal = pool_fry["asset-holding"]["amount"]
    expected_pool = 10_000_000  # 10% of 100 FRY
    record(
        "Fee route (FRY)",
        pool_fry_bal == expected_pool,
        f"pool={pool_fry_bal / 1_000_000:.2f} FRY (expected {expected_pool / 1_000_000:.2f})",
    )

except Exception:
    traceback.print_exc()
    record("Fee route (FRY)", False, "exception")

# --- ALGO fee routing ---
try:
    if not fee_router_app_id or not dist_pool_app_addr:
        raise RuntimeError("FeeRouter or DistributionPool not deployed")

    route_algo_method = abi.Method.from_signature("route_algo_fee(pay)void")

    algo_payment = transaction.PaymentTxn(
        sender=admin_address,
        sp=algod_client.suggested_params(),
        receiver=fee_router_app_addr,
        amt=10_000_000,  # 10 ALGO
    )

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=fee_router_app_id,
        method=route_algo_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client, 2),
        signer=admin_signer,
        method_args=[TransactionWithSigner(algo_payment, admin_signer)],
        accounts=[admin_address, dist_pool_app_addr],
    )
    atc.execute(algod_client, 4)

    # Check pool ALGO balance increased
    pool_info = algod_client.account_info(dist_pool_app_addr)
    pool_algo = pool_info["amount"]
    # Pool was funded with 5 ALGO, should now have ~6 ALGO (5 + 1 from 10% of 10)
    record(
        "Fee route (ALGO)",
        pool_algo > 5_500_000,
        f"pool ALGO={pool_algo / 1_000_000:.2f}",
    )

except Exception:
    traceback.print_exc()
    record("Fee route (ALGO)", False, "exception")


# ══════════════════════════���═══════════════════════════════════════════════════
# SECTION 8 — TEST: DISTRIBUTION
# ════���══════════════════════════════════════════════════════���══════════════════

print("\n" + "=" * 60)
print("  SECTION 8 \u2014 Test: Distribution")
print("=" * 60)

try:
    if not dist_pool_app_id:
        raise RuntimeError("DistributionPool not deployed")

    # Start epoch: 10M total over 1000-supply = 10000 per NFT
    start_method = abi.Method.from_signature("start_epoch(uint64)uint64")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dist_pool_app_id,
        method=start_method,
        sender=admin_address,
        sp=sp_with_fee(algod_client),
        signer=admin_signer,
        method_args=[10_000_000],
        foreign_assets=[fry_asa_id],
    )
    atc_result = atc.execute(algod_client, 4)
    epoch = atc_result.abi_results[0].return_value
    record("Start epoch", epoch == 1, f"epoch={epoch}")

    # Minter opts into FRY
    sp = algod_client.suggested_params()
    opt_txn = transaction.AssetTransferTxn(minter_address, sp, minter_address, 0, fry_asa_id)
    signed = opt_txn.sign(minter_private_key)
    txid = algod_client.send_transaction(signed)
    transaction.wait_for_confirmation(algod_client, txid, 4)
    print("  Minter opted into FRY")

    # Snapshot minter FRY balance before claim
    minter_fry_before = algod_client.account_asset_info(minter_address, fry_asa_id)["asset-holding"]["amount"]

    # Holder claim: minter owns NFT #1, pulls their share (10M // 1000 = 10000 microFRY)
    token_id_bytes = (1).to_bytes(8, "big")
    epoch_bytes = (1).to_bytes(8, "big")  # current_epoch is 1 after start_epoch
    claim_method = abi.Method.from_signature("claim(uint64)uint64")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=dist_pool_app_id,
        method=claim_method,
        sender=minter_address,  # holder pulls, not admin pushes
        sp=sp_with_fee(algod_client, 2),  # 2 inner txns: abi_call + FRY transfer
        signer=minter_signer,
        method_args=[1],
        foreign_apps=[genesis_nft_app_id],
        foreign_assets=[fry_asa_id],
        boxes=[
            (genesis_nft_app_id, b"o" + token_id_bytes),  # GenesisNFT owners box
            (0, b"c" + epoch_bytes + token_id_bytes),     # DistributionPool claim box
        ],
    )
    atc_result = atc.execute(algod_client, 4)
    amount_claimed = atc_result.abi_results[0].return_value
    record("Holder claim", amount_claimed == 10_000, f"received={amount_claimed} microFRY")

    # Verify minter FRY balance delta
    minter_fry_after = algod_client.account_asset_info(minter_address, fry_asa_id)["asset-holding"]["amount"]
    minter_delta = minter_fry_after - minter_fry_before
    print(f"  Minter FRY delta: {minter_delta} microFRY (expected 10000)")

    # Double-claim must fail
    try:
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=dist_pool_app_id,
            method=claim_method,
            sender=minter_address,
            sp=sp_with_fee(algod_client, 2),
            signer=minter_signer,
            method_args=[1],
            foreign_apps=[genesis_nft_app_id],
            foreign_assets=[fry_asa_id],
            boxes=[
                (genesis_nft_app_id, b"o" + token_id_bytes),
                (0, b"c" + epoch_bytes + token_id_bytes),
            ],
        )
        atc.execute(algod_client, 4)
        record("Double claim blocked", False, "second claim succeeded (should have failed)")
    except Exception as e2:
        err_str = str(e2)
        if "logic eval error" in err_str or "assert failed" in err_str:
            record("Double claim blocked", True, "reverted as expected")
        else:
            record("Double claim blocked", False, f"wrong error: {err_str[:200]}")

except Exception as e:
    err_msg = str(e)
    if "logic eval error:" in err_msg:
        reason = err_msg[err_msg.index("logic eval error:") :]
        reason = reason.split(".")[0] if "." in reason else reason[:200]
    else:
        reason = err_msg[:300]
    print(f"  ERROR: {reason}")
    traceback.print_exc()
    if "Start epoch" not in results:
        record("Start epoch", False, "exception")
    if "Holder claim" not in results:
        record("Holder claim", False, "exception")
    if "Double claim blocked" not in results:
        record("Double claim blocked", False, "exception")


# ═══════════════════════��═══════════════════════════════════��══════════════════
# SECTION 9 — FINAL SUMMARY
# ═══════════════════════════���══════════════════════════════════════════════════

print("\n")
print("=" * 60)
print("  LOCALNET INTEGRATION TEST \u2014 RESULTS")
print("=" * 60)

print("\n  Contracts Deployed:")
if genesis_nft_app_id:
    print(f"    GenesisNFT:       App ID {genesis_nft_app_id}, Address {genesis_nft_app_addr}")
if fee_router_app_id:
    print(f"    FeeRouter:        App ID {fee_router_app_id}, Address {fee_router_app_addr}")
if dist_pool_app_id:
    print(f"    DistributionPool: App ID {dist_pool_app_id}, Address {dist_pool_app_addr}")

print(f"\n  Test Tokens:")
print(f"    Test USDC: ASA {usdc_asa_id}")
print(f"    Test FRY:  ASA {fry_asa_id}")

print(f"\n  Test Results:")
test_order = [
    "Mint NFT #1",
    "Token URI",
    "Mint revenue to treasury",
    "Fee route (FRY)",
    "Fee route (ALGO)",
    "Start epoch",
    "Holder claim",
    "Double claim blocked",
]

passed = 0
total = len(test_order)
for name in test_order:
    if name in results:
        ok, detail = results[name]
        mark = "\u2705" if ok else "\u274c"
        if ok:
            passed += 1
        print(f"    {mark} {name:<35s} \u2014 {detail}")
    else:
        print(f"    \u274c {name:<35s} \u2014 not run")

print(f"\n  Overall: {passed}/{total} tests passed")
print("=" * 60)

sys.exit(0 if passed == total else 1)
