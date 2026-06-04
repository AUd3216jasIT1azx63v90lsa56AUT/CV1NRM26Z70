### Title
ERC721 Token Accepted as Collateral via `transferFrom` Selector Collision, Permanently Locking NFTs — (File: src/libraries/SafeTransferLib.sol, src/Midnight.sol)

---

### Summary

`ERC20.transferFrom(address,address,uint256)` and `ERC721.transferFrom(address,address,uint256)` share the identical 4-byte selector `0x23b872dd`. Because `SafeTransferLib.safeTransferFrom` dispatches via this selector and accepts empty returndata as success, an ERC721 token can be silently accepted as a `collateralToken` (or `loanToken`) in any permissionlessly created market. The NFT is transferred **in** successfully, but `SafeTransferLib.safeTransfer` calls `transfer(address,uint256)` (selector `0xa9059cbb`), which does not exist on ERC721. That call returns `success=true` with empty returndata on a standard ERC721 (no fallback), passes the `returndata.length == 0` guard, and silently no-ops — leaving the NFT permanently locked in the contract.

---

### Finding Description

**Root cause — `SafeTransferLib.sol` lines 24–34 and 12–22:**

```solidity
// safeTransferFrom — selector 0x23b872dd (matches ERC721.transferFrom)
(bool success, bytes memory returndata) =
    token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
...
require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());

// safeTransfer — selector 0xa9059cbb (NOT present on ERC721)
(bool success, bytes memory returndata) =
    token.call(abi.encodeCall(IERC20.transfer, (to, value)));
...
require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
```

**Inbound path — `Midnight.sol` line 545 (`supplyCollateral`):**

```solidity
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```

`assets` is interpreted as a token ID. ERC721's `transferFrom(from, to, tokenId)` executes, the NFT moves into the contract, and `returndata` is empty → guard passes.

**Outbound path — `Midnight.sol` line 572 (`withdrawCollateral`):**

```solidity
SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
```

`transfer(address,uint256)` does not exist on a standard ERC721. The low-level `.call` returns `(true, "")` (no fallback, no revert). `returndata.length == 0` satisfies the guard. The function returns without reverting, but **no NFT is moved**. The accounting is decremented, the NFT stays in the contract forever.

The same asymmetry applies to:
- `liquidate` outbound: `SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets)` — line 696
- `take` / `withdraw` / `repay` if `loanToken` is an ERC721 — lines 455–456, 499, 520

**No validation in `touchMarket` (lines 755–791) checks that `loanToken` or any `collateralParams[i].token` is actually ERC-20 compliant.** Markets are created permissionlessly by anyone.

---

### Impact Explanation

Any ERC721 token supplied as collateral (or used as a loan token) is **permanently locked** in the Midnight contract with no recovery path:

- `withdrawCollateral` silently no-ops on the actual transfer while decrementing the on-chain collateral balance — the user loses both their accounting position and their NFT.
- `liquidate` similarly silently no-ops the collateral seizure transfer, so liquidators receive nothing while the borrower's debt is reduced.
- `flashLoan` with an ERC721 token would transfer the NFT out but the repayment `safeTransferFrom` would succeed (selector match), so the NFT would be taken without repayment if the callback returns `CALLBACK_SUCCESS` without actually sending it back — but this is a secondary concern.

**Severity: High** — direct, unrecoverable asset loss.

---

### Likelihood Explanation

- Markets are **permissionless**: any address can create a market with any token address as `collateralToken` or `loanToken` by calling `touchMarket` (or any function that calls it).
- A malicious actor creates a market where `collateralParams[0].token` is a popular ERC721 (e.g., a well-known NFT collection). Victims who supply collateral to this market lose their NFTs permanently.
- Even without malicious intent, a developer or integrator who accidentally passes an ERC721 address as a collateral token will silently lose assets — no revert, no warning.
- The `safeTransferFrom` inbound leg succeeds cleanly (real ERC721 approval + transfer), making the operation appear fully successful to the caller.

---

### Recommendation

1. **Add an ERC20 interface check** in `touchMarket` before accepting a token. A minimal check is to call `token.totalSupply()` or verify the token returns a `bool` from `transfer`/`transferFrom` (non-zero returndata length), rejecting tokens that return empty data on `transferFrom`.
2. **Alternatively**, maintain a protocol-level whitelist of approved ERC20 tokens for `loanToken` and collateral tokens, separate from any ERC721 whitelist, mirroring the mitigation recommended in the original report.
3. **Harden `safeTransfer`**: instead of accepting `returndata.length == 0` as unconditional success, require that the call actually mutates balance (e.g., check `balanceOf` before/after), or require non-zero returndata for tokens that are expected to return `bool`.

---

### Proof of Concept

**Setup:**
- Deploy a standard ERC721 contract (`MockNFT`). Mint token ID `1` to Alice.
- Alice approves Midnight to spend her NFT: `mockNFT.approve(address(midnight), 1)`.

**Step 1 — Create market with ERC721 as collateral:**
```solidity
CollateralParams[] memory cp = new CollateralParams[](1);
cp[0] = CollateralParams({
    token: address(mockNFT),   // ERC721 address
    lltv: ..., maxLif: ..., oracle: address(mockOracle)
});
Market memory market = Market({ loanToken: address(someERC20), collateralParams: cp, ... });
midnight.touchMarket(market);
```

**Step 2 — Supply NFT as collateral:**
```solidity
// assets = 1 (token ID)
midnight.supplyCollateral(market, 0, 1, alice);
// safeTransferFrom calls mockNFT.transferFrom(alice, midnight, 1)
// → selector 0x23b872dd matches ERC721.transferFrom → NFT transferred in ✓
// → returndata is empty → guard passes ✓
assert(mockNFT.ownerOf(1) == address(midnight)); // NFT is now in contract
```

**Step 3 — Attempt withdrawal:**
```solidity
midnight.withdrawCollateral(market, 0, 1, alice, alice);
// safeTransfer calls mockNFT.transfer(alice, 1)
// → selector 0xa9059cbb → no such function on ERC721, no fallback
// → call returns (true, "") → returndata.length == 0 → guard passes ✓
// → function returns WITHOUT reverting
assert(mockNFT.ownerOf(1) == address(midnight)); // NFT still stuck ✗
// Alice's collateral[0] is now 0, but NFT is permanently locked
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/libraries/SafeTransferLib.sol (L12-34)
```text
    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
    }

    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
    }
```

**File:** src/Midnight.sol (L524-546)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }

        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
    }
```

**File:** src/Midnight.sol (L549-573)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
    }
```

**File:** src/Midnight.sol (L755-791)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
            for (uint256 i = 0; i < market.collateralParams.length; i++) {
                address collateralToken = market.collateralParams[i].token;
                require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
                uint256 lltv = market.collateralParams[i].lltv;
                require(isLltvAllowed(lltv), LltvNotAllowed());
                require(
                    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
                        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
                    InvalidMaxLif()
                );
                previousCollateralToken = collateralToken;
            }

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```

**File:** src/interfaces/IERC20.sol (L7-8)
```text
    function transfer(address receiver, uint256 amount) external returns (bool);
    function transferFrom(address sender, address receiver, uint256 amount) external returns (bool);
```
