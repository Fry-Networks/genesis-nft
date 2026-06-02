#!/usr/bin/env python3
"""Deploy FeeRouter on Voi mainnet (AVM v10), fund, opt into vFRY."""

import base64
import datetime
import json
import os
import sys
import time
import traceback

from algosdk import mnemonic, account, transaction, logic
from algosdk.v2client import algod
from algosdk import abi
from algosdk.atomic_transaction_composer import (
    AtomicTransactionComposer,
    AccountTransactionSigner,
)
from algosdk.transaction import StateSchema

# Voi mainnet — public Nodely endpoint (no token)
ALGOD_HOST = 'https://mainnet-api.voi.nodely.dev'
ALGOD_TOKEN = ''

# Voi treasury — pool and treasury both set to this address
TREASURY_ADDR = 'NQA76E235VCMZB4KZQSV6IU64IWF2GGCXK4Y3QA7N7ZMI7MVHUQVV5BUD4'
POOL_BPS = 1000  # 10% to pool (= treasury on Voi)

# vFRY ASA ID on Voi
VFRY_ASA_ID = 48968653

# Funding: 2 VOI for MBR + inner txn buffer
FUND_VOI_MICRO = 2_000_000

# ARC56 build path
ARC56_PATH = '/home/fry/genesis_nft/contracts/build_voi/FeeRouter.arc56.json'
METADATA_PATH = '/home/fry/genesis_nft/contracts/voi_feerouter_deploy_metadata.json'

# .env path for VOI_REWARD_MNEMONIC
ENV_FILE = '/opt/fry-farm/.env'


def log(level, msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f'[{ts}] [{level}] {msg}', flush=True)


def get_env_mnemonic():
    """Read VOI_REWARD_MNEMONIC from .env file."""
    with open(ENV_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('VOI_REWARD_MNEMONIC='):
                val = line.split('=', 1)[1].strip().strip('"').strip("'")
                return val
    raise RuntimeError('VOI_REWARD_MNEMONIC not found in ' + ENV_FILE)


def wait_for_txn(client, txid, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            info = client.pending_transaction_info(txid)
            if info.get('confirmed-round', 0) > 0:
                return info
        except Exception:
            pass
        time.sleep(3)
    raise TimeoutError(f'Transaction {txid} not confirmed within {timeout}s')


def main():
    log('INFO', 'deploy_fee_router_voi starting')

    # Connect to Voi
    client = algod.AlgodClient(ALGOD_TOKEN, ALGOD_HOST)
    status = client.status()
    log('INFO', f'Voi node last round: {status["last-round"]}')

    # Load signer from .env
    mn = get_env_mnemonic()
    sk = mnemonic.to_private_key(mn)
    addr = account.address_from_private_key(sk)
    log('INFO', f'signer wallet: {addr}')

    if addr != TREASURY_ADDR:
        log('WARN', f'signer addr {addr} != TREASURY_ADDR {TREASURY_ADDR}')
        log('WARN', 'continuing — wallet may be rekeyed')

    # Check signer balance
    acct_info = client.account_info(addr)
    balance = acct_info.get('amount', 0)
    log('INFO', f'signer balance: {balance} microVOI ({balance / 1e6:.2f} VOI)')
    if balance < 3_000_000:
        log('ERROR', 'Signer has < 3 VOI. Cannot deploy + fund.')
        sys.exit(1)

    # Load bytecode from ARC56
    log('INFO', f'loading bytecode from {ARC56_PATH}')
    with open(ARC56_PATH, 'r') as f:
        arc56 = json.load(f)
    approval_b64 = ''.join(arc56['byteCode']['approval'])
    clear_b64 = ''.join(arc56['byteCode']['clear'])
    approval_program = base64.b64decode(approval_b64)
    clear_program = base64.b64decode(clear_b64)
    log('INFO', f'approval: {len(approval_program)} bytes, clear: {len(clear_program)} bytes')

    # Build create transaction
    # create(address admin, address treasury, address pool, uint64 pool_bps)void
    sp = client.suggested_params()
    method = abi.Method.from_signature('create(address,address,address,uint64)void')
    signer_obj = AccountTransactionSigner(sk)

    # State schema from ARC56: 3 uints, 3 byte_slices
    global_schema = StateSchema(num_uints=3, num_byte_slices=3)
    local_schema = StateSchema(num_uints=0, num_byte_slices=0)

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=method,
        sender=addr,
        sp=sp,
        signer=signer_obj,
        method_args=[addr, TREASURY_ADDR, TREASURY_ADDR, POOL_BPS],
        approval_program=approval_program,
        clear_program=clear_program,
        global_schema=global_schema,
        local_schema=local_schema,
        extra_pages=1,
    )

    log('INFO', 'submitting create transaction...')
    result = atc.execute(client, 4)
    tx_id = result.tx_ids[0]

    txn_info = wait_for_txn(client, tx_id)
    app_id = txn_info.get('application-index')
    if not app_id:
        log('ERROR', f'No application-index in txn: {txn_info}')
        sys.exit(1)

    app_address = logic.get_application_address(app_id)
    log('INFO', f'DEPLOYED: app_id={app_id}, app_address={app_address}, txid={tx_id}')

    # Fund app with VOI for MBR + inner txn fees
    log('INFO', f'funding app with {FUND_VOI_MICRO} microVOI...')
    fund_sp = client.suggested_params()
    fund_txn = transaction.PaymentTxn(
        sender=addr,
        receiver=app_address,
        amt=FUND_VOI_MICRO,
        sp=fund_sp,
    )
    signed_fund = fund_txn.sign(sk)
    fund_txid = client.send_transaction(signed_fund)
    wait_for_txn(client, fund_txid)
    log('INFO', f'funded VOI: txid={fund_txid}')

    # Opt app into vFRY via admin_opt_in
    log('INFO', f'opting app into vFRY (ASA {VFRY_ASA_ID})...')
    optin_sp = client.suggested_params()
    optin_sp.fee = 2 * optin_sp.min_fee  # cover inner txn fee pooling
    optin_sp.flat_fee = True
    optin_method = abi.Method.from_signature('admin_opt_in(uint64)void')
    atc2 = AtomicTransactionComposer()
    atc2.add_method_call(
        app_id=app_id,
        method=optin_method,
        sender=addr,
        sp=optin_sp,
        signer=signer_obj,
        method_args=[VFRY_ASA_ID],
        foreign_assets=[VFRY_ASA_ID],
    )
    optin_result = atc2.execute(client, 4)
    optin_txid = optin_result.tx_ids[0]
    wait_for_txn(client, optin_txid)
    log('INFO', f'opted in vFRY: txid={optin_txid}')

    # Verify app state
    app_info_resp = client.application_info(app_id)
    gs_items = app_info_resp.get('params', {}).get('global-state', [])
    state = {}
    for item in gs_items:
        key = base64.b64decode(item['key']).decode('utf-8', errors='replace')
        val = item['value']
        if val['type'] == 2:
            state[key] = val['uint']
        elif val['type'] == 1:
            state[key] = base64.b64decode(val['bytes']).hex()
    log('INFO', f'global state: {json.dumps(state, indent=2)}')

    # Verify app balances
    app_acct = client.account_info(app_address)
    app_voi = app_acct.get('amount', 0)
    app_vfry = 0
    for asset in app_acct.get('assets', []):
        if asset['asset-id'] == VFRY_ASA_ID:
            app_vfry = asset.get('amount', 0)
            break
    log('INFO', f'app balance: {app_voi} microVOI, {app_vfry} vFRY')

    # Save metadata
    metadata = {
        'chain': 'voi-mainnet',
        'contract': 'FeeRouter',
        'avm_version': 10,
        'app_id': app_id,
        'app_address': app_address,
        'admin': addr,
        'treasury': TREASURY_ADDR,
        'pool': TREASURY_ADDR,
        'pool_bps': POOL_BPS,
        'vfry_asa_id': VFRY_ASA_ID,
        'deploy_txid': tx_id,
        'fund_txid': fund_txid,
        'optin_txid': optin_txid,
        'deployed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(METADATA_PATH, 'w') as f:
        json.dump(metadata, f, indent=2)
    log('INFO', f'metadata saved to {METADATA_PATH}')

    log('INFO', 'deploy_fee_router_voi complete')
    print(f'\nFEE_ROUTER_APP_ID={app_id}')
    print(f'FEE_ROUTER_ADDR={app_address}')
    print(f'DEPLOY_TXID={tx_id}')
    print(f'FUND_TXID={fund_txid}')
    print(f'OPTIN_TXID={optin_txid}')


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log('ERROR', 'deploy_fee_router_voi FAILED')
        traceback.print_exc()
        sys.exit(1)
