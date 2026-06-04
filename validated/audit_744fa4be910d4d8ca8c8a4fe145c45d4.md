### Title
Gas Griefing via Fake Buy Offers: Maker Token Transfer Checked After Expensive State Mutations — (`src/Midnight.sol`)

### Summary

In `Midnight.sol`'s `take` function, when a maker posts a buy offer (`offer.buy == true`) with no callback, the maker's loan token transfer is the final operation executed — after all expensive state mutations (position updates, consumed tracking, storage writes). A malicious maker can sign a valid buy offer without holding any loan tokens or granting approval, causing any taker who attempts to fill the offer to waste gas on a transaction that is guaranteed to revert. This is the direct analog of the zAuction/zNS gas griefing pattern.

---

### Finding Description

**Root Cause**

In `Midnight.sol`, the `take` function determines the `payer` for the loan token transfer at line 422:

```solidity
address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

When `offer.buy == true` and `offer.callback == address(0)`, `payer` resolves to `buyer = offer.maker`. The actual token pull occurs at lines 455–456:

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

This transfer is the **last** meaningful operation in `take`. Before it executes, the function has already performed:

| Step | Code | Cost |
|---|---|---|
| `touchMarket` (may create market) | L347 | Up to 9 SSTORE writes |
| `isRatified` (ecrecover + Merkle proof) | L356 | ~6,000+ gas |
| `consumed` mapping update | L368–373 | 1 SSTORE |
| `_updatePosition` for buyer | L379 | Multiple SLOAD/SSTORE |
| `_updatePosition` for seller | L380 | Multiple SLOAD/SSTORE |
| Position mutations (credit/debt/pendingFee) | L408–418 | 5+ SSTORE |
| `claimableSettlementFee` update | L418 | 1 SSTORE |
| Callbacks (if any) | L444–475 | Arbitrary |

If the maker has zero balance or zero allowance, `safeTransferFrom` reverts, unwinding all state changes but **consuming all gas** up to that point.

**Attack Path**

1. Attacker (maker) generates a valid EIP-712 signature over a buy offer via `EcrecoverRatifier` — zero on-chain cost.
2. Attacker holds no loan tokens and grants no approval to `Midnight`.
3. Attacker publishes the signed offer off-chain (e.g., via the protocol's order book API).
4. A taker calls `take(offer, ratifierData, units, ...)` to fill the attractive offer.
5. `take` executes all state mutations and callbacks.
6. `safeTransferFrom(loanToken, offer.maker, ...)` reverts — maker has no funds.
7. Entire transaction reverts; taker loses all gas spent.

The attacker can repeat this at zero marginal cost (signing is free).

**Amplification via `MidnightBundles`**

`MidnightBundles` wraps `take` calls in `try/catch` blocks:

```solidity
try IMidnight(MIDNIGHT).take(takes[i].offer, ...) returns (...) {
    ...
} catch {}
```

This means the bundler does **not** revert on a failed `take`, but the gas consumed by each failed call is still charged to the taker. An attacker who floods the off-chain order book with fake buy offers causes the bundler to attempt each one, fail silently, and continue — multiplying the gas waste across every fake offer in the batch.

---

### Impact Explanation

- **Direct financial loss**: Takers pay gas for failed `take` calls. Each failed call can consume 50,000–200,000+ gas depending on market state (new market creation, position update complexity, Merkle proof depth).
- **Bundler amplification**: Via `MidnightBundles`, a single taker transaction can attempt dozens of fake offers, multiplying the gas loss proportionally.
- **Denial of service**: Sustained fake offer spam can make the protocol economically unusable for takers, as the expected gas cost per successful fill becomes unpredictable.
- **Zero cost to attacker**: Signing EIP-712 messages is free; the attacker bears no on-chain cost.

---

### Likelihood Explanation

- **No privileged access required**: Any EOA can become a maker by signing an offer.
- **Trivially executable**: The attacker only needs to sign a message and publish it to the off-chain order book.
- **Economically rational**: On high-fee networks or during congestion, the gas cost to takers can be substantial while the attacker's cost is zero.
- **Scalable**: The attacker can generate thousands of fake offers with different parameters (ticks, maturities, groups) to saturate the order book.

---

### Recommendation

1. **Check maker balance/allowance before expensive state mutations**: Before executing `_updatePosition` and position mutations, verify that `offer.maker` holds sufficient `loanToken` balance and has approved `Midnight` for at least `buyerAssets`. This allows early revert before the expensive SSTORE operations.

2. **Reorder the `isRatified` check to precede `touchMarket`**: `touchMarket` at line 347 can create a new market (9 SSTORE writes). Moving the ratifier check before `touchMarket` reduces gas waste on invalid offers.

3. **Off-chain mitigation**: The protocol's order book frontend/API should validate maker balance and allowance before surfacing offers to takers, consistent with the zAuction resolution acknowledgment ("gas griefing will be mitigated in the dapp with off-client checks").

---

### Proof of Concept

**Setup:**
- `Midnight` deployed, market exists for `loanToken = USDC`.
- Attacker (`maker`) has 0 USDC and 0 approval to `Midnight`.
- Attacker signs a buy offer via `EcrecoverRatifier` for 1,000,000 units at a favorable tick.
- Attacker publishes the signed offer off-chain.

**Execution:**
```solidity
// Taker calls take() with the attacker's offer
midnight.take(
    attackerOffer,       // offer.buy = true, offer.maker = attacker, offer.callback = address(0)
    ratifierData,        // valid EcrecoverRatifier signature
    1_000_000,           // units
    taker,
    address(0),
    address(0),
    ""
);
// Execution path:
// L347: touchMarket() — succeeds (market exists)
// L356: isRatified() — succeeds (valid signature)
// L368: consumed[attacker][group] += units — SSTORE
// L379: _updatePosition(buyer=attacker) — SLOAD/SSTORE
// L380: _updatePosition(seller=taker) — SLOAD/SSTORE
// L408-418: position mutations — 5x SSTORE
// L455: safeTransferFrom(USDC, attacker, ...) — REVERTS (0 balance)
// Result: transaction reverts, taker loses ~100,000+ gas
```

**Bundler amplification:**
```solidity
// Attacker creates 50 fake buy offers, all with 0 USDC
Take[] memory fakeTakes = new Take[](50); // all attacker-signed, no funds
// Taker calls bundler expecting to fill targetUnits
bundles.buyWithUnitsTargetAndWithdrawCollateral(
    targetUnits, maxBuyerAssets, taker, permit, fakeTakes, ...
);
// Each fake take is attempted, fails silently via try/catch
// Taker pays gas for 50 failed take() calls before OutOfOffers() revert
```

**Relevant code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L337-356)
```text
    function take(
        Offer memory offer,
        bytes memory ratifierData,
        uint256 units,
        address taker,
        address receiverIfTakerIsSeller,
        address takerCallback,
        bytes memory takerCallbackData
    ) external returns (uint256, uint256) {
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
        bytes32 id = touchMarket(offer.market);
        MarketState storage _marketState = marketState[id];
        require(_marketState.lossFactor < type(uint128).max, MarketLossFactorMaxedOut());
        require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero());
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
        require(block.timestamp >= offer.start, OfferNotStarted());
        require(block.timestamp <= offer.expiry, OfferExpired());
        require(offer.maker != taker, SelfTake());
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L367-380)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }

        (address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
        Position storage buyerPos = position[id][buyer];
        Position storage sellerPos = position[id][seller];

        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);
```

**File:** src/Midnight.sol (L408-418)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L420-456)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
        address receiver = offer.buy ? receiverIfTakerIsSeller : offer.receiverIfMakerIsSeller;

        emit EventsLib.Take(
            msg.sender,
            id,
            units,
            taker,
            offer.maker,
            offer.buy,
            offer.group,
            buyerAssets,
            sellerAssets,
            newConsumed,
            buyerPendingFeeIncrease,
            sellerPendingFeeDecrease,
            buyerCreditIncrease,
            sellerCreditDecrease,
            receiver,
            payer
        );

        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/periphery/MidnightBundles.sol (L79-86)
```text
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L33-46)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (Signature memory sig, bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (Signature, bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(!isRootCanceled[offer.maker][root], RootCanceled());
        bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
        address _signer = ecrecover(digest, sig.v, sig.r, sig.s);
        require(_signer != address(0), InvalidSignature());
        require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
        return CALLBACK_SUCCESS;
    }
```
