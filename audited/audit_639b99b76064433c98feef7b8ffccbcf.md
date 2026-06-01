### Title
Fee-on-Transfer Collateral Token Inflates Accounting and Bitmap Before Transfer, Enabling Phantom Collateral Borrowing - ([File: src/libraries/SafeTransferLib.sol])

### Summary
`supplyCollateral` in `src/Midnight.sol` writes `_position.collateral[collateralIndex]` and sets the bitmap bit based on the caller-supplied `assets` parameter before calling `SafeTransferLib.safeTransferFrom`. `SafeTransferLib.safeTransferFrom` only checks that `transferFrom` does not revert and returns true; it performs no balance-before/balance-after check. A fee-on-transfer collateral token where the fee equals `assets` (100%) causes `transferFrom` to succeed while zero tokens arrive, leaving the protocol with phantom accounting: `_position.collateral[collateralIndex] == assets` and the bitmap bit set, but `token.balanceOf(address(this))` unchanged.

### Finding Description
**Exact code path:**

`supplyCollateral` (`src/Midnight.sol` lines 524–546):
- Line 533: `_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets)` — accounting written with `assets` before any transfer.
- Lines 535–541: `if (oldCollateral == 0 && assets > 0)` — bitmap bit set and `MAX_COLLATERALS_PER_BORROWER` check performed, all before the transfer.
- Line 545: `SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets)` — transfer is the last operation.

`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol` lines 24–34):
- Calls `token.transferFrom(from, to, value)`, checks `success == true` and return value decodes to `true`.
- No balance snapshot before/after. A fee-on-transfer token with fee = `assets` satisfies both checks while delivering 0 tokens.

`isHealthy` (`src/Midnight.sol` lines 944–960):
- Iterates the bitmap, reads `_position.collateral[i]` (the phantom accounting value `assets`), and computes `maxDebt += assets.mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(lltv, WAD)`.
- **Correction to the question's claim**: `maxDebt` is NOT zero. It equals `assets * price / ORACLE_PRICE_SCALE * lltv / WAD` — a positive phantom credit line. The borrower can borrow against collateral that was never deposited.

**Attacker inputs:**
- Deploy or use an existing fee-on-transfer ERC-20 token (fee = 100%) as the collateral token in a permissionlessly created market.
- Call `supplyCollateral(market, collateralIndex, assets, onBehalf)` with `assets > 0` and `oldCollateral == 0`.

**Why existing checks fail:**
- `SafeTransferLib` has no received-amount guard.
- The bitmap/accounting update is unconditionally committed before the external call.
- The Certora formal verification explicitly assumes "ERC20 tokens are assumed well-behaved" (`certora/README.md` line 112), so the `Solvency.spec` and `nonZeroCollateralsAreActivated` invariants are not proved against fee-on-transfer tokens.
- `touchMarket` validates LLTV tiers, sorted collateral addresses, and maxLif, but imposes no constraint on token transfer behavior.

### Impact Explanation
1. **Phantom collateral / undercollateralized borrowing**: `_position.collateral[collateralIndex]` records `assets` while the contract holds 0 tokens. `isHealthy` computes a positive `maxDebt` from this phantom value, allowing the borrower to take debt with no real backing — direct protocol insolvency.
2. **Bitmap slot consumed with zero real collateral**: The bit at `collateralIndex` is set and counts toward `MAX_COLLATERALS_PER_BORROWER` (16). Repeating across 16 collateral indices exhausts the borrower's bitmap, blocking any further legitimate collateral supply.
3. **Solvency invariant broken**: The central invariant that "the contract's balance always covers the sum of collateral" (`Solvency.spec`) is violated; lenders bear the loss when the phantom debt cannot be repaid.

### Likelihood Explanation
- **Preconditions**: Market creation is permissionless (`live_context.json`: "permissionless market creation"; `touchMarket` is `public`). An attacker can create a market with a fee-on-transfer token as collateral with no admin involvement.
- **Feasibility**: Fee-on-transfer tokens exist on mainnet (e.g., tokens with configurable transfer taxes). A 100% fee is an extreme but deployable configuration; any fee > 0 causes partial accounting inflation.
- **Repeatability**: The attack can be repeated across multiple collateral indices in the same market until the bitmap is saturated, or across multiple markets.
- **Authorization**: `supplyCollateral` requires `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`, which the attacker satisfies by acting as their own `onBehalf`.

### Recommendation
Add a balance-before/balance-after check in `supplyCollateral` to verify the actual received amount equals `assets`:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
require(IERC20(collateralToken).balanceOf(address(this)) - balanceBefore == assets, FeeOnTransferToken());
```

Alternatively, move all state writes (collateral accounting and bitmap update) to after the transfer and compute `actualReceived = balanceAfter - balanceBefore`, using `actualReceived` instead of `assets` for accounting. This is the pattern used by Morpho Blue and similar protocols.

### Proof of Concept
```solidity
// Foundry unit test
contract FeeOnTransferCollateral is ERC20 {
    // transferFrom always returns true but transfers 0 tokens (100% fee)
    function transferFrom(address, address, uint256) public override returns (bool) {
        return true; // no actual transfer
    }
}

function testPhantomCollateralBitmap() public {
    FeeOnTransferCollateral feeToken = new FeeOnTransferCollateral();
    // Create market with feeToken as collateral (permissionless)
    Market memory m = buildMarket(address(feeToken), ...);
    midnight.touchMarket(m);

    uint256 assets = 1e18;
    feeToken.approve(address(midnight), assets);

    uint256 balBefore = feeToken.balanceOf(address(midnight));
    midnight.supplyCollateral(m, 0, assets, address(this));
    uint256 balAfter = feeToken.balanceOf(address(midnight));

    // Assert: 0 tokens arrived
    assertEq(balAfter - balBefore, 0, "no tokens transferred");
    // Assert: accounting shows assets
    assertEq(midnight.collateral(toId(m), address(this), 0), assets, "phantom collateral recorded");
    // Assert: bitmap bit set
    uint128 bitmap = midnight.collateralBitmap(toId(m), address(this));
    assertEq(UtilsLib.countBits(bitmap), 1, "bitmap bit set");
    assertTrue(UtilsLib.getBit(bitmap, 0), "bit 0 set");
    // Assert: isHealthy returns true for non-zero debt (phantom maxDebt > 0)
    // (after borrowing against the phantom collateral via take)
}
```

**Expected assertions:**
- `token.balanceOf(midnight)` unchanged after `supplyCollateral`
- `midnight.collateral(id, borrower, 0) == assets`
- `countBits(collateralBitmap) == 1`
- `isHealthy` returns `true` for a debt position backed only by phantom collateral
- Repeating 16 times causes `TooManyActivatedCollaterals` revert on the 17th legitimate supply [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L531-545)
```text
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
```

**File:** src/Midnight.sol (L948-957)
```text
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
```

**File:** src/libraries/SafeTransferLib.sol (L24-34)
```text
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

**File:** certora/README.md (L112-112)
```markdown
- ERC20 tokens are assumed well-behaved, see the comments in the respective files for more detail.
```

**File:** certora/specs/CollateralBitmap.spec (L39-40)
```text
strong invariant nonZeroCollateralsAreActivated(bytes32 id, address user, uint256 collateralIndex)
    collateralIndex < 128 => (collateral(id, user, collateralIndex) != 0 <=> summaryGetBit(currentContract.position[id][user].collateralBitmap, collateralIndex));
```
