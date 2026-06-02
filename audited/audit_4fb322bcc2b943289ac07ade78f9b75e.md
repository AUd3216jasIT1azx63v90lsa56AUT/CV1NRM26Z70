Based on my investigation of the actual code, I can validate this claim.

**Code verification:**

`src/Midnight.sol:391` confirms the `<=` guard: [1](#0-0) 

`src/Midnight.sol:359` confirms `timeToMaturity = zeroFloorSub(maturity, block.timestamp)` → `0` when equal: [2](#0-1) 

`src/Midnight.sol:414` confirms debt write executes: [3](#0-2) 

`EcrecoverRatifier.isRatified` contains no maturity check — only Merkle proof, root cancellation, and ECDSA signature: [4](#0-3) 

Post-maturity liquidation uses strict `>`, so at exactly `block.timestamp == maturity` the position is NOT yet liquidatable — but becomes so in the very next block: [5](#0-4) 

`live_context.json` confirmed to contain matches for "timestamp equality" and "maturity boundary" invariant language.

---

Audit Report

## Title
Off-by-One in Maturity Guard Allows Debt Creation at Exact Maturity Timestamp - (File: src/Midnight.sol)

## Summary
The maturity guard in `take()` uses `block.timestamp <= offer.market.maturity`, permitting `sellerDebtIncrease > 0` when `block.timestamp == offer.market.maturity`. Debt created at exactly maturity has `timeToMaturity = 0`, giving the borrower zero seconds to repay before the position enters post-maturity liquidation mode in the next block. The `EcrecoverRatifier` performs no compensating maturity check.

## Finding Description
At `src/Midnight.sol:391`:
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
When `block.timestamp == offer.market.maturity`, the left operand is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`. Debt is then written at line 414:
```solidity
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```
`timeToMaturity` is computed at line 359 as `zeroFloorSub(offer.market.maturity, block.timestamp)`, which evaluates to `0` at exact maturity. The `EcrecoverRatifier.isRatified` (lines 33–46) only validates the Merkle proof, root cancellation status, and ECDSA signature — it contains no check on `block.timestamp` vs `offer.market.maturity`.

**Exploit flow:**
1. Maker signs a sell offer for a market with maturity `T`.
2. Taker waits until `block.timestamp == T` and calls `take(offer, ratifierData, units, ...)` where `units > sellerPos.credit` so `sellerDebtIncrease > 0`.
3. `isRatified` passes (signature/Merkle only).
4. `timeToMaturity = zeroFloorSub(T, T) = 0`.
5. `require(T <= T || sellerDebtIncrease == 0)` → `true` → passes.
6. `sellerPos.debt += sellerDebtIncrease` executes — debt created at exactly maturity.
7. In the next block, `block.timestamp > market.maturity` becomes true, enabling post-maturity liquidation mode (line 622), making the position immediately liquidatable regardless of collateral health.

## Impact Explanation
Debt is created at exactly maturity with `timeToMaturity = 0`. The seller-borrower has zero seconds to repay before their position becomes unconditionally liquidatable in post-maturity mode. This constitutes forced creation of immediately-overdue debt, violating the protocol's stated invariant that "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing" (live_context.json). The impact is undercollateralized/overdue debt creation at the maturity boundary, directly enabling griefing or predatory liquidation of any maker who has a live sell offer at market maturity.

## Likelihood Explanation
Any unprivileged taker holding a valid signed sell offer can trigger this in the exact block where `block.timestamp == offer.market.maturity`. No special privileges are required. Ethereum block timestamps are validator-influenceable within ~12 seconds, making exact-timestamp targeting feasible. The condition is repeatable for any market whose maturity timestamp coincides with a block timestamp, which is the common case since maturities are typically set to round timestamps.

## Recommendation
Change the maturity guard from `<=` to `<`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This ensures debt cannot be created at or after maturity, consistent with the protocol's stated invariant and the strict `>` used in the post-maturity liquidation check at line 622.

## Proof of Concept
```solidity
// In a fork/unit test:
uint256 maturity = block.timestamp + 1 days;
// Create market with this maturity, maker signs sell offer
// vm.warp(maturity); // warp to exact maturity
// taker calls take() with units > sellerPos.credit
// Assert: sellerPos.debt > 0 (debt created at maturity)
// vm.warp(maturity + 1);
// Assert: position is liquidatable via postMaturityMode
```
The existing test suite only tests `maturity + 1` for `CannotIncreaseDebtPostMaturity`, leaving the `block.timestamp == maturity` case untested and unguarded.

### Citations

**File:** src/Midnight.sol (L359-359)
```text
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
```

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L620-624)
```text
        require(
            !liquidationLocked(id, borrower)
                && (postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt),
            NotLiquidatable()
        );
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
