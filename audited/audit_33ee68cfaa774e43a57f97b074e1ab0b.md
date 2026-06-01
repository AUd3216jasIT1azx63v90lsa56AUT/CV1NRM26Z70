Looking at the exact code paths in `src/periphery/MidnightBundles.sol`:

The code paths are fully confirmed. In all four functions, `takes[0]` is accessed unconditionally at the very first line after the two `require` guards, before any loop or `OutOfOffers` check. With `takes.length == 0`, Solidity 0.8.34 panics with error code `0x32` at that line.

---

### Title
Empty `takes` array causes Solidity panic (0x32) instead of `OutOfOffers` revert - (`src/periphery/MidnightBundles.sol`)

### Summary
All four bundle functions (`buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithUnitsTarget`, `supplyCollateralAndSellWithAssetsTarget`) unconditionally access `takes[0]` before any length guard or loop. When an unprivileged caller passes an empty `takes` array, Solidity 0.8.34 emits a panic with error code `0x32` (array out-of-bounds) rather than the protocol-defined `OutOfOffers()` custom error. The `OutOfOffers` check only appears after the loop body, which is never reached.

### Finding Description
**Exact code path (identical pattern in all four functions):**

```
require(taker == msg.sender || isAuthorized(...), Unauthorized());   // passable: caller is taker
require(referralFeePct < WAD, PctExceeded());                        // passable: pass 0
address loanToken = takes[0].offer.market.loanToken;                 // ← PANIC if takes.length == 0
bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market); // never reached
...
for (uint256 i; i < takes.length ...) { ... }                        // never reached
require(filledUnits == targetUnits, OutOfOffers());                  // never reached
```

Line references:
- `buyWithUnitsTargetAndWithdrawCollateral`: `takes[0]` at line 62 [1](#0-0) 
- `supplyCollateralAndSellWithUnitsTarget`: `takes[0]` at line 129 [2](#0-1) 
- `buyWithAssetsTargetAndWithdrawCollateral`: `takes[0]` at line 193 [3](#0-2) 
- `supplyCollateralAndSellWithAssetsTarget`: `takes[0]` at line 264 [4](#0-3) 

The `OutOfOffers()` reverts are at lines 88, 163, 224, and 303 respectively — all unreachable when `takes` is empty. [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) 

**Attacker inputs:** `takes = new Take[](0)`, `taker = msg.sender` (self), `referralFeePct = 0`. No other preconditions required.

**Why existing checks fail:** The only two guards before `takes[0]` are the authorization check (trivially satisfied when `taker == msg.sender`) and the `referralFeePct < WAD` check (trivially satisfied with `referralFeePct = 0`). There is no `takes.length > 0` guard anywhere before the first `takes[0]` dereference. [9](#0-8) 

### Impact Explanation
The revert selector received by the caller is the ABI-encoded Solidity panic `Panic(uint256)` with argument `0x32`, not the protocol-defined `OutOfOffers()` selector. Any off-chain tooling, SDK, or integrating contract that catches `OutOfOffers()` to handle the "no liquidity" case will fail to match the panic selector and will either propagate the error incorrectly or misclassify it. This is a concrete non-critical behavior divergence: the wrong error type is surfaced for a semantically valid input (empty offer list = no offers available).

### Likelihood Explanation
Trivially reachable by any caller with no preconditions beyond being their own `taker`. Reproducible 100% of the time with `takes = new Take[](0)`. No special state, no privileged role, no oracle manipulation required.

### Recommendation
Add an explicit length guard before the first `takes[0]` access in each of the four functions:

```solidity
require(takes.length > 0, OutOfOffers());
```

This should be inserted immediately after the `referralFeePct` check and before the `takes[0]` dereference in all four functions. [10](#0-9) 

### Proof of Concept
```solidity
function testEmptyTakesPanicsInsteadOfOutOfOffers() public {
    Take[] memory emptyTakes = new Take[](0);

    // buyWithUnitsTargetAndWithdrawCollateral
    vm.prank(lender);
    // Expect OutOfOffers() but actually get Panic(0x32)
    vm.expectRevert(IMidnightBundles.OutOfOffers.selector); // FAILS: actual revert is Panic(0x32)
    midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
        1, 0, lender, _noPermit(), emptyTakes, new CollateralWithdrawal[](0), address(0), 0, address(0)
    );

    // supplyCollateralAndSellWithUnitsTarget
    vm.prank(borrower);
    vm.expectRevert(IMidnightBundles.OutOfOffers.selector); // FAILS: actual revert is Panic(0x32)
    midnightBundles.supplyCollateralAndSellWithUnitsTarget(
        1, 0, borrower, borrower, new CollateralSupply[](0), emptyTakes, 0, address(0)
    );

    // buyWithAssetsTargetAndWithdrawCollateral
    vm.prank(lender);
    vm.expectRevert(IMidnightBundles.OutOfOffers.selector); // FAILS: actual revert is Panic(0x32)
    midnightBundles.buyWithAssetsTargetAndWithdrawCollateral(
        1, 0, lender, _noPermit(), emptyTakes, new CollateralWithdrawal[](0), address(0), 0, address(0)
    );

    // supplyCollateralAndSellWithAssetsTarget
    vm.prank(borrower);
    vm.expectRevert(IMidnightBundles.OutOfOffers.selector); // FAILS: actual revert is Panic(0x32)
    midnightBundles.supplyCollateralAndSellWithAssetsTarget(
        1, type(uint256).max, borrower, borrower, new CollateralSupply[](0), emptyTakes, 0, address(0)
    );
}
```

Each `vm.expectRevert(OutOfOffers.selector)` assertion will fail because the actual revert data is `abi.encodeWithSignature("Panic(uint256)", 0x32)`. To confirm the panic, replace with `vm.expectRevert(abi.encodeWithSignature("Panic(uint256)", 0x32))` — that assertion will pass, proving the wrong selector is emitted. [11](#0-10)

### Citations

**File:** src/periphery/MidnightBundles.sol (L60-64)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);
```

**File:** src/periphery/MidnightBundles.sol (L88-88)
```text
        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L129-129)
```text
        address loanToken = takes[0].offer.market.loanToken;
```

**File:** src/periphery/MidnightBundles.sol (L163-163)
```text
        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L193-193)
```text
        address loanToken = takes[0].offer.market.loanToken;
```

**File:** src/periphery/MidnightBundles.sol (L224-224)
```text
        require(filledBuyerAssets == targetFilledBuyerAssets, OutOfOffers());
```

**File:** src/periphery/MidnightBundles.sol (L264-264)
```text
        address loanToken = takes[0].offer.market.loanToken;
```

**File:** src/periphery/MidnightBundles.sol (L303-303)
```text
        require(filledSellerAssets == targetFilledSellerAssets, OutOfOffers());
```

**File:** src/periphery/interfaces/IMidnightBundles.sol (L40-40)
```text
    error OutOfOffers();
```
