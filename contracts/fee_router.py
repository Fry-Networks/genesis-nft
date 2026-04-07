"""fry.farm FeeRouter — automatic platform fee splitting.

Receives all fry.farm platform fees and splits on-chain:
- (100 - pool_bps/100)% to treasury
- pool_bps/100% to distribution pool
Fully automated, trustless, verifiable.
"""

from algopy import (
    ARC4Contract,
    Account,
    Global,
    GlobalState,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    op,
)


class FeeRouter(ARC4Contract):
    """fry.farm FeeRouter — automatic on-chain fee splitting."""

    def __init__(self) -> None:
        self.admin = GlobalState(Account)
        self.treasury = GlobalState(Account)
        self.pool = GlobalState(Account)
        self.pool_bps = GlobalState(UInt64)
        self.total_routed_algo = GlobalState(UInt64)
        self.total_routed_asa = GlobalState(UInt64)

    # ── App lifecycle ─────────────────────────────────────────────────────

    @arc4.abimethod(create="require")
    def create(
        self,
        admin: arc4.Address,
        treasury: arc4.Address,
        pool: arc4.Address,
        pool_bps: arc4.UInt64,
    ) -> None:
        bps = pool_bps.as_uint64()
        assert bps > UInt64(0), "pool_bps must be > 0"
        assert bps <= UInt64(5000), "pool_bps must be <= 5000"
        self.admin.value = admin.native
        self.treasury.value = treasury.native
        self.pool.value = pool.native
        self.pool_bps.value = bps
        self.total_routed_algo.value = UInt64(0)
        self.total_routed_asa.value = UInt64(0)

    @arc4.baremethod(allow_actions=["UpdateApplication"])
    def update(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    @arc4.baremethod(allow_actions=["DeleteApplication"])
    def delete(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    # ── Fee routing ───────────────────────────────────────────────────────

    # Fee split uses the current pool_bps value. If admin changes
    # pool_bps between a fee arriving and being routed, the new
    # ratio applies to all pending funds. This is acceptable for
    # the Genesis NFT use case.
    @arc4.abimethod
    def route_asa_fee(self, fee_payment: gtxn.AssetTransferTransaction) -> None:
        assert (
            fee_payment.asset_receiver == Global.current_application_address
        ), "Payment must be to this app"
        amount = fee_payment.asset_amount
        assert amount > UInt64(0), "Amount must be > 0"

        pool_share = amount * self.pool_bps.value // UInt64(10000)
        treasury_share = amount - pool_share
        asset_id = fee_payment.xfer_asset.id

        if pool_share > UInt64(0):
            itxn.AssetTransfer(
                xfer_asset=asset_id,
                asset_amount=pool_share,
                asset_receiver=self.pool.value,
                fee=0,
            ).submit()

        if treasury_share > UInt64(0):
            itxn.AssetTransfer(
                xfer_asset=asset_id,
                asset_amount=treasury_share,
                asset_receiver=self.treasury.value,
                fee=0,
            ).submit()

        self.total_routed_asa.value = self.total_routed_asa.value + amount

        arc4.emit(
            "FeeRouted(uint64,uint64,uint64)",
            arc4.UInt64(asset_id),
            arc4.UInt64(pool_share),
            arc4.UInt64(treasury_share),
        )

    # Fee split uses the current pool_bps value (see route_asa_fee note).
    @arc4.abimethod
    def route_algo_fee(self, fee_payment: gtxn.PaymentTransaction) -> None:
        assert (
            fee_payment.receiver == Global.current_application_address
        ), "Payment must be to this app"
        amount = fee_payment.amount
        assert amount > UInt64(0), "Amount must be > 0"

        pool_share = amount * self.pool_bps.value // UInt64(10000)
        treasury_share = amount - pool_share

        if pool_share > UInt64(0):
            itxn.Payment(
                receiver=self.pool.value,
                amount=pool_share,
                fee=0,
            ).submit()

        if treasury_share > UInt64(0):
            itxn.Payment(
                receiver=self.treasury.value,
                amount=treasury_share,
                fee=0,
            ).submit()

        self.total_routed_algo.value = self.total_routed_algo.value + amount

        arc4.emit(
            "FeeRouted(uint64,uint64,uint64)",
            arc4.UInt64(0),
            arc4.UInt64(pool_share),
            arc4.UInt64(treasury_share),
        )

    # ── Flush (safety net) ────────────────────────────────────────────────

    # NOTE: Flush methods are intentionally public (no admin gate).
    # This is a safety net — if route_*_fee inner transactions fail
    # (e.g., receiver not opted in), anyone can trigger a flush to
    # move stuck funds to pool + treasury. No theft vector exists
    # because funds ALWAYS flow to the configured pool and treasury.
    @arc4.abimethod
    def flush_asa(self, asset_id: arc4.UInt64) -> None:
        aid = asset_id.as_uint64()
        balance, has_asset = op.AssetHoldingGet.asset_balance(
            Global.current_application_address, aid
        )
        assert has_asset, "Contract not opted into this asset"
        assert balance > UInt64(0), "No balance to flush"

        pool_share = balance * self.pool_bps.value // UInt64(10000)
        treasury_share = balance - pool_share

        if pool_share > UInt64(0):
            itxn.AssetTransfer(
                xfer_asset=aid,
                asset_amount=pool_share,
                asset_receiver=self.pool.value,
                fee=0,
            ).submit()

        if treasury_share > UInt64(0):
            itxn.AssetTransfer(
                xfer_asset=aid,
                asset_amount=treasury_share,
                asset_receiver=self.treasury.value,
                fee=0,
            ).submit()

        self.total_routed_asa.value = self.total_routed_asa.value + balance

        arc4.emit(
            "FeeRouted(uint64,uint64,uint64)",
            asset_id,
            arc4.UInt64(pool_share),
            arc4.UInt64(treasury_share),
        )

    # Flush is intentionally public (see flush_asa note).
    @arc4.abimethod
    def flush_algo(self) -> None:
        balance = Global.current_application_address.balance
        min_bal = Global.current_application_address.min_balance
        assert balance > min_bal, "No excess ALGO to flush"
        available = balance - min_bal

        pool_share = available * self.pool_bps.value // UInt64(10000)
        treasury_share = available - pool_share

        if pool_share > UInt64(0):
            itxn.Payment(
                receiver=self.pool.value,
                amount=pool_share,
                fee=0,
            ).submit()

        if treasury_share > UInt64(0):
            itxn.Payment(
                receiver=self.treasury.value,
                amount=treasury_share,
                fee=0,
            ).submit()

        self.total_routed_algo.value = self.total_routed_algo.value + available

        arc4.emit(
            "FeeRouted(uint64,uint64,uint64)",
            arc4.UInt64(0),
            arc4.UInt64(pool_share),
            arc4.UInt64(treasury_share),
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
    def admin_set_pool_bps(self, new_bps: arc4.UInt64) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        bps = new_bps.as_uint64()
        assert bps > UInt64(0), "pool_bps must be > 0"
        assert bps <= UInt64(5000), "pool_bps must be <= 5000"
        self.pool_bps.value = bps

    @arc4.abimethod
    def admin_set_treasury(self, new_treasury: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.treasury.value = new_treasury.native

    @arc4.abimethod
    def admin_set_pool(self, new_pool: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.pool.value = new_pool.native

    @arc4.abimethod
    def admin_set_admin(self, new_admin: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.admin.value = new_admin.native

    # ── Read-only ─────────────────────────────────────────────────────────

    @arc4.abimethod(readonly=True)
    def get_config(
        self,
    ) -> arc4.Tuple[arc4.Address, arc4.Address, arc4.UInt64, arc4.UInt64, arc4.UInt64]:
        return arc4.Tuple(
            (
                arc4.Address(self.treasury.value),
                arc4.Address(self.pool.value),
                arc4.UInt64(self.pool_bps.value),
                arc4.UInt64(self.total_routed_algo.value),
                arc4.UInt64(self.total_routed_asa.value),
            )
        )
