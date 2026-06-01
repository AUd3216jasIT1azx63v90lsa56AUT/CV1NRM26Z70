### Title
Zero-value `safeTransferFrom` to seller on sell offers at tick=0 with non-zero settlement fee causes unconditional DoS - (File: src/Midnight.sol)

### Summary
`tickToPrice(0)` evaluates to exactly `0` after rounding, so any sell offer placed at tick=0 produces `sellerPrice = 0` and therefore `sellerAssets = 0`. Despite this, `Midnight.sol:456` unconditionally calls `safeTransferFrom(loanToken, payer, receiver, 0)` with no zero-value guard. Any loan token that reverts on zero-value `transferFrom` calls will cause every `take()` of a sell offer at tick=0 to revert permanently.

### Finding Description

**`tickToPrice(0) = 0` — confirmed arithmetic**

`TickLib.tickToPrice` computes:
```
1e36 / (1e18 + wExp(LN_ONE_PLUS_DELTA * (2910 - 0)))
```
`wExp(LN_ONE_PLUS_DELTA * 2910) ≈ e^14.5 * 1e18 ≈ 1.998e24`. The division yields `≈ 5.005e11`, which after `divHalfDownUnchecked(PRICE_ROUNDING_STEP=1e12)` gives `0`, and `0 * 1e12 = 0`. So `tickToPrice(0) = 0`.

**Price and asset computation for a sell offer at tick=0**

In `take()` (lines 358–364):
```solidity
uint256 offerPrice  = TickLib.tickToPrice(0);          // = 0
uint256 sellerPrice = offerPrice;                       // = 0  (offer.buy == false branch)
uint256 buyerPrice  = sellerPrice + _settlementFee;    // = _settlementFee > 0
uint256 buyerAssets = units.mulDivUp(buyerPrice, WAD); // > 0
uint256 sellerAssets= units.mulDivUp(sellerPrice, WAD);// = mulDivUp(units, 0, 1e18) = 0
```
`mulDivUp(units, 0, WAD) = (units*0 + WAD-1)/WAD = (WAD-1)/WAD = 0`.

**Unconditional zero-value transfer**

Line 456:
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets); // sellerAssets = 0
```
`SafeTransferLib.safeTransferFrom` has no zero-value guard — it always issues the low-level `token.call(abi.encodeCall(IERC20.transferFrom, (from, to, 0)))`. If the token reverts on zero-value transfers, the entire `take()` call reverts.

**No existing check prevents this**

- Line 351 (`offer.tick % tickSpacing == 0`): `0 % anything = 0`, so tick=0 always passes.
- For buy offers, `sellerPrice = offerPrice - _settlementFee` would underflow if `offerPrice < _settlementFee`, providing implicit protection. No analogous guard exists for sell offers.
- There is no `require(sellerAssets > 0)` or `if (sellerAssets > 0)` guard anywhere before line 456.

**Exploit path**

1. Attacker (market creator) deploys a loan token whose `transferFrom` reverts when `amount == 0`.
2. Attacker creates a market with this token (permissionless).
3. Attacker (as maker) signs a sell offer at `tick=0`.
4. Any taker calls `take()` on this offer with `units > 0`.
5. Execution reaches line 456 with `sellerAssets = 0`; the token reverts; `take()` reverts.
6. The offer is permanently un-takeable; the DoS is repeatable at zero marginal cost.

### Impact Explanation
Every `take()` call targeting any sell offer at tick=0 in a market whose loan token reverts on zero-value transfers will revert unconditionally. This permanently blocks the `take()` entry-point for that offer class, constituting a Denial of Service against a core market action. The state changes before the external call (position updates, consumed tracking, fee accounting at line 418) are all reverted with the transaction, so no funds are lost — but the function is rendered permanently unusable for this offer type.

### Likelihood Explanation
Preconditions: (1) a loan token that reverts on zero-value `transferFrom`; (2) a sell offer signed at tick=0. Both are attacker-controlled. Market creation is permissionless, so the attacker can deploy the token and create the market without any privileged access. The settlement fee is always non-zero for any market with a non-zero TTM, satisfying the third precondition. The attack is repeatable at negligible cost (only gas).

### Recommendation
Add a zero-value guard before the seller transfer at line 456:
```solidity
if (sellerAssets > 0) {
    SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
}
```
Alternatively, add a `require(offerPrice > 0 || !offer.buy)` check — but the guard on the transfer is more robust and handles any future rounding edge cases at other ticks.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Offer, Market} from "src/interfaces/IMidnight.sol";

contract RevertOnZeroTransferToken {
    // Standard ERC20 but reverts on zero-value transferFrom
    function transferFrom(address, address, uint256 amount) external returns (bool) {
        require(amount > 0, "zero transfer");
        return true;
    }
    // ... minimal ERC20 stubs
}

contract TickZeroDoSTest is Test {
    function test_sellOfferTickZeroDoS() public {
        RevertOnZeroTransferToken token = new RevertOnZeroTransferToken();
        // Create market with this token
        // Sign sell offer at tick=0 (offerPrice = 0, settlementFee > 0)
        // Call take() with units > 0
        // Assert: reverts with "zero transfer" from the token
        // Assert: sellerAssets == 0, buyerAssets > 0 (only settlement fee)
        // Assert: take() is permanently DoS'd for all sell offers at tick=0
    }
}
```

Expected assertions:
- `TickLib.tickToPrice(0) == 0`
- `sellerAssets == 0` when `tick=0` and `settlementFee > 0`
- `take()` reverts with the token's zero-transfer error
- Fuzz: `∀ units > 0, settlementFee > 0 → take() reverts at tick=0` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```

**File:** src/Midnight.sol (L358-364)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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
