Audit Report

## Title
Fully-consumed buy offer (`maxAssets` mode) can be re-taken with zero-asset units, mutating position state without payment - (File: src/Midnight.sol)

## Summary
When a buy offer's `consumed[maker][group]` has reached `maxAssets`, a taker can call `take()` with `units = 1` at any tick below `MAX_TICK` such that `units * buyerPrice < WAD`. `mulDivDown` rounds `buyerAssets` to zero, the consumed counter is not incremented, and the cap check passes trivially. The call then mutates `buyerPos.credit`, `sellerPos.debt`, and `totalUnits` while transferring zero tokens, permanently breaking the invariant that every credit unit corresponds to an asset payment.

## Finding Description
**Root cause — rounding to zero in asset computation:**

`buyerAssets` is computed at line 363 using `mulDivDown`: [1](#0-0) 

`mulDivDown` is plain integer division with no zero-result guard: [2](#0-1) 

When `units * buyerPrice < WAD` (e.g., `units = 1` and any tick below `MAX_TICK`), this returns 0.

**Cap check becomes a no-op:**

The consumed counter is incremented by `buyerAssets` (0) and the cap is checked: [3](#0-2) 

If `consumed[maker][group]` is already equal to `maxAssets`, then `newConsumed == maxAssets` and `require(newConsumed <= offer.maxAssets)` evaluates to `require(M <= M)` — always true. There is no check that `buyerAssets > 0` when `units > 0`, and no check that `newConsumed > consumedBefore`.

**State mutation without payment:**

Execution proceeds to mutate positions and market state with `units = 1`: [4](#0-3) 

The token transfer at line 455 sends `buyerAssets - sellerAssets = 0 - 0 = 0` tokens: [5](#0-4) 

**Exploit flow:**
1. Maker creates a buy offer with `maxAssets = M`, `maxUnits = 0`, tick below `MAX_TICK`.
2. Offer is legitimately filled until `consumed[maker][group] == M`.
3. Attacker calls `take(offer, ..., units=1)`. `buyerAssets = 0`, consumed stays at `M`, cap check passes.
4. `buyerPos.credit += 1`, `sellerPos.debt += 1`, `totalUnits += 1` — zero tokens transferred.
5. Steps 3–4 are repeatable indefinitely, including within a single transaction via multicall.

## Impact Explanation
An attacker can accrue unbounded `credit` for the maker and unbounded `debt` for any target seller at zero cost. `totalUnits` and `pendingFee` state inflate without any corresponding asset transfer, permanently corrupting the protocol's core accounting invariant. This constitutes unauthorized state mutation reachable by any unprivileged external user and falls within the in-scope impact class of unauthorized state changes and accounting corruption.

## Likelihood Explanation
All preconditions are attacker-controllable with no privileged access: `offer.buy == true` and `offer.maxAssets > 0` are standard buy offer parameters; `buyerPrice < WAD` holds for any tick below `MAX_TICK` (the vast majority of real offers); `consumed[maker][group] >= maxAssets` is reachable by normal fills. `units = 1` is sufficient. No flash loan, oracle manipulation, or special role is required. The attack is repeatable in the same block via multicall.

## Recommendation
Add a guard requiring `buyerAssets > 0` (or equivalently `units > 0` after asset computation) before proceeding with state mutation. For example, immediately after line 364:

```solidity
require(buyerAssets > 0 || units == 0, ZeroAssetTake());
```

Alternatively, require that the consumed counter strictly increases when `maxAssets > 0`:

```solidity
uint256 consumedBefore = consumed[offer.maker][offer.group];
newConsumed = consumedBefore + (offer.buy ? buyerAssets : sellerAssets);
require(newConsumed > consumedBefore, ZeroAssetIncrement());
require(newConsumed <= offer.maxAssets, ConsumedAssets());
consumed[offer.maker][offer.group] = newConsumed;
```

This ensures a take with zero computed assets cannot proceed when `units > 0`.

## Proof of Concept
A reproducing test `testBugBuyMaxAssetsBypass` already exists in the repository and confirms the behavior: [6](#0-5) 

Manual reproduction steps:
1. Deploy with a buy offer at any tick below `MAX_TICK`, `maxAssets = M`, `maxUnits = 0`.
2. Fill the offer legitimately until `consumed[maker][group] == M`.
3. Call `take(offer, ..., units=1)` as an unprivileged attacker.
4. Observe: call succeeds, `buyerPos.credit` increases by 1, `sellerPos.debt` increases by 1, `totalUnits` increases by 1, zero tokens transferred.
5. Repeat step 3 in a loop within a single transaction to inflate state unboundedly.

### Citations

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L367-373)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L408-417)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/libraries/UtilsLib.sol (L29-31)
```text
    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y) / d;
    }
```

**File:** test/TakeTest.sol (L1-17)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity ^0.8.0;

import {IMidnight, Market, Offer, CollateralParams} from "../src/interfaces/IMidnight.sol";
import {Midnight} from "../src/Midnight.sol";
import {WAD, CALLBACK_SUCCESS, MAX_CONTINUOUS_FEE} from "../src/libraries/ConstantsLib.sol";
import {UtilsLib} from "../src/libraries/UtilsLib.sol";
import {TickLib, MAX_TICK} from "../src/libraries/TickLib.sol";
import {IBuyCallback, ISellCallback} from "../src/interfaces/ICallbacks.sol";
import {IRatifier} from "../src/interfaces/IRatifier.sol";
import {IdLib} from "../src/libraries/IdLib.sol";
import {BaseTest} from "./BaseTest.sol";
import {ERC20} from "./erc20s/ERC20.sol";
import {Oracle} from "./helpers/Oracle.sol";

contract TakeTest is BaseTest {
```
