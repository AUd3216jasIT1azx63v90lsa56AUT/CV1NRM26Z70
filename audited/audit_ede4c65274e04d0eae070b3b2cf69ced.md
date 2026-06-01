Looking at the exact code paths in `src/Midnight.sol`:

### Title
Fee-on-Transfer Collateral Token Inflates `position.collateral` Accounting, Enabling Undercollateralized Debt Creation - (`src/Midnight.sol`)

### Summary

`supplyCollateral` credits the caller-supplied `assets` parameter directly to `position.collateral[collateralIndex]` before performing the ERC20 transfer, with no balance-before/after check. When the collateral token charges a fee on transfer, Midnight receives fewer tokens than recorded, permanently inflating the position's collateral accounting. `isHealthy` then computes `maxDebt` from the inflated stored value, allowing the seller to carry more debt than their actual collateral supports.

### Finding Description

**Root cause — `supplyCollateral` (`src/Midnight.sol` lines 531–545):**

```solidity
Position storage _position = position[id][onBehalf];
uint256 oldCollateral = _position.collateral[collateralIndex];
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets); // line 533: uses input, not received amount

...

SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets); // line 545: actual receipt = assets*(1-fee)
```

The accounting write at line 533 uses the caller-controlled `assets` parameter. The transfer at line 545 executes after the state update with no balance snapshot. For a 1% fee-on-transfer token: `supplyCollateral(..., 100, ...)` records `position.collateral[i] = 100` but Midnight receives 99.

**No restriction on collateral token type — `touchMarket` (`src/Midnight.sol` lines 757–773):**

Market creation only validates sorted order, non-zero address, allowed LLTV tiers, and valid `maxLif`. There is no check that the collateral token is not a fee-on-transfer token. Any unprivileged actor can create such a market.

**Health check uses inflated value — `isHealthy` (`src/Midnight.sol` lines 944–959):**

```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```

`_position.collateral[i]` is the inflated recorded value (100), not the actual balance (99). `maxDebt` is therefore ~1% higher than the true collateral-backed capacity.

**Health check at end of `take` (`src/Midnight.sol` line 476):**

```solidity
require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

`isHealthy` returns `true` using the inflated collateral, so the take succeeds and the seller's debt is increased beyond what their actual collateral covers.

**`isRatified` in `IEcrecoverRatifier` (`src/ratifiers/interfaces/IEcrecoverRatifier.sol`):**

`isRatified` is a pure signature verification — it checks that the maker signed the offer via `ecrecover`. It has no visibility into collateral token behavior, actual balances, or the accounting delta. It does not and cannot prevent this attack.

**Exploit flow:**
1. Attacker deploys or uses an existing fee-on-transfer ERC20 (1% fee) as collateral token.
2. Anyone calls `touchMarket` with this token as `collateralParams[0].token` — succeeds, no restriction.
3. Maker (seller/borrower) signs a sell offer for this market; `isRatified` will pass for any taker presenting this signature.
4. Seller calls `supplyCollateral(market, 0, 100, seller)` → `position.collateral[0] = 100`, Midnight balance increases by 99.
5. Taker calls `take(offer, ratifierData, units, taker, ...)`.
6. `isRatified` passes (valid signature).
7. `sellerPos.debt += sellerDebtIncrease` — seller's debt is increased.
8. `isHealthy` computes `maxDebt` using `position.collateral[0] = 100`, returns `true`.
9. Take succeeds. Seller holds debt backed by 99 units of actual collateral, but protocol believes it is backed by 100.

### Impact Explanation

The invariant "contract balances cover collateral" is violated from the moment `supplyCollateral` is called with a fee-on-transfer token. The seller's position is undercollateralized at inception: actual collateral held < collateral used to compute `maxDebt`. The seller can borrow up to `100 * price * lltv / WAD` units of debt while only 99 units of collateral are held. The shortfall is proportional to the fee rate and scales with position size. This constitutes concrete undercollateralized debt creation.

### Likelihood Explanation

**Preconditions:**
- A fee-on-transfer ERC20 token exists (common; USDT has historically had this mode, and many DeFi tokens implement it).
- A market is created with this token as collateral — permissionless, no admin required.
- A maker signs a sell offer for this market — normal user action.

**Feasibility:** Fully reachable by any unprivileged user. No admin keys, no oracle manipulation, no impossible values required. The attack is repeatable on every `supplyCollateral` call with a fee-on-transfer token.

### Recommendation

In `supplyCollateral`, measure the actual received amount using a balance snapshot:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Move the state update after the transfer and use `received` instead of `assets`. Alternatively, document that fee-on-transfer tokens are unsupported and enforce this at market creation (e.g., via a transfer-and-check in `touchMarket`).

### Proof of Concept

```solidity
// Foundry unit test
contract FeeOnTransferCollateral is ERC20 {
    // 1% fee on every transfer
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // Midnight receives amount-fee
        return true;
    }
}

function testFeeOnTransferUndercollateralized() public {
    FeeOnTransferCollateral fot = new FeeOnTransferCollateral();
    // Create market with fot as collateral, lltv = 0.77e18
    // oracle price = 1e36 (ORACLE_PRICE_SCALE)
    
    // Seller supplies 100 units
    fot.mint(seller, 100);
    vm.prank(seller); fot.approve(address(midnight), 100);
    vm.prank(seller); midnight.supplyCollateral(market, 0, 100, seller);
    
    // Assert: position records 100, but actual balance is 99
    assertEq(midnight.collateral(id, seller, 0), 100);
    assertEq(fot.balanceOf(address(midnight)), 99); // MISMATCH — invariant violated
    
    // Taker fills sell offer — seller borrows based on inflated collateral
    // maxDebt = 100 * 1e36/1e36 * 0.77e18/1e18 = 77 units
    // but actual max should be 99 * 0.77 = 76.23 units
    take(77, lender, sellerOffer); // succeeds, but position is undercollateralized
    
    assertEq(midnight.debtOf(id, seller), 77);
    // Actual collateral value: 99 * 0.77 = 76.23 < 77 debt → undercollateralized
    assertTrue(fot.balanceOf(address(midnight)) * 77e18 / 100e18 < midnight.debtOf(id, seller));
}
```

**Expected assertions:**
- `midnight.collateral(id, seller, 0) > fot.balanceOf(address(midnight))` — accounting exceeds actual balance.
- `isHealthy` returns `true` despite the position being undercollateralized against actual holdings.
- Debt created exceeds `actualBalance * price * lltv / WAD`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L474-476)
```text
        }
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

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

**File:** src/Midnight.sol (L757-773)
```text
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
```

**File:** src/Midnight.sol (L944-959)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
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
        }
        return maxDebt >= debt;
```

**File:** src/ratifiers/interfaces/IEcrecoverRatifier.sol (L16-33)
```text
interface IEcrecoverRatifier is IRatifier {
    /// ERRORS ///
    error InvalidProof();
    error InvalidSignature();
    error NotMidnight();
    error RootCanceled();
    error Unauthorized();

    /// EVENTS ///
    event CancelRoot(address indexed caller, address indexed maker, bytes32 indexed root);

    /// FUNCTIONS ///
    function cancelRoot(address maker, bytes32 root) external;

    /// STORAGE GETTERS ///
    function MIDNIGHT() external view returns (address);
    function isRootCanceled(address maker, bytes32 root) external view returns (bool);
}
```
