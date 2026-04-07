# Genesis NFT Mainnet Testing — Recovery Runbook

## Overview

During the initial mainnet testing phase, the mint page will be at an unlisted URL
(not linked from fry.farm navigation). Only the admin wallet and invited testers
will mint. This document covers recovery procedures if bugs are found.

## Architecture Summary

- **GenesisNFT**: ARC-72 NFTs, ownership tracked in contract BoxMap state
- **FeeRouter**: Splits platform fees 90% treasury / 10% distribution pool
- **DistributionPool**: Holds FRY for holder-pull claiming per epoch

Key facts:
- USDC mint payments go DIRECTLY to treasury via inner transaction — the GenesisNFT contract never holds USDC
- NFT ownership is app-level state (BoxMap), not ASAs
- All 3 contracts are upgradeable by admin
- Admin wallet: E2F2LT2INE75DBOYHQXTCTOP2PAP5MHAXQRXTTCCXFKHQTVG36DJONBQZE

## Scenario 1: Minor Bug Found — Fix In Place

If a non-critical bug is found that can be patched:

1. Call `admin_pause(1)` on GenesisNFT — halts all minting immediately
2. Fix the contract code locally on HEPHAESTUS00
3. Recompile with PuyaPy
4. Test the fix on localnet (deploy_localnet.py + test_security.py)
5. Push the update to mainnet via admin `update()` call
6. Verify the fix on mainnet
7. Call `admin_pause(0)` to resume minting

Minted NFTs and their ownership are preserved across updates.
No user funds are at risk — USDC already went to treasury.

## Scenario 2: Critical Bug — Abandon and Redeploy

If the contract is fundamentally broken and needs redeployment:

### Step 1: Halt everything
- Call `admin_pause(1)` on GenesisNFT
- Call `cancel_epoch()` on DistributionPool (if epoch is active)

### Step 2: Recover funds
- GenesisNFT: Call `admin_withdraw()` to recover any assets held by the contract
  (USDC goes to treasury on mint, so contract typically holds nothing)
- DistributionPool: Call `admin_emergency_withdraw_fry()` to recover all FRY
- DistributionPool: Call `admin_withdraw_algo()` to recover ALGO (MBR funding)
- FeeRouter: Call `flush_asa()` and `flush_algo()` to move any stuck fees

### Step 3: Record who minted
Before abandoning the contract, query the on-chain state to identify:
- All minted token IDs and their current owners
- Amounts paid (175 USDC x number of mints)
This data is needed for manual refunds from the treasury wallet.

### Step 4: Refund testers
Send USDC refunds from the treasury wallet to each tester who minted.
Amount: 175 USDC per NFT minted.

### Step 5: Deploy fresh contracts
- Fix the bug in contract code
- Test on localnet
- Deploy new GenesisNFT, FeeRouter, DistributionPool contracts
- Call `admin_set_base_uri("https://fry.farm/genesis/metadata/")` on new GenesisNFT
- Wire up cross-references between the 3 new contracts
- Fund DistributionPool with ALGO for box MBR
- Opt contracts into USDC and FRY

The old contracts remain on-chain but are effectively dead.
NFT token IDs 1-1000 are available fresh in the new contract.
The same images and metadata at fry.farm/genesis/ work unchanged.

### Step 6: Resume testing
Update the mint page to point to the new GenesisNFT app ID.
Invite testers again.

## Scenario 3: DistributionPool Claim Bug — FRY Stuck

If the claim() logic has a bug and holders cannot claim FRY:

1. Call `cancel_epoch()` to stop the current epoch
2. Call `admin_emergency_withdraw_fry()` to pull all FRY out
3. Fix the claim logic, recompile, test on localnet
4. Push update via `update()` call
5. Re-fund the pool with FRY
6. Start a new epoch with `start_epoch()`

## Contract Kill Switches

| Contract | Method | Effect |
|----------|--------|--------|
| GenesisNFT | `admin_pause(1)` | Halts all minting |
| GenesisNFT | `admin_withdraw(asset, amt, rcvr)` | Drain any asset |
| DistributionPool | `cancel_epoch()` | Stop active distribution |
| DistributionPool | `admin_emergency_withdraw_fry(amt, rcvr)` | Emergency FRY recovery |
| DistributionPool | `admin_withdraw_algo(amt, rcvr)` | Recover ALGO/MBR |
| FeeRouter | `flush_asa(asset)` / `flush_algo()` | Move stuck fees |
| All 3 | `update()` | Push code fix (admin only) |
| All 3 | `admin_set_admin(new_admin)` | Rotate admin key |
| GenesisNFT | `admin_withdraw_algo(amt, rcvr)` | Recover ALGO from contract |

## Mainnet Testing Checklist

Before making the mint page public:

- [ ] Admin mints NFT #1 — verify ownership, metadata URL, image loads
- [ ] Admin mints NFT #2 — verify sequential token ID
- [ ] Verify treasury received 175 USDC per mint
- [ ] Start a test epoch on DistributionPool with small FRY amount
- [ ] Admin claims for owned NFT — verify FRY received
- [ ] Verify double-claim is blocked
- [ ] Test admin_pause(1) / admin_pause(0) cycle (single method, input must be 0 or 1)
- [ ] Test admin_emergency_withdraw_fry (deposit then withdraw small amount)
- [ ] Invite 2-3 trusted community members to mint
- [ ] Verify their mints, claims work correctly
- [ ] Run for at least 1 full epoch cycle before going public
- [ ] Remove unlisted URL restriction, add mint page to fry.farm navigation

## Private Mint URL

During testing, the mint page will be accessible at an unlisted path
(e.g., fry.farm/genesis-mint) that is NOT linked from the main navigation.
Only share this URL with invited testers.

When ready to go public:
1. Add the mint page link to fry.farm navigation
2. Update Discord announcement
3. Consider keeping admin_pause(0) confirmation as a final go/no-go gate
