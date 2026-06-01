### Title
Unchecked uint128 debt accumulation overflow in `take()` causes arithmetic panic DoS - (File: src/Midnight.sol)

### Summary

In `take()`, `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` performs a checked `uint128 += uint128` addition under Solidity 0.8+ semantics. `UtilsLib.toUint128` only validates that the *increment* fits in `uint128`; it does not validate that the *sum* `sellerPos.debt + sellerDebtIncrease` fits. If a seller has accumulated debt near `type(uint128).max`, any subsequent take that produces a non-zero `sellerDebtIncrease` reverts with an arithmetic panic, permanently blocking all takers from filling that seller's offer.

### Finding Description

**Code path:**

`src/Midnight.sol` → `take()`:

```
// line 383-384
uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
uint256 sellerDebtIncrease   = units - sellerCreditDecrease;

// line 414
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);   // ← overflow here
```

`src/libraries/UtilsLib.sol` → `toUint128`:

```
function toUint128(uint256 x) internal pure returns (uint128) {
    require(x <= type(uint128).max, CastOverflow());   // only checks the increment
    return uint128(x);
}
```

**Root cause:** `toUint128` guards only that `sellerDebtIncrease ≤ type(uint128).max`. It does not guard that `sellerPos.debt + sellerDebtIncrease ≤ type(uint128).max`. The subsequent `uint128 += uint128` is a Solidity 0.8 checked operation and panics on overflow.

**Exploit flow:**

1. Seller accumulates `sellerPos.debt = type(uint128).max - 1` through many prior `take()` calls (each individual increment passes `toUint128`; no cumulative cap is enforced).
2. Seller posts a sell offer (`offer.buy = false`, so `seller = offer.maker`).
3. Any taker calls `take(offer, ..., units = 2, ...)`. With `sellerPos.credit = 0`, `sellerCreditDecrease = 0`, `sellerDebtIncrease = 2`.
4. `toUint128(2)` succeeds (2 ≤ `type(uint128).max`).
5. `sellerPos.debt += 2` → `(type(uint128).max - 1) + 2` overflows `uint128` → arithmetic panic revert.

**Why existing checks fail:**

- The `CannotIncreaseDebtPostMaturity` check (line 391) only blocks post-maturity; it does not cap the magnitude.
- The `reduceOnly` check (line 392-395) is opt-in per offer.
- The `enterGate` check (lines 402-406) is optional and market-specific.
- No check anywhere in `take()` asserts `sellerPos.debt + sellerDebtIncrease ≤ type(uint128).max`. [1](#0-0) [2](#0-1) 

### Impact Explanation

Any taker attempting to fill a sell offer from a seller whose debt is within `sellerDebtIncrease` of `type(uint128).max` receives an arithmetic panic revert. Because the seller's debt state is not modified (the transaction reverts entirely), the condition persists indefinitely. No taker can ever successfully fill that offer again with a non-zero `sellerDebtIncrease`, constituting a permanent DoS on `take()` for that seller.

### Likelihood Explanation

**Preconditions:**

- Seller must have `sellerPos.debt` near `type(uint128).max` (`≈ 3.4 × 10^38`). For tokens with 18 decimals this is `≈ 3.4 × 10^20` whole tokens — extreme but not impossible for low-denomination tokens or synthetic assets with no decimal scaling.
- The seller must have sufficient collateral to remain healthy throughout debt accumulation (enforced by the `isHealthy` check at line 476 on every prior take).
- No single take needs to be large; debt can be accumulated incrementally across many calls.

**Feasibility:** Low in mainnet conditions with standard ERC-20 tokens. Elevated for tokens with 0–6 decimals or in test/fork environments. Once the threshold is reached, the DoS is permanent and repeatable for that seller.

### Recommendation

Replace the bare addition with an explicit pre-check before line 414:

```solidity
require(
    uint256(sellerPos.debt) + sellerDebtIncrease <= type(uint128).max,
    DebtOverflow()
);
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

Alternatively, extend `UtilsLib.toUint128` with a variant that accepts a base and an increment and validates the sum, or perform the addition in `uint256` space and then cast the result through `toUint128`.

### Proof of Concept

```solidity
// Foundry unit test
function testSellerDebtOverflowDoS() public {
    // 1. Give seller near-maximum debt by directly writing storage
    //    (or via many take() calls with sufficient collateral).
    uint128 nearMax = type(uint128).max - 1;
    stdstore
        .target(address(midnight))
        .sig("position(bytes32,address)")
        .with_key(id)
        .with_key(seller)
        .depth(4)          // debt field offset in Position struct
        .checked_write(nearMax);

    // 2. Seller posts a sell offer (offer.buy = false).
    Offer memory offer = buildSellOffer(seller, /* units cap */ type(uint256).max);

    // 3. Taker attempts to take 2 units → sellerDebtIncrease = 2.
    vm.prank(taker);
    vm.expectRevert(stdError.arithmeticError);   // uint128 overflow panic
    midnight.take(offer, hex"", 2, taker, taker, address(0), hex"");

    // 4. Assert seller debt is unchanged (transaction reverted).
    assertEq(midnight.debtOf(id, seller), nearMax);
}
```

**Expected assertion:** `vm.expectRevert(stdError.arithmeticError)` passes, confirming the arithmetic panic. The seller's debt remains at `type(uint128).max - 1`, and no taker can ever fill the offer with `sellerDebtIncrease ≥ 2`.

### Citations

**File:** src/libraries/UtilsLib.sol (L38-42)
```text
    function toUint128(uint256 x) internal pure returns (uint128) {
        require(x <= type(uint128).max, CastOverflow());
        // forge-lint: disable-next-item(unsafe-typecast) as x is less than type(uint128).max
        return uint128(x);
    }
```

**File:** src/Midnight.sol (L382-414)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );

        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );

        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```
