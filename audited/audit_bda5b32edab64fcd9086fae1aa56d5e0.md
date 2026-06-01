### Title
Debt Increase Permitted at Exact Maturity Timestamp via `<=` Boundary in `take()` - (File: src/Midnight.sol)

### Summary
The maturity guard in `take()` uses `block.timestamp <= offer.market.maturity`, which permits `sellerDebtIncrease > 0` when `block.timestamp == offer.market.maturity`. The protocol's own core invariant explicitly states "maturity boundary must not allow debt increase by rounding, **timestamp equality**, or callback sequencing." The `EcrecoverRatifier.isRatified` performs no maturity check, so it cannot compensate for this off-by-one.

### Finding Description

**Code path:**

`take()` at `src/Midnight.sol:391`:
```solidity
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

When `block.timestamp == offer.market.maturity`, the left side of the `||` evaluates to `true`, so the require passes regardless of `sellerDebtIncrease`. Debt is then written at line 414:
```solidity
sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
``` [2](#0-1) 

**Ratifier path:**

`EcrecoverRatifier.isRatified` only validates the Merkle proof, root cancellation, and ECDSA signature. It contains zero check on `block.timestamp` vs `offer.market.maturity`: [3](#0-2) 

There is no compensating guard anywhere in the ratifier.

**Attacker-controlled inputs:**
- Taker (unprivileged) calls `take()` with a valid signed sell offer where `offer.market.maturity = T`
- Taker warps or waits until `block.timestamp = T` exactly
- `units > sellerPos.credit` so `sellerDebtIncrease > 0`

**Exploit flow:**
1. Maker signs a sell offer for market with maturity `T`; offer is valid (not expired, not canceled, signature correct)
2. At `block.timestamp = T`, taker calls `take(offer, ratifierData, units, taker, ...)`
3. `isRatified` passes — only checks signature/Merkle proof
4. `timeToMaturity = zeroFloorSub(T, T) = 0`
5. `require(T <= T || sellerDebtIncrease == 0)` → `true || ...` → passes
6. `sellerPos.debt += sellerDebtIncrease` executes — debt created at exactly maturity

**Why existing checks fail:**

The only guard is the `<=` comparison. The protocol's own stated invariant in `live_context.json` line 221 explicitly names "timestamp equality" as a forbidden case:
```
"maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing"
``` [4](#0-3) 

The existing tests for post-maturity debt prevention all use `maturity + 1`, never `maturity` exactly: [5](#0-4) [6](#0-5) 

This leaves the `block.timestamp == maturity` case untested and unguarded.

### Impact Explanation

Debt is created at exactly maturity with `timeToMaturity = 0`. The borrower has zero seconds to repay before the debt is overdue. In the next block (`block.timestamp > maturity`), the position enters post-maturity liquidation mode (`block.timestamp > market.maturity` at line 622), making the debt immediately liquidatable regardless of collateral health. This constitutes undercollateralized/overdue debt creation at the maturity boundary, directly violating the core invariant `"debt must not increase after maturity"` and the explicit maturity boundary invariant. [7](#0-6) 

### Likelihood Explanation

Any unprivileged taker holding a valid signed sell offer can trigger this by calling `take()` in the exact block where `block.timestamp == offer.market.maturity`. This is a single-transaction, single-actor exploit requiring no special privileges. Ethereum block timestamps are miner/validator-influenceable within ~12 seconds, making exact-timestamp targeting feasible. The condition is repeatable for any market whose maturity timestamp coincides with a block timestamp.

### Recommendation

Change the maturity guard from `<=` to `<` (strictly less than):

```solidity
// Before (buggy):
require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());

// After (correct):
require(block.timestamp < offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
``` [1](#0-0) 

This aligns the code with the explicit invariant that "timestamp equality" must not permit debt increase.

### Proof of Concept

```solidity
// Foundry unit test
function testDebtIncreaseAtExactMaturity() public {
    uint256 units = 100;
    // Warp to EXACTLY maturity (not maturity + 1)
    vm.warp(market.maturity);
    borrowerOffer.expiry = market.maturity;
    borrowerOffer.maxUnits = units;
    deal(address(loanToken), lender, units);
    collateralize(market, borrower, units);

    uint256 debtBefore = midnight.debtOf(id, borrower);

    // This should revert with CannotIncreaseDebtPostMaturity but does NOT
    take(units, lender, borrowerOffer);

    uint256 debtAfter = midnight.debtOf(id, borrower);
    // Assertion: debt must not increase at maturity
    assertEq(debtAfter, debtBefore, "debt must not increase at maturity");
}

// Foundry fuzz test
function testFuzzDebtNotIncreaseAtOrAfterMaturity(uint256 warpDelta) public {
    // fuzz: warpDelta in {0, 1, 2} maps to {maturity-1, maturity, maturity+1}
    warpDelta = bound(warpDelta, 0, 2);
    uint256 ts = market.maturity - 1 + warpDelta;
    vm.warp(ts);
    borrowerOffer.expiry = ts;
    borrowerOffer.maxUnits = 100;
    deal(address(loanToken), lender, 100);
    collateralize(market, borrower, 100);

    if (ts >= market.maturity) {
        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
    }
    take(100, lender, borrowerOffer);

    if (ts >= market.maturity) {
        assertEq(midnight.debtOf(id, borrower), 0, "no debt at or after maturity");
    }
}
```

**Expected assertions:** Both tests fail against the current code when `warpDelta == 1` (i.e., `block.timestamp == maturity`), confirming the bug. After applying the `<` fix, both pass.

### Citations

**File:** src/Midnight.sol (L391-391)
```text
        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
```

**File:** src/Midnight.sol (L414-414)
```text
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
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

**File:** live_context.json (L196-198)
```json
      "selling units must reduce existing credit before increasing debt",
      "debt must not increase after maturity"
    ],
```

**File:** live_context.json (L219-222)
```json
    "maturity": [
      "pre-maturity and post-maturity paths must not be composable to bypass debt repayment",
      "maturity boundary must not allow debt increase by rounding, timestamp equality, or callback sequencing",
      "post-maturity unwinding must not mint free credit or erase debt incorrectly"
```

**File:** test/TakeTest.sol (L315-326)
```text
    function testBuy1PostMaturity() public {
        uint256 units = 100;
        uint256 timestamp = market.maturity + 1;
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = units;
        deal(address(loanToken), lender, units);
        collateralize(market, borrower, units);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(units, lender, borrowerOffer);
    }
```

**File:** test/TakeTest.sol (L666-677)
```text
    function testBuyPastMaturity(uint256 timestamp) public {
        timestamp = bound(timestamp, market.maturity + 1, type(uint32).max);
        vm.warp(timestamp);
        borrowerOffer.expiry = timestamp;
        borrowerOffer.maxUnits = 100;
        borrowerOffer.tick = MAX_TICK;
        deal(address(loanToken), lender, 100);
        collateralize(market, borrower, 100);

        vm.expectRevert(IMidnight.CannotIncreaseDebtPostMaturity.selector);
        take(100, lender, borrowerOffer);
    }
```
