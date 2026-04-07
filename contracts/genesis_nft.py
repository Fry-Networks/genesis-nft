"""fry.farm Genesis NFT — ARC-72 collection contract.

1,000 unique cyberpunk french fry characters.
175 USDC mint price. Upgradeable. Box storage for ownership.
"""

import typing

from algopy import (
    ARC4Contract,
    Account,
    BoxMap,
    Bytes,
    Global,
    GlobalState,
    String,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    op,
    subroutine,
)

@subroutine
def uint64_to_string(value: UInt64) -> String:
    """Convert a UInt64 to its decimal string representation."""
    if value == 0:
        return String("0")
    digits = Bytes(b"0123456789")
    result = Bytes(b"")
    remaining = value
    while remaining > 0:
        digit = remaining % UInt64(10)
        remaining //= UInt64(10)
        result = op.extract(digits, digit, 1) + result
    return String.from_bytes(result)


class GenesisNFT(ARC4Contract):
    """fry.farm Genesis NFT — ARC-72 collection.

    1,000 unique cyberpunk french fry characters.
    Holders receive 10% of all fry.farm platform fees distributed monthly in FRY.
    """

    def __init__(self) -> None:
        # Global state
        self.admin = GlobalState(Account)
        self.total_minted = GlobalState(UInt64)
        self.max_supply = GlobalState(UInt64)
        self.mint_price = GlobalState(UInt64)
        self.mint_asset_id = GlobalState(UInt64)
        self.treasury = GlobalState(Account)
        self.base_uri = GlobalState(Bytes)
        self.paused = GlobalState(UInt64)
        self.royalty_bps = GlobalState(UInt64)

        # Box storage
        self.owners = BoxMap(UInt64, Account, key_prefix=b"o")
        self.approved = BoxMap(UInt64, Account, key_prefix=b"a")
        self.balances = BoxMap(Account, UInt64, key_prefix=b"b")
        self.operators = BoxMap(Bytes, UInt64, key_prefix=b"")

    # ── App lifecycle ─────────────────────────────────────────────────────

    @arc4.abimethod(create="require")
    def create(
        self,
        admin: arc4.Address,
        treasury: arc4.Address,
        mint_price: arc4.UInt64,
        max_supply: arc4.UInt64,
        mint_asset_id: arc4.UInt64,
        base_uri: arc4.String,
        royalty_bps: arc4.UInt64,
    ) -> None:
        self.admin.value = admin.native
        self.treasury.value = treasury.native
        self.mint_price.value = mint_price.as_uint64()
        self.max_supply.value = max_supply.as_uint64()
        self.mint_asset_id.value = mint_asset_id.as_uint64()
        self.base_uri.value = base_uri.native.bytes
        assert royalty_bps.as_uint64() <= UInt64(10000), "Royalty cannot exceed 10000 bps"
        self.royalty_bps.value = royalty_bps.as_uint64()
        self.paused.value = UInt64(0)
        self.total_minted.value = UInt64(0)

    @arc4.baremethod(allow_actions=["UpdateApplication"])
    def update(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    @arc4.baremethod(allow_actions=["DeleteApplication"])
    def delete(self) -> None:
        assert Txn.sender == self.admin.value, "Only admin"

    # ── Minting ───────────────────────────────────────────────────────────

    @arc4.abimethod
    def mint(self, payment: gtxn.AssetTransferTransaction) -> arc4.UInt64:
        assert self.paused.value == UInt64(0), "Minting is paused"
        assert self.total_minted.value < self.max_supply.value, "Max supply reached"
        assert payment.xfer_asset.id == self.mint_asset_id.value, "Wrong asset"
        assert payment.asset_amount == self.mint_price.value, "Payment must equal mint price"
        assert payment.sender == Txn.sender, "Payment sender must be caller"
        assert (
            payment.asset_receiver == Global.current_application_address
        ), "Wrong receiver"

        # Assign 1-indexed token ID
        token_id = self.total_minted.value + UInt64(1)
        self.total_minted.value = token_id

        # Record ownership
        self.owners[token_id] = Txn.sender
        current_balance, exists = self.balances.maybe(Txn.sender)
        if exists:
            self.balances[Txn.sender] = current_balance + UInt64(1)
        else:
            self.balances[Txn.sender] = UInt64(1)

        # Forward USDC to treasury
        itxn.AssetTransfer(
            xfer_asset=self.mint_asset_id.value,
            asset_amount=payment.asset_amount,
            asset_receiver=self.treasury.value,
            fee=0,
        ).submit()

        # Emit ARC-72 transfer event (mint = from zero address)
        arc4.emit(
            "arc72_Transfer(address,address,uint64)",
            arc4.Address(Global.zero_address),
            arc4.Address(Txn.sender),
            arc4.UInt64(token_id),
        )

        return arc4.UInt64(token_id)

    # ── ARC-72 queries ────────────────────────────────────────────────────

    @arc4.abimethod(readonly=True)
    def arc72_ownerOf(self, token_id: arc4.UInt64) -> arc4.Address:
        tid = token_id.as_uint64()
        assert tid in self.owners, "Token does not exist"
        return arc4.Address(self.owners[tid])

    @arc4.abimethod(readonly=True)
    def arc72_totalSupply(self) -> arc4.UInt64:
        return arc4.UInt64(self.total_minted.value)

    @arc4.abimethod(readonly=True)
    def arc72_balanceOf(self, owner: arc4.Address) -> arc4.UInt64:
        balance, exists = self.balances.maybe(owner.native)
        if exists:
            return arc4.UInt64(balance)
        return arc4.UInt64(0)

    @arc4.abimethod(readonly=True)
    def arc72_tokenURI(self, token_id: arc4.UInt64) -> arc4.String:
        tid = token_id.as_uint64()
        assert tid in self.owners, "Token does not exist"
        uri = String.from_bytes(self.base_uri.value) + uint64_to_string(tid) + ".json"
        return arc4.String(uri)

    @arc4.abimethod(readonly=True)
    def arc72_isApprovedForAll(
        self, owner: arc4.Address, operator: arc4.Address
    ) -> arc4.Bool:
        op_key = owner.native.bytes + operator.native.bytes
        op_val, exists = self.operators.maybe(op_key)
        if exists:
            return arc4.Bool(op_val == UInt64(1))
        return arc4.Bool(False)

    @arc4.abimethod(readonly=True)
    def supportsInterface(
        self,
        interface_id: arc4.StaticArray[arc4.Byte, typing.Literal[4]],
    ) -> arc4.Bool:
        return arc4.Bool(interface_id.bytes == Bytes.from_hex("53f02a40"))

    # ── ARC-72 transfers & approvals ──────────────────────────────────────

    @arc4.abimethod
    def arc72_transferFrom(
        self,
        from_: arc4.Address,
        to: arc4.Address,
        token_id: arc4.UInt64,
    ) -> None:
        tid = token_id.as_uint64()
        from_account = from_.native
        to_account = to.native
        assert to_account != Global.zero_address, "Cannot transfer to zero address"

        # Verify ownership
        owner = self.owners[tid]
        assert owner == from_account, "from_ is not the owner"

        # Check authorization: owner OR approved OR operator
        is_authorized = Txn.sender == owner

        if not is_authorized:
            approved_addr, has_approval = self.approved.maybe(tid)
            if has_approval:
                is_authorized = Txn.sender == approved_addr

        if not is_authorized:
            op_key = from_account.bytes + Txn.sender.bytes
            op_val, op_exists = self.operators.maybe(op_key)
            if op_exists:
                is_authorized = op_val == UInt64(1)

        assert is_authorized, "Not authorized"

        # Transfer ownership
        self.owners[tid] = to_account

        # Clear any existing approval
        if tid in self.approved:
            del self.approved[tid]

        # Update balances
        from_balance = self.balances[from_account]
        self.balances[from_account] = from_balance - UInt64(1)

        to_balance, to_exists = self.balances.maybe(to_account)
        if to_exists:
            self.balances[to_account] = to_balance + UInt64(1)
        else:
            self.balances[to_account] = UInt64(1)

        arc4.emit(
            "arc72_Transfer(address,address,uint64)",
            from_,
            to,
            token_id,
        )

    @arc4.abimethod
    def arc72_approve(
        self, approved_addr: arc4.Address, token_id: arc4.UInt64
    ) -> None:
        tid = token_id.as_uint64()
        owner = self.owners[tid]
        assert Txn.sender == owner, "Only owner can approve"

        self.approved[tid] = approved_addr.native

        arc4.emit(
            "arc72_Approval(address,address,uint64)",
            arc4.Address(owner),
            approved_addr,
            token_id,
        )

    @arc4.abimethod
    def arc72_setApprovalForAll(
        self, operator: arc4.Address, is_approved: arc4.Bool
    ) -> None:
        op_key = Txn.sender.bytes + operator.native.bytes
        if is_approved.native:
            self.operators[op_key] = UInt64(1)
        else:
            if op_key in self.operators:
                del self.operators[op_key]

        arc4.emit(
            "arc72_ApprovalForAll(address,address,bool)",
            arc4.Address(Txn.sender),
            operator,
            is_approved,
        )

    # ── Royalties ─────────────────────────────────────────────────────────

    @arc4.abimethod(readonly=True)
    def royalty_info(
        self, token_id: arc4.UInt64, sale_price: arc4.UInt64
    ) -> arc4.Tuple[arc4.Address, arc4.UInt64]:
        tid = token_id.as_uint64()
        assert tid in self.owners, "Token does not exist"
        price = sale_price.as_uint64()
        royalty_amount = price * self.royalty_bps.value // UInt64(10000)
        return arc4.Tuple(
            (
                arc4.Address(self.treasury.value),
                arc4.UInt64(royalty_amount),
            )
        )

    # ── Admin ─────────────────────────────────────────────────────────────

    @arc4.abimethod
    def admin_pause(self, new_paused: arc4.UInt64) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        assert new_paused.as_uint64() <= UInt64(1), "Pause value must be 0 or 1"
        self.paused.value = new_paused.as_uint64()

    @arc4.abimethod
    def admin_set_base_uri(self, new_uri: arc4.String) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.base_uri.value = new_uri.native.bytes

    @arc4.abimethod
    def admin_set_treasury(self, new_treasury: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.treasury.value = new_treasury.native

    @arc4.abimethod
    def admin_set_mint_price(self, new_price: arc4.UInt64) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.mint_price.value = new_price.as_uint64()

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
    def admin_withdraw(
        self, asset_id: arc4.UInt64, amount: arc4.UInt64, receiver: arc4.Address
    ) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        itxn.AssetTransfer(
            xfer_asset=asset_id.as_uint64(),
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

    @arc4.abimethod
    def admin_set_admin(self, new_admin: arc4.Address) -> None:
        assert Txn.sender == self.admin.value, "Only admin"
        self.admin.value = new_admin.native
