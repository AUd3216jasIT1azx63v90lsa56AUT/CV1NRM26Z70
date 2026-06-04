### Title
Unbounded Gas Forwarded to Untrusted External Callbacks Allows Malicious Maker to Drain Relayer Gas — (File: src/Midnight.sol, src/periphery/MidnightBundles.sol)

---

### Summary

In `Midnight.sol`'s `take()` function, external calls to `IRatifier`, `IEnterGate`, `IBuyCallback`, and `ISellCallback` are made without any gas limit, defaulting to `gasRemaining * 63 / 64`. When a relayer (`msg.sender ≠ taker`) submits transactions through `MidnightBundles.sol`, a malicious maker can manipulate state in their callback or ratifier contract between the relayer's off-chain gas estimation and on-chain execution, causing the relayer to pay significantly more gas than expected.

---

### Finding Description

**Root cause — no gas cap on external calls in `take()`:**

`Midnight.sol`'s `take()` makes four categories of unbounded external calls:

1. **Ratifier** (line 356):
```solidity
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
``` [1](#0-0) 

2. **Enter gates** (lines 398–406):
```solidity
IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer)
IEnterGate(offer.market.enterGate).canIncreaseDebt(seller)
``` [2](#0-1) 

3. **Buy callback** (lines 447–453):
```solidity
IBuyCallback(buyerCallback).onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
``` [3](#0-2) 

4. **Sell callback** (lines 458–474):
```solidity
ISellCallback(sellerCallback).onSell(id, offer.market, sellerAssets, units, sellerPendingFeeDecrease, seller, receiver, sellerCallbackData)
``` [4](#0-3) 

None of these calls use `{gas: N}`. All forward the EIP-150 default of `gasleft() * 63 / 64` to an untrusted external address.

**Relayer entry point — `MidnightBundles.sol`:**

`MidnightBundles.sol` explicitly supports a relayer pattern: `msg.sender` can be an authorized third party different from `taker`. The relayer pays all gas for the transaction.

```solidity
require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
``` [5](#0-4) 

The bundler then calls `take()` in a `try/catch` loop for each offer:

```solidity
try IMidnight(MIDNIGHT)
    .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
    uint256 resBuyerAssets, uint256
) {
    filledUnits += unitsToTake;
    filledBuyerAssets += resBuyerAssets;
} catch {}
``` [6](#0-5) 

The `try/catch` does not protect the relayer from excessive gas consumption — it only catches reverts. Gas consumed by the callback before any revert (or without reverting) is still charged to the relayer.

**Attack flow:**

1. Malicious maker deploys a callback contract (`offer.callback`) whose `onBuy`/`onSell` gas usage is controlled by an external state variable (e.g., a storage array length, a mapping, or a flag).
2. Maker signs an offer with this callback address and distributes it off-chain.
3. Relayer estimates gas for the bundle transaction off-chain — at this point the callback is cheap.
4. Before the relayer's transaction is mined, the maker calls a function on the callback contract to change state (e.g., expand a storage array), making `onBuy`/`onSell` consume significantly more gas.
5. Relayer's transaction executes with the original `gasLimit` estimate. The callback now consumes far more gas than estimated, either exhausting the gas limit (causing the `try` to catch an OOG revert, wasting all gas) or simply consuming more ETH than the relayer budgeted.

The same attack applies to `offer.ratifier`: the maker controls which ratifier is authorized (`isAuthorized[offer.maker][offer.ratifier]`), and a malicious ratifier can have state-dependent gas usage. [7](#0-6) 

---

### Impact Explanation

A relayer submitting bundle transactions through `MidnightBundles.sol` can be made to pay significantly more ETH in gas than they estimated. In the worst case (OOG), the relayer loses the entire gas cost of the transaction while no state change is committed. This is a direct, repeatable financial loss for any relayer operating on this protocol. The attack requires no privileged access — any maker can craft a malicious offer.

---

### Likelihood Explanation

The relayer pattern is explicitly supported and documented in `MidnightBundles.sol`. The attack requires only:
- A malicious maker (no privileged role — any user can be a maker).
- A callback or ratifier contract with state-dependent gas usage (trivial to implement).
- The ability to change state between the relayer's gas estimation and execution (always possible on Ethereum due to mempool latency).

This is a realistic, low-barrier attack against any relayer service integrating with `MidnightBundles`.

---

### Recommendation

1. **Add a `gasLimit` field to the `Offer` struct** (or to the `Take` struct in `MidnightBundles`) specifying the maximum gas to forward to `offer.callback` and `offer.ratifier`. Use `{gas: offer.callbackGasLimit}` syntax on the external calls.

2. **Cap gas forwarded to gates and oracles** with a reasonable fixed limit (e.g., `{gas: 100_000}`), since these are view functions that should not require unbounded computation.

3. **In `MidnightBundles.sol`**, allow the relayer to pass per-offer gas limits and add a pre-flight check that `gasleft()` is sufficient to cover the declared gas limit before each `take()` call, preventing a deliberate under-gas attack.

---

### Proof of Concept

```solidity
// Malicious callback contract controlled by maker
contract MaliciousCallback is IBuyCallback {
    uint256[] public bloat;
    bool public activated;

    // Maker calls this before relayer's tx is mined
    function activate(uint256 n) external {
        activated = true;
        for (uint256 i; i < n; i++) bloat.push(i); // expand storage
    }

    function onBuy(bytes32, Market memory, uint256, uint256, uint256, address, bytes memory)
        external returns (bytes32)
    {
        if (activated) {
            // Read all bloat storage slots — costs ~2100 gas per cold slot
            uint256 len = bloat.length;
            uint256 sum;
            for (uint256 i; i < len; i++) sum += bloat[i];
        }
        return CALLBACK_SUCCESS;
    }
}
```

**Steps:**
1. Deploy `MaliciousCallback`.
2. Create an offer with `offer.callback = address(MaliciousCallback)` and `offer.buy = true`.
3. Relayer estimates gas for `buyWithUnitsTargetAndWithdrawCollateral(...)` — callback is cheap (not activated).
4. Maker calls `MaliciousCallback.activate(5000)` — adds 5000 storage slots.
5. Relayer submits with original gas estimate. The `onBuy` call now reads 5000 cold storage slots (~10.5M gas), exhausting the relayer's gas budget. The `try/catch` catches the OOG, but the relayer has already paid for all consumed gas. [3](#0-2) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L354-356)
```text
        require(offer.maker != taker, SelfTake());
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L397-406)
```text
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

**File:** src/Midnight.sol (L458-474)
```text
        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
        }
```

**File:** src/periphery/MidnightBundles.sol (L60-61)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
```

**File:** src/periphery/MidnightBundles.sol (L79-85)
```text
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
```
