"""fry.farm DistributionPool — monthly FRY distributions to Genesis NFT holders.

Accumulates FRY from the FeeRouter's 10% split. Admin starts epochs;
holders pull their share by calling claim(token_id). Ownership is verified
on-chain via a cross-contract call to the GenesisNFT contract. Each
(epoch, token_id) pair can be claimed exactly once — tracked via BoxMap.
"""

from algopy import (
    ARC4Contract,
    Account,
    BoxMap,
    Bytes,
    Global,
    GlobalState,
    Txn,
    UInt64,
    arc4,
    itxn,
    op,
)


class DistributionPool(ARC4Contract):
    """fry.farm DistributionPool — epoch-based FRY distributions (holder-pull)."""

    def __init__(self) -> None:
        self.admin = GlobalState(Account)
        self.fry_asset_id = GlobalState(UInt64)
        self.nft_app_id = GlobalState(UInt64)
        self.total_supply = GlobalState(UInt64)
        self.current_epoch = GlobalState(UInt64)
        self.epoch_active = GlobalState(UInt64)
        self.epoch_total = GlobalState(UInt64)
        self.epoch_per_nft = GlobalState(UInt64)
        self.epoch_claimed_count = GlobalState(UInt64)

        # Claim tracking: key = epoch(8 BE) || token_id(8 BE), value = 1 if claimed
        self.claimed = BoxMap(Bytes, UInt64, key_prefix=b"c")

    # ── App lifecycle ─────────────────────────────────────────────────────

    @arc4.abimethod(create="require")
    def create(
        self,
        fry_asa_id: arc4.UInt64,
        nft_app_id: arc4.UInt64,
        total_supply: arc4.UInt64,
    ) -> None:
        ts = total_supply.as_uint64()
        assert ts > UInt64(0), "Supply must be positive"
        self.admin.value = Txn.sender
        self.fry_asset_id.value = fry_asa_id.as_uint64()
        self.nft_app_id.value = nft_app_id.as_uint64()
        self.total_supply.value = ts
        self.current_epoch.value = UInt64(0)
        self.epoch_active.value = UInt64(0)
        self.epoch_total.value = UInt64(0)
        self.epoch_per_nft.value = UInt64(0)
        self.epoch_claimed_count.value = UInt64(0)

    @arc4.baremethod(allow_actions=["UpdateApplication"])
    def update(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    @arc4.baremethod(allow_actions=["DeleteApplication"])
    def delete(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    # ── Epoch management ──────────────────────────────────────────────────

    @arc4.abimethod
    def start_epoch(self, total_amount: arc4.UInt64) -> arc4.UInt64:
        assert Txn.sender == self.admin.value, "Only admin"
        assert self.epoch_active.value == UInt64(0), "Epoch already active"

        total_amt = total_amount.as_uint64()
        assert total_amt > UInt64(0), "Amount must be positive"

        # Verify contract has enough FRY
        balance, has_asset = op.AssetHoldingGet.asset_balance(
            Global.current_application_address, self.fry_asset_id.value
        )
        assert has_asset, "Not opted into FRY"
        assert balance >= total_amt, "Insufficient FRY balance"

        per_nft = total_amt // self.total_supply.value
        assert per_nft > UInt64(0), "Amount too small for supply"

        new_epoch = self.current_epoch.value + UInt64(1)
        self.current_epoch.value = new_epoch
        self.epoch_active.value = UInt64(1)
        self.epoch_total.value = total_amt
        self.epoch_per_nft.value = per_nft
        self.epoch_claimed_count.value = UInt64(0)

        arc4.emit(
            "EpochStarted(uint64,uint64,uint64)",
            arc4.UInt64(new_epoch),
            total_amount,
            arc4.UInt64(per_nft),
        )

        return arc4.UInt64(new_epoch)

    @arc4.abimethod
    def claim(self, token_id: arc4.UInt64) -> arc4.UInt64:
        assert self.epoch_active.value == UInt64(1), "No active epoch"
        tid = token_id.as_uint64()
        assert tid >= UInt64(1), "Invalid token ID"
        assert tid <= self.total_supply.value, "Invalid token ID"

        # Collision-free key: epoch(8 BE) || token_id(8 BE) = 16 bytes
        claim_key = op.itob(self.current_epoch.value) + op.itob(tid)
        assert claim_key not in self.claimed, "Already claimed this epoch"

        # Cross-contract call: arc72_ownerOf(uint64)address on GenesisNFT
        owner, _txn = arc4.abi_call[arc4.Address](
            "arc72_ownerOf(uint64)address",
            token_id,
            app_id=self.nft_app_id.value,
            fee=0,
        )
        assert owner.native == Txn.sender, "Caller must own this NFT"

        # Mark claimed BEFORE transfer (prevent reentrancy even though AVM doesn't have it)
        self.claimed[claim_key] = UInt64(1)
        amount = self.epoch_per_nft.value

        itxn.AssetTransfer(
            xfer_asset=self.fry_asset_id.value,
            asset_amount=amount,
            asset_receiver=Txn.sender,
            fee=0,
        ).submit()

        self.epoch_claimed_count.value = self.epoch_claimed_count.value + UInt64(1)

        # Auto-complete epoch when all NFTs have claimed
        if self.epoch_claimed_count.value == self.total_supply.value:
            self.epoch_active.value = UInt64(0)

        arc4.emit(
            "Claim(uint64,uint64,address,uint64)",
            arc4.UInt64(self.current_epoch.value),
            token_id,
            arc4.Address(Txn.sender),
            arc4.UInt64(amount),
        )

        return arc4.UInt64(amount)

    # NOTE: If holders have already claimed before cancel, that FRY
    # is legitimately distributed and not returned to the pool.
    # When starting the next epoch, use the current pool FRY balance
    # (start_epoch validates balance >= total_amount).
    @arc4.abimethod
    def cancel_epoch(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        assert self.epoch_active.value == UInt64(1), "No active epoch"
        self.epoch_active.value = UInt64(0)
        self.epoch_total.value = UInt64(0)
        self.epoch_per_nft.value = UInt64(0)
        self.epoch_claimed_count.value = UInt64(0)

    @arc4.abimethod
    def cleanup_claim_box(self, epoch: arc4.UInt64, token_id: arc4.UInt64) -> None:
        # Permissionless: anyone can reclaim MBR from past-epoch claim boxes
        ep = epoch.as_uint64()
        assert ep < self.current_epoch.value, "Can only clean past epochs"
        claim_key = op.itob(ep) + op.itob(token_id.as_uint64())
        if claim_key in self.claimed:
            del self.claimed[claim_key]

    # ── Read-only queries ─────────────────────────────────────────────────

    @arc4.abimethod(readonly=True)
    def get_pool_balance(self) -> arc4.UInt64:
        balance, has_asset = op.AssetHoldingGet.asset_balance(
            Global.current_application_address, self.fry_asset_id.value
        )
        if has_asset:
            return arc4.UInt64(balance)
        return arc4.UInt64(0)

    @arc4.abimethod(readonly=True)
    def get_epoch_info(
        self,
    ) -> arc4.Tuple[arc4.UInt64, arc4.UInt64, arc4.UInt64, arc4.UInt64, arc4.UInt64]:
        return arc4.Tuple(
            (
                arc4.UInt64(self.current_epoch.value),
                arc4.UInt64(self.epoch_active.value),
                arc4.UInt64(self.epoch_total.value),
                arc4.UInt64(self.epoch_per_nft.value),
                arc4.UInt64(self.epoch_claimed_count.value),
            )
        )

    # ── Admin ─────────────────────────────────────────────────────────────

    @arc4.abimethod
    def admin_opt_in(self, asset_id: arc4.UInt64) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        itxn.AssetTransfer(
            xfer_asset=asset_id.as_uint64(),
            asset_amount=0,
            asset_receiver=Global.current_application_address,
            fee=0,
        ).submit()

    @arc4.abimethod
    def admin_set_nft_app_id(self, new_app_id: arc4.UInt64) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.nft_app_id.value = new_app_id.as_uint64()

    @arc4.abimethod
    def admin_withdraw_non_fry(
        self, asset_id: arc4.UInt64, amount: arc4.UInt64, receiver: arc4.Address
    ) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        aid = asset_id.as_uint64()
        assert aid != self.fry_asset_id.value, "Cannot withdraw FRY"
        itxn.AssetTransfer(
            xfer_asset=aid,
            asset_amount=amount.as_uint64(),
            asset_receiver=receiver.native,
            fee=0,
        ).submit()

    @arc4.abimethod
    def admin_withdraw_algo(self, amount: arc4.UInt64, receiver: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        itxn.Payment(
            receiver=receiver.native,
            amount=amount.as_uint64(),
            fee=0,
        ).submit()

    # EMERGENCY: Allows admin to withdraw FRY from the pool.
    # This bypasses the normal claim-only distribution model.
    # Intended for recovery during mainnet testing phase.
    # Consider removing or adding a timelock in a future upgrade
    # once the claim model is proven stable in production.
    @arc4.abimethod
    def admin_emergency_withdraw_fry(
        self, amount: arc4.UInt64, receiver: arc4.Address
    ) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        itxn.AssetTransfer(
            xfer_asset=self.fry_asset_id.value,
            asset_amount=amount.as_uint64(),
            asset_receiver=receiver.native,
            fee=0,
        ).submit()

    @arc4.abimethod
    def admin_set_admin(self, new_admin: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.admin.value = new_admin.native
