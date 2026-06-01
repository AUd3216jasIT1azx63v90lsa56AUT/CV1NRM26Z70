### Title
Buy-offer `mulDivDown` rounding to zero bypasses `maxAssets` cap and mints unbacked credit — (`src/Midnight.sol` / `src/ratifiers/EcrecoverRatifier.sol`)

### Summary

When `offer.buy = true` and `buyerPrice < WAD`, calling `take` with `units = 1` causes `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The `maxAssets` consumed counter is incremented by zero, so a fully-consumed offer cap is never exceeded. Simultaneously, the maker's credit and the taker's debt each increase by 1 unit with zero loan-token transfer, minting credit that has no loan-token backing in the protocol. The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` explicitly documents and reproduces this state.

### Finding Description

**Code path — `src/Midnight.sol` `take`:**

Line 363 computes `buyerAssets` with `mulDivDown` for buy offers: [1](#0-0) 

When `units = 1` and `buyerPrice < WAD` (any tick below `MAX_TICK` with zero or low settlement fee), `1 * buyerPrice < WAD`, so `mulDivDown` returns 0.

Line 368 increments the consumed counter by `buyerAssets` (for buy offers): [2](#0-1) 

Adding 0 means a fully-consumed offer (`consumed == maxAssets`) passes the cap check unchanged. The offer can be taken an unlimited number of additional times.

Lines 382 and 410 then unconditionally credit the buyer (maker) with `units = 1`: [3](#0-2) [4](#0-3) 

Lines 414 and 455–456 give the seller (taker) 1 unit of debt and transfer 0 loan tokens: [5](#0-4) [6](#0-5) 

**`EcrecoverRatifier.isRatified` role:** The ratifier only verifies the Merkle-proof signature over the offer struct. It does not inspect `units`, `buyerAssets`, or the consumed counter, so it passes unconditionally for any validly-signed offer. [7](#0-6) 

**Attacker inputs:**
- `offer.buy = true`, `offer.tick` set to any value where `tickToPrice(tick) < WAD` (any tick below `MAX_TICK` with zero settlement fee, or any tick where `offerPrice < WAD`)
- `offer.maxAssets = N` (any finite cap), `offer.maxUnits = 0`
- `units = 1`
- Taker is any address ≠ maker

**Exploit flow:**
1. Maker (lender) creates and signs a buy offer with low tick (`buyerPrice < WAD`) and `maxAssets = N`.
2. Taker calls `take(..., units=1, ...)`.
3. `buyerAssets = 0`, `sellerAssets = 0`.
4. `consumed` does not increase → cap check passes even if already at `maxAssets`.
5. Maker's `credit += 1`, taker's `debt += 1`, `totalUnits += 1`.
6. Zero loan tokens transferred.
7. Step 2–6 repeatable indefinitely.

**Why existing checks fail:**
- `require(newConsumed <= offer.maxAssets)` — passes because `newConsumed` is unchanged.
- `require(offer.maker != taker)` — requires two addresses, trivially satisfied.
- `reduceOnly` — not set in the attack offer.
- Health check on seller — taker must be collateralized, but the taker can supply collateral before each take.

### Impact Explanation

The maker (buyer) accumulates unbounded credit without ever transferring loan tokens. The taker (seller) accumulates debt without receiving loan tokens. `totalUnits` grows without a matching increase in `withdrawable` or protocol loan-token balance. When the taker eventually repays, the maker withdraws loan tokens that were never deposited, draining tokens from other lenders. This directly breaks the invariant: every credit increase must correspond to a valid asset transfer.

### Likelihood Explanation

Preconditions are easily met: any buy offer with a tick below `MAX_TICK` (i.e., `buyerPrice < WAD`) and `maxAssets > 0` is vulnerable. The maker and taker can be two addresses controlled by the same party. The attack is repeatable in a single transaction via `multicall`. No oracle manipulation, admin access, or special token behavior is required.

### Recommendation

Reject the take when `units > 0` but `buyerAssets == 0` (or `sellerAssets == 0` for sell offers). Add a guard immediately after computing the asset amounts:

```solidity
require(units == 0 || (buyerAssets > 0 && sellerAssets > 0), ZeroAssets());
```

Alternatively, enforce a minimum of 1 wei for `buyerAssets` by using `mulDivUp` for buy offers as well, or require `units >= WAD / buyerPrice` before proceeding.

### Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` is a complete Foundry unit test that reproduces the bug: [8](#0-7) 

Expected assertions (already present in the test):
- `buyerAssets == 0` and `sellerAssets == 0`
- `consumed(lender, group) == maxAssets` (cap not incremented, bypass confirmed)
- `loanToken.balanceOf(lender) == lenderBalBefore` (no tokens paid)
- `creditOf(id, lender) > lenderCreditBefore` (free credit minted)
- `debtOf(id, borrower) > borrowerDebtBefore` (unbacked debt created)
- `totalUnits(id) > totalUnitsBefore` (protocol accounting inflated)

A fuzz extension should vary `tick` over `[1, MAX_TICK-1]` and assert `buyerAssets > 0` whenever `units > 0`, catching all rounding-to-zero cases across the full tick range.

### Citations

**File:** src/Midnight.sol (L363-364)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L367-369)
```text
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
```

**File:** src/Midnight.sol (L382-382)
```text
        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
```

**File:** src/Midnight.sol (L410-410)
```text
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
```

**File:** src/Midnight.sol (L414-414)
```text
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

**File:** test/TakeTest.sol (L858-889)
```text
    function testBugBuyMaxAssetsBypass() public {
        deal(address(loanToken), lender, 0); // lender pays 0
        collateralize(market, borrower, 100);

        lenderOffer.maxUnits = 0;
        lenderOffer.maxAssets = 1;
        lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD

        // Fully consume the offer before the take.
        vm.prank(lender);
        midnight.setConsumed(lenderOffer.group, lenderOffer.maxAssets, lender);

        uint256 lenderCreditBefore = midnight.creditOf(id, lender);
        uint256 borrowerDebtBefore = midnight.debtOf(id, borrower);
        uint256 totalUnitsBefore = midnight.totalUnits(id);
        uint256 lenderBalBefore = loanToken.balanceOf(lender);
        uint256 borrowerBalBefore = loanToken.balanceOf(borrower);

        (uint256 buyerAssets, uint256 sellerAssets) = take(1, borrower, lenderOffer);

        assertEq(buyerAssets, 0);
        assertEq(sellerAssets, 0);

        // Nothing observable to the cap or token balances changed:
        assertEq(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets);
        assertEq(loanToken.balanceOf(lender), lenderBalBefore);
        assertEq(loanToken.balanceOf(borrower), borrowerBalBefore);
        // But position state strictly changed:
        assertGt(midnight.creditOf(id, lender), lenderCreditBefore);
        assertGt(midnight.debtOf(id, borrower), borrowerDebtBefore);
        assertGt(midnight.totalUnits(id), totalUnitsBefore);
    }
```
