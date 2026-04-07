"""fry.farm EventVesting — time-locked reward distribution for event participants.

Holds reward ASA tokens and releases them linearly over a vesting period.
Admin registers participant allocations after event ends; participants
pull their vested share by calling claim(). Each wallet's allocation
and claimed amount are tracked via BoxMaps.

The First Harvest deployment reference:
  asset_id       = 2485314946   (FRY)
  vesting_start  = 1743206400   (2026-03-29T00:00:00Z, event end date)
  vesting_duration = 31536000   (365 days)
  cliff_end      = 0            (no cliff, linear from start)
"""

from algopy import (
    ARC4Contract,
    Account,
    BoxMap,
    Global,
    GlobalState,
    Txn,
    UInt64,
    arc4,
    itxn,
    op,
)


class EventVesting(ARC4Contract):
    """fry.farm EventVesting — linear/cliff vesting for event reward distribution."""

    def __init__(self) -> None:
        self.admin = GlobalState(Account)
        self.asset_id = GlobalState(UInt64)
        self.vesting_start = GlobalState(UInt64)
        self.vesting_duration = GlobalState(UInt64)
        self.cliff_end = GlobalState(UInt64)
        self.total_allocated = GlobalState(UInt64)
        self.total_claimed = GlobalState(UInt64)
        self.allocations_count = GlobalState(UInt64)
        self.finalized = GlobalState(UInt64)

        # Allocation tracking: wallet -> amount allocated (microunits)
        self.allocations = BoxMap(Account, UInt64, key_prefix=b"a")
        # Claim tracking: wallet -> amount already claimed (microunits)
        self.claimed_amounts = BoxMap(Account, UInt64, key_prefix=b"c")

    # -- App lifecycle -----------------------------------------------------

    @arc4.abimethod(create="require")
    def create(
        self,
        asset_id: arc4.UInt64,
        vesting_start: arc4.UInt64,
        vesting_duration: arc4.UInt64,
        cliff_end: arc4.UInt64,
    ) -> None:
        dur = vesting_duration.as_uint64()
        assert dur > UInt64(0), "Duration must be positive"

        ce = cliff_end.as_uint64()
        vs = vesting_start.as_uint64()
        if ce > UInt64(0):
            assert ce > vs, "Cliff must be after vesting start"
            assert ce <= vs + dur, "Cliff must be within vesting period"

        self.admin.value = Txn.sender
        self.asset_id.value = asset_id.as_uint64()
        self.vesting_start.value = vs
        self.vesting_duration.value = dur
        self.cliff_end.value = ce
        self.total_allocated.value = UInt64(0)
        self.total_claimed.value = UInt64(0)
        self.allocations_count.value = UInt64(0)
        self.finalized.value = UInt64(0)

    @arc4.baremethod(allow_actions=["UpdateApplication"])
    def update(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    @arc4.baremethod(allow_actions=["DeleteApplication"])
    def delete(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    # -- Allocation management ---------------------------------------------

    @arc4.abimethod
    def register_allocation(
        self,
        wallet: arc4.Address,
        amount: arc4.UInt64,
    ) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        assert self.finalized.value == UInt64(0), "Already finalized"

        amt = amount.as_uint64()
        assert amt > UInt64(0), "Amount must be positive"
        target = wallet.native

        # Check if updating existing allocation
        existing, exists = self.allocations.maybe(target)
        if exists:
            # Update: adjust total by removing old and adding new
            self.total_allocated.value = self.total_allocated.value - existing + amt
            self.allocations[target] = amt
        else:
            # New allocation: create both boxes
            self.total_allocated.value = self.total_allocated.value + amt
            self.allocations_count.value = self.allocations_count.value + UInt64(1)
            self.allocations[target] = amt
            self.claimed_amounts[target] = UInt64(0)

        arc4.emit(
            "AllocationRegistered(address,uint64)",
            wallet,
            amount,
        )

    @arc4.abimethod
    def finalize(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        assert self.finalized.value == UInt64(0), "Already finalized"
        assert self.allocations_count.value > UInt64(0), "No allocations registered"

        # Verify contract holds enough reward ASA
        balance, has_asset = op.AssetHoldingGet.asset_balance(
            Global.current_application_address, self.asset_id.value
        )
        assert has_asset, "Not opted into reward asset"
        assert balance >= self.total_allocated.value, "Insufficient asset balance"

        self.finalized.value = UInt64(1)

        arc4.emit(
            "VestingFinalized(uint64,uint64)",
            arc4.UInt64(self.allocations_count.value),
            arc4.UInt64(self.total_allocated.value),
        )

    # -- Claiming ----------------------------------------------------------

    @arc4.abimethod
    def claim(self) -> arc4.UInt64:
        assert self.finalized.value == UInt64(1), "Not yet finalized"

        wallet = Txn.sender
        allocation = self.allocations[wallet]  # fails if not registered
        already_claimed = self.claimed_amounts[wallet]

        # Calculate vested amount
        now = Global.latest_timestamp
        start = self.vesting_start.value
        duration = self.vesting_duration.value

        # Before vesting start: nothing vested
        if now <= start:
            vested = UInt64(0)
        else:
            elapsed = now - start
            if elapsed >= duration:
                # Fully vested
                vested = allocation
            else:
                # Wide multiply to avoid UInt64 overflow for large allocations
                # (e.g. 35M FRY = 35e12 microunits * 31e6 seconds > UInt64 max)
                hi, lo = op.mulw(allocation, elapsed)
                q_hi, vested, r_hi, r_lo = op.divmodw(hi, lo, UInt64(0), duration)

        # Cliff check: nothing claimable before cliff ends
        ce = self.cliff_end.value
        if ce > UInt64(0):
            if now < ce:
                vested = UInt64(0)

        claimable = vested - already_claimed
        assert claimable > UInt64(0), "Nothing to claim"

        # Update state BEFORE transfer
        self.claimed_amounts[wallet] = already_claimed + claimable
        self.total_claimed.value = self.total_claimed.value + claimable

        itxn.AssetTransfer(
            xfer_asset=self.asset_id.value,
            asset_amount=claimable,
            asset_receiver=wallet,
            fee=0,
        ).submit()

        arc4.emit(
            "Claimed(address,uint64,uint64)",
            arc4.Address(wallet),
            arc4.UInt64(claimable),
            arc4.UInt64(already_claimed + claimable),
        )

        return arc4.UInt64(claimable)

    # -- Read-only queries -------------------------------------------------

    @arc4.abimethod(readonly=True)
    def get_allocation(
        self,
        wallet: arc4.Address,
    ) -> arc4.Tuple[arc4.UInt64, arc4.UInt64, arc4.UInt64]:
        """Returns (allocation, claimed, claimable_now) for a wallet."""
        target = wallet.native
        allocation, alloc_exists = self.allocations.maybe(target)
        if not alloc_exists:
            return arc4.Tuple((
                arc4.UInt64(0),
                arc4.UInt64(0),
                arc4.UInt64(0),
            ))

        claimed, claimed_exists = self.claimed_amounts.maybe(target)
        if not claimed_exists:
            claimed = UInt64(0)

        # Calculate current claimable
        now = Global.latest_timestamp
        start = self.vesting_start.value
        duration = self.vesting_duration.value

        if self.finalized.value == UInt64(0) or now <= start:
            vested = UInt64(0)
        else:
            elapsed = now - start
            if elapsed >= duration:
                vested = allocation
            else:
                hi, lo = op.mulw(allocation, elapsed)
                q_hi, vested, r_hi, r_lo = op.divmodw(hi, lo, UInt64(0), duration)

        ce = self.cliff_end.value
        if ce > UInt64(0):
            if now < ce:
                vested = UInt64(0)

        if vested > claimed:
            claimable = vested - claimed
        else:
            claimable = UInt64(0)

        return arc4.Tuple((
            arc4.UInt64(allocation),
            arc4.UInt64(claimed),
            arc4.UInt64(claimable),
        ))

    @arc4.abimethod(readonly=True)
    def get_vesting_status(
        self,
    ) -> arc4.Tuple[arc4.UInt64, arc4.UInt64, arc4.UInt64, arc4.UInt64, arc4.UInt64, arc4.UInt64]:
        """Returns (total_allocated, total_claimed, count, start, duration, cliff_end)."""
        return arc4.Tuple((
            arc4.UInt64(self.total_allocated.value),
            arc4.UInt64(self.total_claimed.value),
            arc4.UInt64(self.allocations_count.value),
            arc4.UInt64(self.vesting_start.value),
            arc4.UInt64(self.vesting_duration.value),
            arc4.UInt64(self.cliff_end.value),
        ))

    # -- Admin -------------------------------------------------------------

    @arc4.abimethod
    def admin_opt_in(self, asset_id: arc4.UInt64) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        itxn.AssetTransfer(
            xfer_asset=asset_id.as_uint64(),
            asset_amount=0,
            asset_receiver=Global.current_application_address,
            fee=0,
        ).submit()

    # EMERGENCY: Allows admin to withdraw reward ASA from the pool.
    # This bypasses the normal vesting claim model.
    # Intended for recovery during mainnet testing phase.
    # Consider removing or adding a timelock in a future upgrade
    # once the vesting model is proven stable in production.
    @arc4.abimethod
    def admin_emergency_withdraw(
        self,
        amount: arc4.UInt64,
        receiver: arc4.Address,
    ) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        itxn.AssetTransfer(
            xfer_asset=self.asset_id.value,
            asset_amount=amount.as_uint64(),
            asset_receiver=receiver.native,
            fee=0,
        ).submit()

    @arc4.abimethod
    def admin_withdraw_algo(
        self,
        amount: arc4.UInt64,
        receiver: arc4.Address,
    ) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        itxn.Payment(
            receiver=receiver.native,
            amount=amount.as_uint64(),
            fee=0,
        ).submit()

    @arc4.abimethod
    def admin_set_admin(self, new_admin: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.admin.value = new_admin.native

    # -- Box cleanup -------------------------------------------------------

    @arc4.abimethod
    def cleanup_allocation_box(self, wallet: arc4.Address) -> None:
        """Permissionless: anyone can reclaim MBR from fully-claimed allocations."""
        target = wallet.native
        allocation = self.allocations[target]
        claimed = self.claimed_amounts[target]
        assert claimed >= allocation, "Allocation not fully claimed"

        del self.allocations[target]
        del self.claimed_amounts[target]
        self.allocations_count.value = self.allocations_count.value - UInt64(1)
