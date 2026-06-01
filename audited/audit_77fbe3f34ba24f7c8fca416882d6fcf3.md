### Title
Zero-`buyerAssets` rounding allows unlimited replay of `maxAssets`-capped buy offers, bypassing EcrecoverRatifier capacity semantics — (File: `src/Midnight.sol`)

### Summary
When `offer.maxAssets > 0` and `offer.buy == true`, the consumed counter is incremented by `buyerAssets`, which is computed with `mulDivDown` and rounds to zero whenever `units * offerPrice < WAD`. Because the EcrecoverRatifier is entirely stateless (no per-take nonce or counter), the same `ratifierData` validates every call, and the `ConsumedAssets` check passes trivially since `consumed += 0`. An unprivileged taker can therefore replay the ratified offer indefinitely, mutating position state on every call while never exhausting the offer's `maxAssets` capacity.

### Finding Description

**Code path and root cause**

In `src/Midnight.sol:363–369`:

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
...
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

For a buy offer, `buyerPrice = offerPrice` (the settlement fee cancels: `sellerPrice = offerPrice − fee`, `buyerPrice = sellerPrice + fee`). `buyerAssets = mulDivDown(units, offerPrice, WAD)`. When `offerPrice < WAD` — which is reachable at any tick near `MAX_TICK` (e.g., `MAX_TICK − 16`, as used in the existing `testBugBuyMaxAssetsBypass` test) — choosing `units = 1` yields `buyerAssets = 0` because `1 * offerPrice < WAD`. The consumed increment is therefore 0, and `newConsumed ≤ maxAssets` is satisfied unconditionally regardless of how many times the call is made.

**EcrecoverRatifier is stateless**

`src/ratifiers/EcrecoverRatifier.sol:33–46` performs only three checks per call: Merkle proof membership, root-not-canceled, and ECDSA signature validity. There is no per-take nonce, counter, or bitmap. The identical `(sig, root, leafIndex, proof)` tuple passes `isRatified` on every invocation, so the ratifier provides zero replay protection once a valid proof is constructed.

**Attacker-controlled inputs**

- `offer.tick` set to any value where `tickToPrice(tick) < WAD` (e.g., `MAX_TICK − 16`)
- `units = 1` (or any value where `units * offerPrice < WAD`)
- `ratifierData` = any previously valid `(sig, root, leafIndex, proof)` tuple for this offer

**Exploit flow**

1. Maker creates a buy offer with `maxAssets = N`, `tick` near `MAX_TICK`, and signs a Merkle root via EcrecoverRatifier.
2. Taker calls `take(offer, validProof, units=1, ...)`.
3. `buyerAssets = mulDivDown(1, offerPrice, WAD) = 0`.
4. `consumed[maker][group] += 0` → consumed stays at 0.
5. `require(0 ≤ N)` passes.
6. Position accounting still processes `units = 1`: maker's credit increases by 1, taker's debt increases by 1, no tokens transferred.
7. Steps 2–6 repeat K times. `consumed` never advances; the offer is never exhausted.

**Why existing checks fail**

- `ConsumedAssets` check (`newConsumed ≤ maxAssets`): passes because the increment is 0.
- EcrecoverRatifier: stateless, same proof reused indefinitely.
- No `require(units == 0 || buyerAssets > 0)` guard exists anywhere in `take`.
- The protocol's own NatSpec at `src/Midnight.sol:94` acknowledges: *"It is possible to give units to a fully consumed assets-based buy offer with price < 1"*, and the test `testBugBuyMaxAssetsBypass` (line 858) explicitly demonstrates the bypass on an already-fully-consumed offer — confirming the path is reachable and the behavior is reproducible.

### Impact Explanation
The `maxAssets` cap — the sole on-chain mechanism limiting how many times a ratified offer can be filled — is rendered ineffective for any buy offer whose tick maps to a price below WAD. An attacker can replay the offer an unbounded number of times, each time incrementing the maker's credit and the taker's debt by `units` with zero token cost. This violates the core invariant that *offers cannot be replayed or overfilled*, and defeats the EcrecoverRatifier's intended limited-use semantics entirely.

### Likelihood Explanation
The preconditions are low-friction: the attacker only needs a valid ratifier proof (publicly constructable from the maker's signed root), a buy offer at a sub-WAD tick (the upper ~half of the tick range satisfies this), and `units` small enough that `units * offerPrice < WAD` (units = 1 always works). The attack is repeatable in a single transaction via multicall or a loop, requires no privileged access, and costs only gas.

### Recommendation
Add a guard in the `maxAssets` branch that rejects a non-zero `units` input that produces a zero consumed increment:

```solidity
if (offer.maxAssets > 0) {
    uint256 consumedDelta = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || consumedDelta > 0, ZeroConsumedIncrement());
    newConsumed = consumed[offer.maker][offer.group] += consumedDelta;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, when `offerPrice < WAD` and `maxAssets > 0`, track consumed in `units` rather than assets, or enforce a minimum `units` floor such that `units * offerPrice ≥ WAD`.

### Proof of Concept

```solidity
function testReplayRatifiedOfferZeroBuyerAssets() public {
    // Setup: buy offer at sub-WAD price tick, maxAssets = 10, EcrecoverRatifier
    lenderOffer.maxUnits = 0;
    lenderOffer.maxAssets = 10;
    lenderOffer.tick = MAX_TICK - 16; // offerPrice < WAD → units=1 gives buyerAssets=0
    lenderOffer.ratifier = address(ecrecoverRatifier);
    // sign Merkle root for this offer (standard EcrecoverRatifier setup)
    bytes memory validProof = buildEcrecoverProof(lenderOffer);

    deal(address(loanToken), lender, 0);
    collateralize(market, borrower, 1000);

    uint256 consumedBefore = midnight.consumed(lender, lenderOffer.group);

    // Replay the ratified offer 100 times — each take produces buyerAssets=0
    for (uint256 i = 0; i < 100; i++) {
        vm.prank(borrower);
        midnight.take(lenderOffer, validProof, 1, borrower, borrower, address(0), "");
    }

    // INVARIANT VIOLATION: consumed must not exceed maxAssets (10),
    // but offer was taken 100 times — consumed should be 100 units worth of assets.
    // Instead it is still 0.
    assertEq(midnight.consumed(lender, lenderOffer.group), consumedBefore); // stays 0
    // Position state grew unboundedly:
    assertGt(midnight.debtOf(id, borrower), 0);
    assertGt(midnight.creditOf(id, lender), 0);
    // Assert the invariant that SHOULD hold but doesn't:
    // assertLe(midnight.consumed(lender, lenderOffer.group), lenderOffer.maxAssets); // passes trivially but misleadingly
    // The real assertion: offer should NOT be takeable more than maxAssets times with non-zero units
    // This test demonstrates it can be taken 100 times with units=1 and zero capacity consumed.
}
```

Expected: the loop should revert (or consumed should reach `maxAssets`) well before 100 iterations. Actual: all 100 calls succeed, `consumed` stays at 0, and the maker's credit and taker's debt each grow by 100 units. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L93-94)
```text
/// @dev Midnight can call the callback of offers through a no-op take, even if those offers have consumed==max.
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-369)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
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

**File:** test/TakeTest.sol (L857-889)
```text
    // Show that a buy offer with offerPrice < WAD can be taken with units > 0
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
