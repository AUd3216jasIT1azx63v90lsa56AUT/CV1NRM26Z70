### Title
Maker Offer Spoofing via Credit Withdrawal Before Take Execution — (File: src/Midnight.sol)

### Summary

Midnight's off-chain offer system does not escrow or lock the maker's credit units when a sell offer is created. A maker can sign a valid sell offer via `EcrecoverRatifier`, broadcast it to off-chain order books, then frontrun any taker's `take()` call by withdrawing their credit via `withdraw()`. The taker's transaction reverts with `SellerIsLiquidatable()` (or `MakerCreditOrDebtIncreased()` for `reduceOnly` offers), wasting gas and creating phantom liquidity in off-chain markets.

### Finding Description

**Root cause**: The `take()` function in `src/Midnight.sol` validates the maker's signature at call time but reads the maker's on-chain credit position at execution time. No capital is locked when an offer is created.

**Code path**:

In `take()`, signature validation passes regardless of the maker's current position:

```solidity
// src/Midnight.sol:355-356
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
``` [1](#0-0) 

Then the seller's credit is read live from storage:

```solidity
// src/Midnight.sol:383-384
uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
uint256 sellerDebtIncrease = units - sellerCreditDecrease;
``` [2](#0-1) 

If the maker withdrew their credit before this executes, `sellerPos.credit == 0`, so `sellerCreditDecrease = 0` and `sellerDebtIncrease = units`. The final health check then reverts:

```solidity
// src/Midnight.sol:476
require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
``` [3](#0-2) 

For offers with `reduceOnly = true` (the natural setting for a lender exiting a position), the revert is even earlier:

```solidity
// src/Midnight.sol:392-395
require(
    !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
    MakerCreditOrDebtIncreased()
);
``` [4](#0-3) 

The `EcrecoverRatifier` signs the offer struct but has no mechanism to verify the maker's current on-chain position:

```solidity
// src/ratifiers/EcrecoverRatifier.sol:33-45
function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    ...
    require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
    return CALLBACK_SUCCESS;
}
``` [5](#0-4) 

The protocol's own documentation confirms this is by design: "The protocol utilizes an off-chain offer system where makers do not lock capital."

**Exploit flow**:

1. Maker holds 1000 credit units in market M.
2. Maker signs a sell offer (offer.buy = false, reduceOnly = true, units = 1000) via `EcrecoverRatifier` and publishes it to an off-chain order book.
3. Taker sees the offer and submits `take(offer, ratifierData, 1000, taker, ...)`.
4. Maker observes the pending transaction and frontruns with `withdraw(market, 1000, maker, maker)`.
5. Taker's `take()` executes: `sellerPos.credit == 0`, `sellerDebtIncrease = 1000 > 0`, reverts with `MakerCreditOrDebtIncreased()`.
6. Taker loses gas. Maker retains their withdrawn tokens.

### Impact Explanation

- Takers waste gas on transactions that are guaranteed to fail due to maker-controlled frontrunning.
- Off-chain order books (including the protocol's own periphery) display phantom sell-side liquidity that cannot be filled.
- In `MidnightBundles.buyWithUnitsTargetAndWithdrawCollateral`, spoofed offers are silently skipped via `try/catch`, potentially causing `OutOfOffers()` revert after the taker's tokens have been pulled (though they are returned on revert).
- No direct theft of funds occurs; impact is gas griefing and degraded market integrity. [6](#0-5) 

### Likelihood Explanation

- Any maker with a valid `EcrecoverRatifier` authorization can execute this with zero cost beyond gas.
- No privileged access is required.
- The attack is trivially repeatable: the maker can re-sign and re-publish the same offer after withdrawing, creating a persistent phantom liquidity loop.
- Frontrunning is straightforward on any EVM chain with a public mempool.

### Recommendation

1. **Short-term**: Document clearly in the `EcrecoverRatifier` and `SetterRatifier` interfaces that off-chain order books must treat sell offers as non-binding until execution, and should re-validate maker credit on-chain before displaying liquidity.
2. **Medium-term**: Introduce an optional `minSellerCredit` field in the `Offer` struct that `take()` enforces, allowing makers to self-attest a minimum credit floor and causing an early, descriptive revert rather than `SellerIsLiquidatable()`.
3. **Long-term**: Consider an optional escrow path for sell offers (analogous to the Foundation's acknowledged mitigation), where makers can lock credit units into a dedicated slot that is atomically released on take or cancellation.

### Proof of Concept

```
Preconditions:
  - Market M exists with loanToken = USDC
  - Maker has position[M][maker].credit = 1000
  - EcrecoverRatifier deployed, maker authorized it

Step 1: Maker signs Offer{buy=false, maker=maker, reduceOnly=true, maxUnits=1000, ...}
        and publishes signature off-chain.

Step 2: Taker submits:
        midnight.take(offer, ratifierData, 1000, taker, taker, address(0), "")

Step 3: Maker frontruns (higher gas):
        midnight.withdraw(market, 1000, maker, maker)
        → position[M][maker].credit = 0

Step 4: Taker's take() executes:
        sellerPos.credit = 0
        sellerCreditDecrease = min(1000, 0) = 0
        sellerDebtIncrease = 1000 - 0 = 1000
        require(!true || sellerDebtIncrease == 0)  ← REVERTS: MakerCreditOrDebtIncreased()

Result: Taker's tx reverts. Maker holds 1000 USDC equivalent. Gas wasted by taker.
``` [7](#0-6) [8](#0-7)

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

**File:** src/Midnight.sol (L375-395)
```text
        (address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
        Position storage buyerPos = position[id][buyer];
        Position storage sellerPos = position[id][seller];

        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);

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
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L33-45)
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
