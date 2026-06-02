Audit Report

## Title
Buy-offer `mulDivDown` rounding to zero bypasses `maxAssets` cap and mints unbacked credit — (`src/Midnight.sol`)

## Summary
When `offer.buy = true` and `buyerPrice < WAD`, calling `take` with `units = 1` causes `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The `consumed` counter is incremented by zero, so the `maxAssets` cap is never exhausted regardless of how many times the offer is taken. Each call still increments the maker's credit and the taker's debt by 1 unit while transferring zero loan tokens, minting unbacked credit and debt without limit.

## Finding Description
**Root cause — `src/Midnight.sol` line 363:**
```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```
With `units = 1` and any `buyerPrice < WAD` (any tick below `MAX_TICK = 5820`), `1 * buyerPrice < WAD`, so `mulDivDown` returns 0.

**Cap bypass — line 368:**
```solidity
newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
require(newConsumed <= offer.maxAssets, ConsumedAssets());
```
`buyerAssets = 0` means `newConsumed` never increases. The cap check passes unconditionally, even after `maxAssets` would otherwise be exhausted.

**Unbacked credit/debt — lines 382, 410, 414:**
```solidity
uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt); // = 1
buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);                // maker credit +1
sellerPos.debt   += UtilsLib.toUint128(sellerDebtIncrease);                // taker debt +1
```
These are computed from `units`, not from `buyerAssets`, so they increase by 1 regardless.

**Zero transfer — lines 455–456:**
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets); // 0
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);                    // 0
```
No loan tokens move.

**EcrecoverRatifier (`src/ratifiers/EcrecoverRatifier.sol` line 37):** Only verifies the Merkle-proof signature over the offer struct. It does not inspect `units`, `buyerAssets`, or the consumed counter, so it passes for any validly-signed offer.

**Existing checks that fail:**
- `require(newConsumed <= offer.maxAssets)` — passes because `newConsumed` is unchanged.
- `require(offer.maker != taker)` — trivially satisfied with two addresses.
- `reduceOnly` — not set in the attack offer.
- Health check on seller — taker can pre-supply collateral.

**Confirmed by test:** `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` explicitly documents and reproduces this state.

## Impact Explanation
The maker (buyer/lender) accumulates unbounded credit without ever depositing loan tokens. The taker accumulates debt without receiving loan tokens. `totalUnits` grows without a matching increase in the protocol's loan-token balance. When the taker eventually repays, the maker withdraws tokens that were never deposited, directly draining other lenders. This breaks the core invariant that every credit increase must correspond to a valid asset transfer, constituting a direct loss of funds for honest lenders — a critical severity impact.

## Likelihood Explanation
Preconditions are trivially met: any buy offer with `tick < MAX_TICK` (i.e., `buyerPrice < WAD`, which is every tick except the maximum) and `maxAssets > 0` is vulnerable. The maker and taker can be two addresses controlled by the same party. The attack is repeatable in a single transaction via `multicall`. No oracle manipulation, admin access, or special token behavior is required.

## Recommendation
Replace the `mulDivDown` rounding with `mulDivUp` for buy-offer asset computation, or add an explicit check that `buyerAssets > 0` (or `sellerAssets > 0`) before proceeding. The simplest targeted fix is:

```solidity
// Line 363 — use mulDivUp for buy offers to prevent rounding to zero
uint256 buyerAssets = units.mulDivUp(buyerPrice, WAD);
uint256 sellerAssets = units.mulDivDown(sellerPrice, WAD);
```

Alternatively, add `require(buyerAssets > 0 || units == 0)` after line 363 to reject zero-asset takes when `units > 0`.

## Proof of Concept
The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` reproduces the issue. Minimal manual steps:
1. Deploy Midnight; create a market with `tickSpacing = 4`.
2. Maker signs a buy offer with `tick = 0` (so `buyerPrice = tickToPrice(0) << WAD`), `maxAssets = 100`, `maxUnits = 0`.
3. Taker calls `take(..., units=1, ...)` repeatedly.
4. Assert: `consumed[maker][group]` remains 0 after each call; maker's credit and taker's debt each increase by 1 per call; zero loan tokens transferred; call succeeds indefinitely past `maxAssets = 100`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** src/Midnight.sol (L363-363)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L382-384)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
```

**File:** src/Midnight.sol (L408-414)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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

**File:** src/libraries/TickLib.sol (L6-6)
```text
uint256 constant MAX_TICK = 5820;
```

**File:** src/libraries/ConstantsLib.sol (L8-8)
```text
uint256 constant WAD = 1e18;
```
