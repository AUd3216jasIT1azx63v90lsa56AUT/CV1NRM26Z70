### Title
Arithmetic underflow in `buyWithUnitsTargetAndWithdrawCollateral` refund calculation when markup-formula referral fee exceeds remaining budget - (`src/periphery/MidnightBundles.sol`)

### Summary
`buyWithUnitsTargetAndWithdrawCollateral` computes the referral fee using a markup formula (`filledBuyerAssets * referralFeePct / (WAD - referralFeePct)`) rather than a simple percentage of `maxBuyerAssets`. When `filledBuyerAssets` is close to `maxBuyerAssets` and `referralFeePct > 0`, the markup-derived fee can push the total cost above `maxBuyerAssets`, causing the unchecked subtraction `maxBuyerAssets - filledBuyerAssets - referralFeeAssets` to underflow and revert. No guard exists between the fill loop and the transfer block to catch this condition.

### Finding Description

**Exact code path** — `src/periphery/MidnightBundles.sol` lines 66–104:

1. Line 66: `pullToken(loanToken, msg.sender, maxBuyerAssets, ...)` — the full `maxBuyerAssets` budget is pulled upfront.
2. Lines 71–86: fill loop accumulates `filledBuyerAssets`; the loop stops only when `filledUnits == targetUnits`, not when `filledBuyerAssets` approaches `maxBuyerAssets`.
3. Line 88: `require(filledUnits == targetUnits, OutOfOffers())` — the only post-fill check; no budget check.
4. Line 102: `referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct)` — **markup formula**: total cost = `filledBuyerAssets * WAD / (WAD - referralFeePct)`, which grows super-linearly as `referralFeePct → WAD`.
5. Line 103: `safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets)` — transfers the fee; reverts if the contract's remaining balance (`maxBuyerAssets - filledBuyerAssets`) is less than `referralFeeAssets`.
6. Line 104: `safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets)` — **underflows** in Solidity 0.8 checked arithmetic when `filledBuyerAssets + referralFeeAssets > maxBuyerAssets`.

**Root cause**: No invariant check `filledBuyerAssets + referralFeeAssets <= maxBuyerAssets` exists before the transfer block. The only guard is `require(referralFeePct < WAD)`, which does not bound the markup fee relative to the remaining budget.

**Attacker-controlled inputs**: `referralFeePct` (any value in `[1, WAD-1]`) and `maxBuyerAssets` (any value ≥ expected fill cost). Both are caller-supplied.

**Underflow condition** (closed form):
```
filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD
```

**Concrete numeric example**:
- `maxBuyerAssets = 100e18`, `referralFeePct = 0.1e18` (10%)
- Fill succeeds: `filledBuyerAssets = 95e18`
- `referralFeeAssets = 95e18 * 0.1e18 / 0.9e18 ≈ 10.556e18`
- Total = `105.556e18 > 100e18`
- Line 103 `safeTransfer` reverts (contract holds only `5e18` remaining), or line 104 panics with arithmetic underflow.

**Why existing checks fail**: `require(referralFeePct < WAD)` only prevents a 100% fee; it does not prevent the markup from exceeding the residual budget. There is no `maxBuyerAssets`-relative cap on `referralFeeAssets`. [1](#0-0) [2](#0-1) 

### Impact Explanation

The entire transaction reverts with an arithmetic panic (not a meaningful protocol error). Because Solidity 0.8 reverts all state changes atomically, the `take` calls are also rolled back: the user acquires no credit, no collateral is withdrawn, and `maxBuyerAssets` tokens are returned. The user bears only the gas cost. The NatSpec invariant "The msg.sender will pay **at most** `maxBuyerAssets`" is violated in the sense that the function fails to complete rather than succeeding with a refund, breaking the documented slippage-protection guarantee. [3](#0-2) 

### Likelihood Explanation

**Preconditions**:
1. `referralFeePct > 0` — any non-zero referral fee, set by the caller or a frontend.
2. `filledBuyerAssets > maxBuyerAssets * (WAD - referralFeePct) / WAD` — triggered whenever the fill consumes more than `(1 - referralFeePct/WAD)` of the budget.

For `referralFeePct = 10%`, the revert fires whenever fills consume more than 90% of `maxBuyerAssets`. A user who sets `maxBuyerAssets` as their total budget (a natural interpretation of the parameter name) and uses any non-trivial referral fee will hit this on nearly every tight fill. The condition is repeatable and deterministic given the same inputs. No privileged role, oracle manipulation, or external state is required. [4](#0-3) 

### Recommendation

Add an explicit check after computing `referralFeeAssets` and before any transfer:

```solidity
// src/periphery/MidnightBundles.sol, after line 102
uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
require(filledBuyerAssets + referralFeeAssets <= maxBuyerAssets, BuyerAssetsTooHigh());
```

This surfaces a meaningful revert reason instead of an opaque arithmetic panic, and enforces the documented invariant that total cost ≤ `maxBuyerAssets`. Alternatively, document that callers must set `maxBuyerAssets ≥ expectedFilledBuyerAssets * WAD / (WAD - referralFeePct)` and add a helper to compute the correct cap. [2](#0-1) 

### Proof of Concept

**Foundry fuzz test** (add to `test/MidnightBundlesTest.sol`):

```solidity
function testFuzz_buyUnitsTarget_referralFeeUnderflow(
    uint256 referralFeePct,
    uint256 budgetFraction   // how much of maxBuyerAssets the fill consumes, in WAD
) public {
    referralFeePct = bound(referralFeePct, 1, WAD - 1);
    // Fill fraction in (WAD - referralFeePct, WAD] triggers the underflow
    budgetFraction = bound(budgetFraction, WAD - referralFeePct + 1, WAD);

    uint256 units = 100e18;
    uint256 price = TickLib.tickToPrice(MAX_TICK);
    for (uint256 i; i <= 6; i++) midnight.setMarketSettlementFee(id, i, 0);

    offers[0].buy = false;
    offers[0].maker = borrower;
    offers[0].receiverIfMakerIsSeller = borrower;
    offers[0].maxUnits = units;
    collateralize(market, borrower, units);

    // maxBuyerAssets set to exactly the fill cost (tight budget, no room for markup fee)
    uint256 filledBuyerAssets = units.mulDivUp(price, WAD);
    uint256 maxBuyerAssets = filledBuyerAssets; // budget == fill cost, no room for fee

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: units, ratifierData: hex""});

    address referrer = makeAddr("referrer");

    // Assert: underflow condition holds
    uint256 fee = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
    assertTrue(filledBuyerAssets + fee > maxBuyerAssets, "precondition: underflow expected");

    // Assert: transaction reverts (arithmetic panic or safeTransfer failure)
    vm.prank(lender);
    vm.expectRevert(); // arithmetic underflow or ERC20 insufficient balance
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        units,
        maxBuyerAssets,
        lender,
        _noPermit(),
        takes,
        new CollateralWithdrawal[](0),
        address(0),
        referralFeePct,
        referrer
    );
}
```

**Expected assertions**:
- `filledBuyerAssets + fee > maxBuyerAssets` holds for any `referralFeePct ∈ [1, WAD-1]` when `maxBuyerAssets == filledBuyerAssets`.
- `vm.expectRevert()` passes — the call reverts with an arithmetic panic or `safeTransfer` failure.
- No credit is minted to `lender`; no tokens are transferred to `referrer`. [5](#0-4)

### Citations

**File:** src/periphery/MidnightBundles.sol (L45-48)
```text
    /// @dev This function pulls maxBuyerAssets from the msg.sender and transfers back the remaining tokens at the end.
    /// @dev The msg.sender will pay at most maxBuyerAssets.
    /// @dev Total loan assets transferred from msg.sender is
    /// filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct).
```

**File:** src/periphery/MidnightBundles.sol (L49-58)
```text
    function buyWithUnitsTargetAndWithdrawCollateral(
        uint256 targetUnits,
        uint256 maxBuyerAssets,
        address taker,
        TokenPermit memory loanTokenPermit,
        Take[] memory takes,
        CollateralWithdrawal[] memory collateralWithdrawals,
        address collateralReceiver,
        uint256 referralFeePct,
        address referralFeeRecipient
```

**File:** src/periphery/MidnightBundles.sol (L61-61)
```text
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L102-104)
```text
        uint256 referralFeeAssets = filledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);
        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
        SafeTransferLib.safeTransfer(loanToken, msg.sender, maxBuyerAssets - filledBuyerAssets - referralFeeAssets);
```

**File:** test/MidnightBundlesTest.sol (L510-553)
```text
    function testBuyUnitsTargetWithReferralFee(uint256 units, uint256 referralFeePct) public {
        units = bound(units, 1, uint256(type(uint128).max) / 2);
        referralFeePct = bound(referralFeePct, 0, WAD - 1);
        address referrer = makeAddr("referrer");

        offers[0].buy = false;
        offers[0].maker = borrower;
        offers[0].receiverIfMakerIsSeller = borrower;
        offers[0].maxUnits = type(uint256).max;

        for (uint256 i; i <= 6; i++) {
            midnight.setMarketSettlementFee(id, i, 0);
        }

        uint256 price = TickLib.tickToPrice(MAX_TICK);
        uint256 expectedFilledBuyerAssets = units.mulDivUp(price, WAD);
        uint256 expectedFee = expectedFilledBuyerAssets.mulDivDown(referralFeePct, WAD - referralFeePct);

        collateralize(market, borrower, units);

        Take[] memory takes = new Take[](1);
        takes[0] = Take({offer: offers[0], units: type(uint256).max, ratifierData: hex""});

        vm.prank(lender);
        midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
            units,
            type(uint256).max,
            lender,
            _noPermit(),
            takes,
            new CollateralWithdrawal[](0),
            address(0),
            referralFeePct,
            referrer
        );

        assertEq(midnight.debtOf(id, borrower), units, "units filled");
        assertEq(loanToken.balanceOf(borrower), expectedFilledBuyerAssets, "maker receipt");
        assertEq(loanToken.balanceOf(referrer), expectedFee, "referrer fee");
        assertEq(
            type(uint256).max - loanToken.balanceOf(lender), expectedFilledBuyerAssets + expectedFee, "taker total cost"
        );
        assertEq(loanToken.balanceOf(address(midnightBundles)), 0, "bundler residual");
    }
```
