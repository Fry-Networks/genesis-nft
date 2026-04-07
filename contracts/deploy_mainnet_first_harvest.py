"""Deploy EventVesting for The First Harvest on Algorand mainnet.

Reads secrets from 1Password. Does NOT touch MongoDB.
Default mode: DRY_RUN (simulates but does not submit).
Pass --execute to submit real transactions.
Pass --verify APP_ID to verify an existing deployment.

Usage:
  python3 deploy_mainnet_first_harvest.py              # dry-run (default)
  python3 deploy_mainnet_first_harvest.py --execute     # LIVE mainnet deploy
  python3 deploy_mainnet_first_harvest.py --verify 1234 # verify existing app
"""

import argparse
import base64
import os
import subprocess
import sys

from algosdk import abi, account, encoding, logic, transaction
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
)
from algosdk.v2client import algod

# ── Constants ────────────────────────────────────────────────────────────────

CONTRACTS_DIR = os.path.dirname(os.path.abspath(__file__))

# First Harvest parameters
FRY_ASA_ID = 2485314946
VESTING_START = 1743206400   # 2026-03-29T00:00:00Z (event end date)
VESTING_DURATION = 31536000  # 365 days
CLIFF_END = 0                # no cliff, linear from start
TOTAL_POOL = 35_000_000_000_000  # 35M FRY in microunits (6 decimals)

ATLAS00_ALGOD_URL = "http://100.69.195.100:4190"

# 8 qualified participants — exact fractional points from MongoDB
# Points are multiplied by 1e6 and stored as integers to avoid floating point
PARTICIPANTS = [
    {"wallet": "J6P3YDW72L4TWEGNHSLWTIQTQYH3L7CHI2PHTLHRCP7ARSMKN4UN6FJYRI", "points_micro": 1000_000000},
    {"wallet": "FMVQGM4DCVL2DHXET2RT5AAUPY2TZZV7RCF3V4BEBWGYIAYBU4CEP67G24", "points_micro":  112_393172},
    {"wallet": "XETLSDPESZHWVAVWWQLZPS5T65UBBIRVGRN4SMRNWSYBFH5BPPLGUWPF4Q", "points_micro":  101_207953},
    {"wallet": "ZS5OGNCKLPSOTMN36OONCQ6XOVBP26PHDVWGLLO2R5ADBFE2DMA3HFVZLU", "points_micro":   86_817718},
    {"wallet": "3KSLWW7KIUGIWOVGPIIPIXZUUGTFQ63XST6BIRYDTIIH6B7OOPHDEH764Q", "points_micro":   48_341539},
    {"wallet": "FGCOER2A6CWC3XLQ3AWNBAB6HZF2SHYS26YCAG6EH56I62DM6RCU7BGJAQ", "points_micro":   42_110372},
    {"wallet": "54UXMVIM5722JP5UONDCI2NDRZLSSVNPJOZGZVIVGXEPLBBFKZIJDG4GX4", "points_micro":   35_000000},
    {"wallet": "K6YAELU4EETZFETOWWMGJJALOTRD2KQ4EHMDSCKMEF5AIKFU4VCMXY3BGQ", "points_micro":   34_995720},
]

TOTAL_POINTS_MICRO = sum(p["points_micro"] for p in PARTICIPANTS)  # 1460_866474


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def op_read(ref):
    """Read a secret from 1Password CLI."""
    result = subprocess.run(
        ["op", "read", ref],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "")},
    )
    if result.returncode != 0:
        print(f"  [ERROR] Failed to read {ref}: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


def confirm(msg):
    """Prompt for confirmation. Returns True if confirmed."""
    resp = input(f"\n  >>> {msg} [y/N] ")
    return resp.strip().lower() == "y"


# ── Allocation Calculation (integer math only) ──────────────────────────────

def compute_allocations():
    """Compute proportional allocations using integer-only math.

    allocation_i = (points_micro_i * TOTAL_POOL) // TOTAL_POINTS_MICRO

    This avoids floating point entirely. The sum may be slightly less than
    TOTAL_POOL due to integer truncation — the difference is dust that
    remains in the contract.
    """
    allocations = []
    for p in PARTICIPANTS:
        alloc = (p["points_micro"] * TOTAL_POOL) // TOTAL_POINTS_MICRO
        allocations.append({**p, "allocation": alloc})

    total_alloc = sum(a["allocation"] for a in allocations)
    dust = TOTAL_POOL - total_alloc
    return allocations, total_alloc, dust


# ══════════════════════════════════════════════════════════════════════════════
# VERIFY MODE — reads an existing deployment
# ══════════════════════════════════════════════════════════════════════════════

def run_verify(app_id):
    print("=" * 60)
    print(f"  VERIFY MODE — App ID {app_id}")
    print("=" * 60)

    # Load token
    token = op_read("op://FryFarm/ATLAS00 Algod Token/ALGOD_TOKEN")
    client = algod.AlgodClient(token, ATLAS00_ALGOD_URL)
    status = client.status()
    print(f"  Mainnet round: {status['last-round']}")

    app_addr = logic.get_application_address(app_id)
    print(f"  Contract address: {app_addr}")

    # Check contract FRY balance
    try:
        asa_info = client.account_asset_info(app_addr, FRY_ASA_ID)
        balance = asa_info["asset-holding"]["amount"]
        print(f"  Contract FRY balance: {balance:,} ({balance / 1e6:,.2f} FRY)")
    except Exception as e:
        print(f"  [WARN] Could not read contract FRY balance: {e}")

    # Read vesting status
    print(f"\n  --- Vesting Status ---")
    atc = AtomicTransactionComposer()
    # Use admin_address for the simulate call — anyone can read
    # We need a signer, so derive from REWARD_MNEMONIC
    mnemonic = op_read("op://FryFarm/Backend Secrets/REWARD_MNEMONIC")
    og_account = account.address_from_private_key(account.mnemonic.to_private_key(mnemonic))
    pk = account.mnemonic.to_private_key(mnemonic)
    signer = AccountTransactionSigner(pk)

    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("get_vesting_status()(uint64,uint64,uint64,uint64,uint64,uint64)"),
        sender=og_account,
        sp=sp_with_fee(client),
        signer=signer,
        method_args=[],
    )
    result = atc.execute(client, 4)
    total_alloc, total_claimed, count, v_start, v_duration, v_cliff = result.abi_results[0].return_value

    print(f"  total_allocated:   {total_alloc:,} ({total_alloc / 1e6:,.2f} FRY)")
    print(f"  total_claimed:     {total_claimed:,} ({total_claimed / 1e6:,.2f} FRY)")
    print(f"  allocations_count: {count}")
    print(f"  vesting_start:     {v_start}")
    print(f"  vesting_duration:  {v_duration} ({v_duration // 86400} days)")
    print(f"  cliff_end:         {v_cliff}")

    # Read each participant allocation
    allocations, expected_total, dust = compute_allocations()

    print(f"\n  --- Per-Participant Allocations ---")
    print(f"  {'#':<4} {'Points':<14} {'Expected':>24} {'On-Chain':>24} {'Claimed':>18} {'Match':<6}")
    print(f"  {'—'*4} {'—'*14} {'—'*24} {'—'*24} {'—'*18} {'—'*6}")

    all_match = True
    for i, a in enumerate(allocations):
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=app_id,
            method=abi.Method.from_signature("get_allocation(address)(uint64,uint64,uint64)"),
            sender=og_account,
            sp=sp_with_fee(client),
            signer=signer,
            method_args=[a["wallet"]],
            boxes=boxes_for_wallet(a["wallet"]),
        )
        result = atc.execute(client, 4)
        alloc_val, claimed_val, claimable_val = result.abi_results[0].return_value

        match = "OK" if alloc_val == a["allocation"] else "MISMATCH"
        if match != "OK":
            all_match = False
        pts_str = f"{a['points_micro'] / 1e6:.6f}"
        print(f"  {i+1:<4} {pts_str:<14} {a['allocation']:>24,} {alloc_val:>24,} {claimed_val:>18,} {match:<6}")

    print(f"\n  Overall: {'ALL MATCH' if all_match else 'MISMATCHES FOUND'}")
    return all_match


# ══════════════════════════════════════════════════════════════════════════════
# DEPLOY MODE — deploys and sets up the contract
# ══════════════════════════════════════════════════════════════════════════════

def run_deploy(execute):
    mode = "EXECUTE" if execute else "DRY-RUN"
    print("=" * 60)
    print(f"  FIRST HARVEST MAINNET DEPLOYMENT — {mode}")
    print("=" * 60)

    if not execute:
        print("  (DRY-RUN: transactions will be built but NOT submitted)")
        print("  (Pass --execute to submit real transactions)")

    # ── Load secrets ──────────────────────────────────────────────────────

    print("\n  Loading secrets from 1Password...")
    reward_mnemonic = op_read("op://FryFarm/Backend Secrets/REWARD_MNEMONIC")
    reward_rekey = op_read("op://FryFarm/Backend Secrets/REWARD_REKEY")
    algod_token = op_read("op://FryFarm/ATLAS00 Algod Token/ALGOD_TOKEN")

    # Derive address from REWARD_MNEMONIC, signing key from REWARD_REKEY
    og_pk = account.mnemonic.to_private_key(reward_mnemonic)
    treasury_addr = account.address_from_private_key(og_pk)
    rekey_pk = account.mnemonic.to_private_key(reward_rekey)
    rekey_signer = AccountTransactionSigner(rekey_pk)

    print(f"  Treasury address: {treasury_addr}")
    print(f"  Signing with:     REWARD_REKEY-derived key")

    # ── Connect to mainnet ────────────────────────────────────────────────

    client = algod.AlgodClient(algod_token, ATLAS00_ALGOD_URL)
    status = client.status()
    print(f"  Mainnet round:    {status['last-round']}")

    # Verify treasury balances
    acct_info = client.account_info(treasury_addr)
    algo_balance = acct_info["amount"]
    fry_balance = 0
    for asset in acct_info.get("assets", []):
        if asset["asset-id"] == FRY_ASA_ID:
            fry_balance = asset["amount"]
            break

    print(f"  ALGO balance:     {algo_balance:,} ({algo_balance / 1e6:,.2f} ALGO)")
    print(f"  FRY balance:      {fry_balance:,} ({fry_balance / 1e6:,.2f} FRY)")

    if fry_balance < TOTAL_POOL:
        print(f"  [FATAL] Insufficient FRY: have {fry_balance:,}, need {TOTAL_POOL:,}")
        sys.exit(1)
    if algo_balance < 3_000_000:
        print(f"  [FATAL] Insufficient ALGO: have {algo_balance:,}, need >= 3,000,000")
        sys.exit(1)

    # ── Compute allocations ───────────────────────────────────────────────

    allocations, total_alloc, dust = compute_allocations()

    print(f"\n  --- Allocation Table ---")
    print(f"  {'#':<4} {'Points':<14} {'Allocation (micro)':>24} {'FRY':>16}")
    print(f"  {'—'*4} {'—'*14} {'—'*24} {'—'*16}")
    for i, a in enumerate(allocations):
        pts_str = f"{a['points_micro'] / 1e6:.6f}"
        print(f"  {i+1:<4} {pts_str:<14} {a['allocation']:>24,} {a['allocation'] / 1e6:>14,.6f}")
    print(f"  {'':4} {'TOTAL':<14} {total_alloc:>24,} {total_alloc / 1e6:>14,.6f}")
    print(f"  {'':4} {'DUST':<14} {dust:>24,} {dust / 1e6:>14,.6f}")

    assert total_alloc + dust == TOTAL_POOL, f"Allocation + dust ({total_alloc + dust}) != pool ({TOTAL_POOL})"

    if not execute:
        print("\n  [DRY-RUN] Would deploy EventVesting contract")
        print(f"  [DRY-RUN] Would fund with 2 ALGO + {TOTAL_POOL:,} microFRY")
        print(f"  [DRY-RUN] Would register {len(allocations)} allocations")
        print(f"  [DRY-RUN] Would finalize")
        print("\n  To execute for real: python3 deploy_mainnet_first_harvest.py --execute")
        return

    # ── STEP 1: Deploy contract ───────────────────────────────────────────

    print("\n  STEP 1: Deploy EventVesting contract")
    if not confirm("Deploy EventVesting to Algorand MAINNET?"):
        print("  Aborted.")
        sys.exit(0)

    with open(os.path.join(CONTRACTS_DIR, "EventVesting.approval.teal")) as f:
        approval_teal = f.read()
    with open(os.path.join(CONTRACTS_DIR, "EventVesting.clear.teal")) as f:
        clear_teal = f.read()
    approval_compiled = client.compile(approval_teal)
    clear_compiled = client.compile(clear_teal)
    approval_prog = base64.b64decode(approval_compiled["result"])
    clear_prog = base64.b64decode(clear_compiled["result"])

    create_method = abi.Method.from_signature("create(uint64,uint64,uint64,uint64)void")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=create_method,
        sender=treasury_addr,
        sp=sp_with_fee(client),
        signer=rekey_signer,
        method_args=[FRY_ASA_ID, VESTING_START, VESTING_DURATION, CLIFF_END],
        global_schema=transaction.StateSchema(num_uints=9, num_byte_slices=1),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=3,
        approval_program=approval_prog,
        clear_program=clear_prog,
    )
    result = atc.execute(client, 4)
    app_id = result.abi_results[0].tx_info["application-index"]
    app_addr = logic.get_application_address(app_id)
    tx_id = result.tx_ids[0]
    print(f"  [OK] Deployed: app_id={app_id}")
    print(f"  [OK] Address:  {app_addr}")
    print(f"  [OK] Tx:       {tx_id}")

    # ── STEP 2: Fund with ALGO ────────────────────────────────────────────

    print("\n  STEP 2: Fund contract with 2 ALGO")
    sp = client.suggested_params()
    fund_txn = transaction.PaymentTxn(treasury_addr, sp, app_addr, 2_000_000)
    signed_fund = fund_txn.sign(rekey_pk)
    txid = client.send_transaction(signed_fund)
    transaction.wait_for_confirmation(client, txid, 4)
    print(f"  [OK] Funded 2 ALGO — tx: {txid}")

    # ── STEP 3: Opt contract into FRY ASA ─────────────────────────────────

    print("\n  STEP 3: Opt contract into FRY ASA")
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("admin_opt_in(uint64)void"),
        sender=treasury_addr,
        sp=sp_with_fee(client, 1),
        signer=rekey_signer,
        method_args=[FRY_ASA_ID],
        foreign_assets=[FRY_ASA_ID],
    )
    result = atc.execute(client, 4)
    print(f"  [OK] Opted into FRY — tx: {result.tx_ids[0]}")

    # ── STEP 4: Transfer 35M FRY ──────────────────────────────────────────

    print(f"\n  STEP 4: Transfer {TOTAL_POOL:,} microFRY to contract")
    if not confirm(f"Transfer {TOTAL_POOL / 1e6:,.0f} FRY from treasury to contract?"):
        print(f"  Aborted. Contract deployed but NOT funded.")
        print(f"  App ID: {app_id} — you can fund manually or re-run.")
        sys.exit(0)

    sp = client.suggested_params()
    asa_txn = transaction.AssetTransferTxn(treasury_addr, sp, app_addr, TOTAL_POOL, FRY_ASA_ID)
    signed_asa = asa_txn.sign(rekey_pk)
    txid = client.send_transaction(signed_asa)
    transaction.wait_for_confirmation(client, txid, 4)
    print(f"  [OK] Transferred {TOTAL_POOL:,} microFRY — tx: {txid}")

    # Verify
    asa_info = client.account_asset_info(app_addr, FRY_ASA_ID)
    contract_balance = asa_info["asset-holding"]["amount"]
    assert contract_balance == TOTAL_POOL, f"Balance mismatch: {contract_balance} != {TOTAL_POOL}"
    print(f"  [OK] Contract FRY balance verified: {contract_balance:,}")

    # ── STEP 5: Register 8 allocations ────────────────────────────────────

    print(f"\n  STEP 5: Register {len(allocations)} allocations")
    if not confirm(f"Register allocations for {len(allocations)} participants?"):
        print(f"  Aborted. Contract funded but allocations NOT registered.")
        print(f"  App ID: {app_id}")
        sys.exit(0)

    register_method = abi.Method.from_signature("register_allocation(address,uint64)void")
    for i, a in enumerate(allocations):
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=app_id,
            method=register_method,
            sender=treasury_addr,
            sp=sp_with_fee(client),
            signer=rekey_signer,
            method_args=[a["wallet"], a["allocation"]],
            boxes=boxes_for_wallet(a["wallet"]),
        )
        result = atc.execute(client, 4)
        pts_str = f"{a['points_micro'] / 1e6:.6f}"
        print(f"  [OK] #{i+1}: {a['wallet'][:16]}... → {a['allocation']:,} ({pts_str} pts) — tx: {result.tx_ids[0]}")

    # ── STEP 6: Finalize ──────────────────────────────────────────────────

    print(f"\n  STEP 6: Finalize (lock allocations, enable claiming)")
    if not confirm("Finalize? This locks allocations permanently."):
        print(f"  Aborted. Allocations registered but NOT finalized.")
        print(f"  App ID: {app_id}")
        sys.exit(0)

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=abi.Method.from_signature("finalize()void"),
        sender=treasury_addr,
        sp=sp_with_fee(client),
        signer=rekey_signer,
        method_args=[],
        foreign_assets=[FRY_ASA_ID],
    )
    result = atc.execute(client, 4)
    print(f"  [OK] Finalized — tx: {result.tx_ids[0]}")

    # ── STEP 7: Verify ───────────────────────────────────────────────────

    print(f"\n  STEP 7: Post-deployment verification")
    success = run_verify(app_id)

    # ── Summary ───────────────────────────────────────────────────────────

    print("\n" + "=" * 60)
    print("  DEPLOYMENT COMPLETE")
    print("=" * 60)
    print(f"  App ID:            {app_id}")
    print(f"  Contract Address:  {app_addr}")
    print(f"  FRY ASA:           {FRY_ASA_ID}")
    print(f"  Vesting Start:     2026-03-29T00:00:00Z ({VESTING_START})")
    print(f"  Vesting Duration:  365 days ({VESTING_DURATION}s)")
    print(f"  Cliff:             None")
    print(f"  Total Allocated:   {total_alloc:,} microFRY ({total_alloc / 1e6:,.2f} FRY)")
    print(f"  Dust:              {dust:,} microFRY")
    print(f"  Participants:      {len(allocations)}")
    print(f"  Status:            {'VERIFIED' if success else 'VERIFICATION FAILED'}")
    print("=" * 60)

    if not success:
        print("\n  [WARN] Verification found mismatches. Review manually.")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy EventVesting for The First Harvest")
    parser.add_argument("--execute", action="store_true",
                        help="Submit real mainnet transactions (default: dry-run)")
    parser.add_argument("--verify", type=int, metavar="APP_ID",
                        help="Verify an existing deployment by app ID")
    args = parser.parse_args()

    # Ensure OP_SERVICE_ACCOUNT_TOKEN is set
    if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        token_path = "/etc/opt/fryfarm/op-sa-token"
        if os.path.exists(token_path):
            try:
                with open(token_path) as f:
                    os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = f.read().strip()
            except PermissionError:
                print(f"  [ERROR] Cannot read {token_path} — run with sudo or set OP_SERVICE_ACCOUNT_TOKEN")
                sys.exit(1)
        else:
            print("  [ERROR] OP_SERVICE_ACCOUNT_TOKEN not set and token file not found")
            sys.exit(1)

    if args.verify is not None:
        success = run_verify(args.verify)
        sys.exit(0 if success else 1)
    else:
        run_deploy(execute=args.execute)
