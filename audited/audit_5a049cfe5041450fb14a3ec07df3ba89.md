### Title
Gas-Exhausting Ratifier Bypasses `try/catch` in Bundle Functions, Causing Full Transaction DoS — (`src/periphery/MidnightBundles.sol`)

---

### Summary

`MidnightBundles.sol` implements four bundle functions that iterate over a list of offers and use `try/catch` to skip individual failed `take` calls. However, `Midnight.sol`'s `take` function forwards all available gas to the external `ratifier` contract with no gas cap. A malicious maker can deploy a ratifier that consumes all forwarded gas, leaving only the EVM-reserved 1/64 fraction for the caller. This residual gas is insufficient to continue the bundle loop, causing the entire transaction to revert — defeating the `try/catch` skip-on-failure design.

---

### Finding Description

**Root cause — uncapped external call in `Midnight.sol`:**

In `take()`, the ratifier is called with no gas limit:

```solidity
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
``` [1](#0-0) 

The `offer.ratifier` is an attacker-controlled address. The only guard is that `isAuthorized[offer.maker][offer.ratifier]` must be true — a condition the maker satisfies by self-authorizing their own malicious contract. [1](#0-0) 

**Vulnerable skip-on-failure pattern in `MidnightBundles.sol`:**

All four bundle functions use the same `try/catch {}` pattern to skip reverted takes:

```solidity
try IMidnight(MIDNIGHT).take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, ...) returns (...) {
    filledUnits += unitsToTake;
    ...
} catch {}
``` [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

The NatSpec on each function explicitly documents this intent: *"If taking an offer reverts, the bundler will completely skip this offer."* [6](#0-5) 

**Why `try/catch` fails against a gas-exhausting ratifier:**

The EVM's 63/64 rule means that when `MidnightBundles` calls `Midnight.take` with gas `G`, `Midnight` receives `≈63G/64`. When `Midnight` calls `isRatified`, it forwards `≈63²G/64²` to the ratifier. If the ratifier consumes all of that, `Midnight` has only `≈G/4096` left, reverts out-of-gas, and returns control to `MidnightBundles` with only `≈G/64` remaining. With a 30M gas block limit, a single poison offer leaves `≈468K` gas for all remaining bundle operations. A second poison offer leaves `≈7.3K` — far too little to continue. The `catch {}` block itself may not even execute cleanly.

---

### Impact Explanation

- **All four bundle functions are affected**: `buyWithUnitsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithUnitsTarget`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithAssetsTarget`.
- A taker attempting a market-style buy or sell via the bundler has their transaction reverted entirely, losing only gas.
- An attacker who advertises a poison offer at an attractive price (best tick) will have it included first in any off-chain-constructed bundle, guaranteeing the DoS.
- The attack is **free to sustain**: the attacker only needs to deploy a gas-consuming contract once and authorize it as a ratifier. No tokens are at risk for the attacker.
- This can be used to selectively disable a competitor's market or prevent liquidations that go through the bundler.

---

### Likelihood Explanation

- **Attacker preconditions**: none beyond deploying a contract and calling `setIsAuthorized`. No privileged role required.
- **Trigger**: post a single offer at the best available tick in a target market. Off-chain order books and routing bots will naturally include it first.
- **Cost**: zero ongoing cost. One-time contract deployment (~21K gas).
- **Detection difficulty**: the poison offer looks identical to a legitimate offer on-chain until `take` is attempted.

---

### Recommendation

Cap the gas forwarded to the ratifier in `Midnight.sol`'s `take` function. The gas limit can be:

1. **A parameter supplied by the taker** (most flexible — matches the audit team's note in the external report), added to the `take` function signature.
2. **A field in the `Offer` struct** set by the maker, so the maker declares the maximum gas their ratifier needs.
3. **A protocol-level constant** as a conservative upper bound (least flexible but simplest).

The fix should be applied at the `isRatified` call site:

```solidity
// Example: taker-supplied gas cap
IRatifier(offer.ratifier).isRatified{gas: maxRatifierGas}(offer, ratifierData)
``` [1](#0-0) 

---

### Proof of Concept

**Setup:**

1. Deploy `PoisonRatifier`:
```solidity
contract PoisonRatifier {
    function isRatified(Offer memory, bytes memory) external view returns (bytes32) {
        // Consume all forwarded gas
        assembly { invalid() }
    }
}
```

2. Maker calls `Midnight.setIsAuthorized(address(poisonRatifier), true, maker)`.

3. Maker constructs an `Offer` with `ratifier = address(poisonRatifier)` at the best tick in the target market.

**Attack:**

4. A taker (or routing bot) calls `MidnightBundles.buyWithUnitsTargetAndWithdrawCollateral(...)` with a `takes[]` array where the poison offer appears first (best price).

5. The bundle loop reaches the poison offer. `Midnight.take` calls `poisonRatifier.isRatified` with ~63²/64² of the bundle's gas. `invalid()` consumes it all.

6. `Midnight.take` reverts out-of-gas. `MidnightBundles` has ~1/64 of its original gas left.

7. If a second poison offer exists in `takes[]`, the remaining gas drops to ~1/64² of the original — the transaction reverts entirely with out-of-gas.

**Expected result:** The bundle transaction reverts. The taker's `targetUnits` are never filled. The attacker spent nothing beyond initial deployment. [7](#0-6) [1](#0-0)

### Citations

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/periphery/MidnightBundles.sol (L43-44)
```text
    /// @dev Reverts if ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
```

**File:** src/periphery/MidnightBundles.sol (L71-86)
```text
        for (uint256 i; i < takes.length && filledUnits < targetUnits; i++) {
            require(!takes[i].offer.buy, InconsistentSide());
            require(IMidnight(MIDNIGHT).toId(takes[i].offer.market) == id, InconsistentMarket());
            uint256 unitsToTake = min(
                targetUnits - filledUnits,
                takes[i].units,
                ConsumableUnitsLib.consumableUnits(MIDNIGHT, id, takes[i].offer)
            );
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }
```

**File:** src/periphery/MidnightBundles.sol (L152-160)
```text
            try IMidnight(MIDNIGHT)
                .take(
                    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
                ) returns (
                uint256, uint256 resSellerAssets
            ) {
                filledUnits += unitsToTake;
                filledSellerAssets += resSellerAssets;
            } catch {}
```

**File:** src/periphery/MidnightBundles.sol (L215-221)
```text
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
```

**File:** src/periphery/MidnightBundles.sol (L292-300)
```text
            try IMidnight(MIDNIGHT)
                .take(
                    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
                ) returns (
                uint256, uint256 resSellerAssets
            ) {
                filledUnits += unitsToTake;
                filledSellerAssets += resSellerAssets;
            } catch {}
```
