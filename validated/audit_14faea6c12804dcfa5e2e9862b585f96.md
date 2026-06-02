All five code references check out against the actual source. Let me confirm each:

- **Line 391**: `require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity())` — `<=` confirmed. [1](#0-0) 
- **Line 359**: `uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp)` — evaluates to `0` at equality, confirmed. [2](#0-1) 
- **Line 414**: `sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease)` — debt write confirmed. [3](#0-2) 
- **EcrecoverRatifier.isRatified** (lines 33–46): only validates Merkle proof, root cancellation, and ECDSA — no maturity check, confirmed. [4](#0-3) 
- **Line 622**: `postMaturityMode ? block.timestamp > market.maturity : originalDebt > maxDebt` — strict `>` confirmed. [5](#0-4) 

All claims are accurate. The exploit path is coherent and reachable by an unprivileged taker. The impact (forced creation of immediately-overdue debt enabling unconditional post-maturity liquidation in the next block) is concrete and in-scope.

---

Audit Report

## Title
Off-by-One in Maturity Guard Allows Debt Creation at Exact Maturity Timestamp - (File: src/Midnight.sol)

## Summary
The maturity guard in `take()` uses `block.timestamp <= offer.market.maturity`, permitting `sellerDebtIncrease > 0` when `block.timestamp == offer.market.maturity`. Debt created at exactly maturity has `timeToMaturity = 0`, giving the borrower at most one block (~12 seconds on Ethereum) to repay before their position enters unconditional post-maturity liquidation mode. The `EcrecoverRatifier` performs no compensating maturity check.

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
Change the maturity guard at line 391 from `<=` to `<`:
```solidity
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```
This ensures debt cannot be created at or after maturity, consistent with the intent of the guard and the strict `>` used in the liquidation check at line 622.

## Proof of Concept
1. Deploy a market with maturity `T`.
2. Maker signs a sell offer with `offer.expiry = T` and sufficient collateral posted.
3. Warp block timestamp to exactly `T` (e.g., `vm.warp(T)` in Foundry).
4. Taker calls `take()` with `units > sellerPos.credit` so `sellerDebtIncrease > 0`.
5. Assert the call succeeds and `sellerPos.debt > 0`.
6. Warp one second forward (`vm.warp(T + 1)`).
7. Call `liquidate(..., postMaturityMode = true, ...)` on the seller's position.
8. Assert the liquidation succeeds unconditionally (regardless of collateral ratio).

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
