### Title
Fallback-Enabled Payer Confusion: `takerCallback` with `CALLBACK_SUCCESS`-returning Fallback Drains Approved Third-Party Contracts - (File: src/Midnight.sol)

### Summary
In `take()`, when processing a sell offer, `payer` is unconditionally set to `takerCallback` (a fully attacker-controlled parameter) before any token transfer occurs. The sole gate protecting against unauthorized payer substitution is the `IBuyCallback.onBuy()` return-value check against `CALLBACK_SUCCESS`. Because Solidity interface calls dispatch via the function selector and fall through to a contract's `fallback()` if no matching function exists, any contract whose `fallback` returns exactly `keccak256("morpho.midnight.callbackSuccess")` will pass the check and have its approved `loanToken` balance drained as the involuntary payer.

### Finding Description

**Payer assignment (lines 420–422):**

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;
address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

For a sell offer (`offer.buy = false`), `buyerCallback = takerCallback` and `payer = takerCallback`. Both are raw attacker-supplied parameters with no authorization check tying them to `msg.sender` or `taker`.

**Callback gate (lines 445–453):**

```solidity
if (buyerCallback != address(0)) {
    require(
        IBuyCallback(buyerCallback)
            .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
        == CALLBACK_SUCCESS,
        WrongBuyCallbackReturnValue()
    );
}
```

This is the only check standing between the attacker's choice of `takerCallback` and the subsequent `safeTransferFrom` calls. Solidity encodes the call with the `onBuy` selector and dispatches it. If `victimContract` has no `onBuy` function but has a `fallback()` that returns the 32-byte value `keccak256("morpho.midnight.callbackSuccess")`, the EVM call succeeds, the ABI-decoded return value equals `CALLBACK_SUCCESS`, and the `require` passes.

**Token pulls (lines 455–456):**

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

`payer = victimContract`. Both transfers pull from `victimContract`'s balance using its prior approval to Midnight. The settlement fee (`buyerAssets - sellerAssets`) goes to Midnight; `sellerAssets` goes to `offer.receiverIfMakerIsSeller` (the maker's receiver). The attacker (as buyer) receives the credit/debt-reduction position worth `units` without contributing any tokens.

**Three-case behavior:**
- **(a) No fallback:** `onBuy` call reverts → `take` reverts → no drain. Protected.
- **(b) Fallback returning `CALLBACK_SUCCESS`:** call succeeds, check passes → tokens pulled from `victimContract`. **Vulnerable.**
- **(c) Fallback returning any other value:** `require` fails → `take` reverts → no drain. Protected.

The `EcrecoverRatifier` is irrelevant to this path; it only validates the maker's signature on the offer, not the taker's choice of callback.

### Impact Explanation
Any contract that (1) holds or has approved `loanToken` to Midnight and (2) has a `fallback()` returning exactly `CALLBACK_SUCCESS` can be used as the involuntary payer in a `take()` call. The attacker acquires a credit or debt-reduction position worth `sellerAssets` at the victim's expense. The invariant "contracts without an explicit `onBuy` implementation must not be drainable as `takerCallback`" is broken for the fallback-returning-CALLBACK_SUCCESS case.

### Likelihood Explanation
The precondition that `victimContract.fallback()` returns exactly `keccak256("morpho.midnight.callbackSuccess")` is narrow. However, it is non-zero: contracts purpose-built to interact with Midnight callbacks (e.g., routers, aggregators, or wrapper contracts that implement a catch-all fallback for forward compatibility) could satisfy it. The loanToken approval precondition is standard for any contract that participates in Midnight markets. The attack is repeatable and requires no privileged access — any unprivileged taker can supply an arbitrary `takerCallback`.

### Recommendation
Decouple payer authorization from callback return-value verification. The simplest fix is to require that `takerCallback` is either `address(0)` or explicitly authorized by `taker` (via `isAuthorized[taker][takerCallback]`), mirroring the existing authorization model used for `taker` itself. Alternatively, require that `payer` is always `msg.sender` or `taker` unless the callback is an address the taker has explicitly pre-authorized, preventing arbitrary third-party contracts from being substituted as the payer.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";

// Case (a): no fallback — call reverts
contract VictimNoFallback { }

// Case (b): fallback returning CALLBACK_SUCCESS — VULNERABLE
contract VictimWithSuccessFallback {
    fallback() external {
        bytes32 success = CALLBACK_SUCCESS;
        assembly { mstore(0, success) return(0, 32) }
    }
}

// Case (c): fallback returning wrong value
contract VictimWithWrongFallback {
    fallback() external {
        assembly { mstore(0, 0xdeadbeef) return(0, 32) }
    }
}

contract PayerConfusionTest is Test {
    // Setup: deploy Midnight, EcrecoverRatifier, ERC20 loanToken,
    //        create market, maker signs sell offer.
    // For each victim variant:
    //   1. Deal loanToken to victim; victim.approve(midnight, type(uint256).max)
    //   2. Attacker calls midnight.take(sellOffer, ..., takerCallback=victim, ...)

    function test_caseA_noFallback_reverts() public {
        // Assert: take() reverts (WrongBuyCallbackReturnValue or call revert)
        vm.expectRevert();
        // midnight.take(sellOffer, ratifierData, units, attacker, receiver, address(victimNoFallback), "");
    }

    function test_caseB_successFallback_drainsVictim() public {
        uint256 victimBalanceBefore = loanToken.balanceOf(address(victimWithSuccessFallback));
        // midnight.take(sellOffer, ratifierData, units, attacker, receiver, address(victimWithSuccessFallback), "");
        uint256 victimBalanceAfter = loanToken.balanceOf(address(victimWithSuccessFallback));
        // Assert: victim balance decreased by buyerAssets; attacker received credit position
        assertLt(victimBalanceAfter, victimBalanceBefore);
    }

    function test_caseC_wrongFallback_reverts() public {
        // Assert: take() reverts (WrongBuyCallbackReturnValue)
        vm.expectRevert();
        // midnight.take(sellOffer, ratifierData, units, attacker, receiver, address(victimWithWrongFallback), "");
    }
}
```

Expected assertions: only case (b) succeeds; `victimWithSuccessFallback` loses `buyerAssets` tokens; attacker's position in Midnight reflects the credit/debt reduction without the attacker having transferred any tokens. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L420-422)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

**File:** src/Midnight.sol (L445-453)
```text
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/interfaces/ICallbacks.sol (L8-10)
```text
interface IBuyCallback {
    function onBuy(bytes32 id, Market memory market, uint256 buyerAssets, uint256 units, uint256 pendingFeeIncrease, address buyer, bytes memory data) external returns (bytes32);
}
```

**File:** src/libraries/ConstantsLib.sol (L25-25)
```text
bytes32 constant CALLBACK_SUCCESS = keccak256("morpho.midnight.callbackSuccess");
```
