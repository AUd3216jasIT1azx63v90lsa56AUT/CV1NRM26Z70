Based on my direct code review, all code references in the submitted report are accurate. Here is my validation:

**Line 391 confirmed** — `<=` guard: [1](#0-0) 

**Line 359 confirmed** — `timeToMaturity = zeroFloorSub(maturity, block.timestamp)` → `0` at equality: [2](#0-1) 

**Line 414 confirmed** — debt write executes: [3](#0-2) 

**EcrecoverRatifier.isRatified confirmed** — no maturity check, only Merkle proof, root cancellation, ECDSA: [4](#0-3) 

**Line 622 confirmed** — post-maturity liquidation uses strict `>`: [5](#0-4) 

---

Audit Report

## Title
Off-by-One in Maturity Guard Allows Debt Creation at Exact Maturity Timestamp - (File: src/Midnight.sol)

## Summary
The maturity guard in `take()` uses `block.timestamp <= offer.market.maturity`, permitting `sellerDebtIncrease > 0` when `block.timestamp == offer.market.maturity`. Debt created at exactly maturity has `timeToMaturity = 0`, giving the borrower at most one block to repay before their position enters unconditional post-maturity liquidation mode. The `EcrecoverRatifier` performs no compensating maturity check.

## Finding Description
At `src/Midnight.sol:391`:
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
When `block.timestamp == offer.market.maturity`, the left operand is `true`, so the `require` passes unconditionally regardless of `sellerDebtIncrease`. Debt is then written at line 414:
```solidity
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```
`timeToMaturity` is computed at line 359 as `zeroFloorSub(offer.market.maturity, block.timestamp)`, which evaluates to `0` at exact maturity. The continuous fee accrual at line 386 (`buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD)`) also evaluates to `0`, meaning the buyer pays no fee for the position.

`EcrecoverRatifier.isRatified` (lines 33–46) only validates the Merkle proof, root cancellation status, and ECDSA signature — it contains no check on `block.timestamp` vs `offer.market.maturity`.

The liquidation guard at line 622 uses strict `>`:
```solidity
postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt
```
This means at `block.timestamp == maturity`, post-maturity liquidation is not yet enabled (same block). However, in the very next block where `block.timestamp > maturity`, any unprivileged liquidator can call `liquidate(..., postMaturityMode = true, ...)` and the position is unconditionally liquidatable regardless of collateral health.

**Exploit flow:**
1. Maker signs a sell offer for a market with maturity `T`, with `offer.expiry >= T`.
2. Taker waits until `block.timestamp == T` and calls `take(offer, ratifierData, units, ...)` where `units > sellerPos.credit` so `sellerDebtIncrease > 0`.
3. `isRatified` passes (signature/Merkle only, no maturity check).
4. `timeToMaturity = zeroFloorSub(T, T) = 0`.
5. `require(T <= T || sellerDebtIncrease == 0)` → `true` → passes.
6. `sellerPos.debt += sellerDebtIncrease` executes — debt created at exactly maturity.
7. In the next block, `block.timestamp > market.maturity` becomes true, enabling post-maturity liquidation mode (line 622), making the position immediately liquidatable regardless of collateral health.

## Impact Explanation
Debt is created at exactly maturity with `timeToMaturity = 0`. The seller-borrower has at most one block (~12 seconds on Ethereum) to repay before their position becomes unconditionally liquidatable in post-maturity mode. This constitutes forced creation of immediately-overdue debt, enabling griefing or predatory liquidation of any maker who has a live sell offer with `expiry >= maturity`. The attacker (taker) can front-run the repayment transaction or simply liquidate in the next block, seizing collateral at post-maturity terms regardless of the position's collateral health.

## Likelihood Explanation
Any unprivileged taker holding a valid signed sell offer with `offer.expiry >= maturity` can trigger this in the exact block where `block.timestamp == offer.market.maturity`. No special privileges are required. Ethereum block timestamps are validator-influenceable within ~12 seconds, making exact-timestamp targeting feasible. The condition is repeatable for any market whose maturity timestamp coincides with a block timestamp, which is the common case since maturities are typically set to round timestamps (e.g., end of day/week/month).

## Recommendation
Change the maturity guard in `take()` from `<=` to `<`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This ensures that at exactly `block.timestamp == maturity`, debt creation is blocked, making the guard consistent with the post-maturity liquidation guard (`block.timestamp > market.maturity`) and eliminating the zero-`timeToMaturity` debt creation edge case.

## Proof of Concept
```solidity
// Minimal fork test outline:
// 1. Deploy market with maturity = block.timestamp + 1 (or warp to maturity - 1).
// 2. Maker signs sell offer with expiry = maturity.
// 3. Warp to block.timestamp == maturity.
// 4. Taker calls take() with units > sellerPos.credit.
// 5. Assert sellerPos.debt > 0 and timeToMaturity == 0.
// 6. Warp one block forward (block.timestamp = maturity + 1).
// 7. Liquidator calls liquidate(..., postMaturityMode = true, ...).
// 8. Assert liquidation succeeds (NotLiquidatable not reverted).
// Expected: Steps 4 and 7 both succeed, demonstrating debt creation at maturity
// followed by immediate post-maturity liquidation.
```

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
