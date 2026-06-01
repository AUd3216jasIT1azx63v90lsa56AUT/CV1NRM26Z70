### Title
`mulDivDown` rounding in `buyerAssets` allows units credit beyond `maxAssets` cap on buy offers - (`src/Midnight.sol`)

### Summary

For buy offers with `maxAssets > 0` and `buyerPrice < WAD`, the consumed tracking increments by `buyerAssets = units.mulDivDown(buyerPrice, WAD)`, which rounds down. Because the rounding is in the taker's favor, an attacker can supply a `units` value such that `buyerAssets` rounds down to exactly `maxAssets` (or even to zero when the offer is already fully consumed), while the buyer's position still receives the full `units` credit. The protocol's own NatSpec and a test named `testBugBuyMaxAssetsBypass` explicitly acknowledge this as a known bug.

### Finding Description

**Exact code path:**

In `take()`, for a buy offer (`offer.buy == true`):

```
buyerAssets = units.mulDivDown(buyerPrice, WAD)   // rounds DOWN
consumed[maker][group] += buyerAssets
require(newConsumed <= offer.maxAssets)            // check on rounded-down value
...
buyerPos.credit += buyerCreditIncrease             // credit granted for full `units`
``` [1](#0-0) [2](#0-1) 

Because `mulDivDown` truncates, `buyerAssets = floor(units * buyerPrice / WAD)`. When `buyerPrice < WAD`, there exist values of `units` where `floor(units * buyerPrice / WAD) < units * buyerPrice / WAD`, meaning the buyer receives more units of credit than `maxAssets / buyerPrice * WAD` would permit.

**Two concrete attack shapes:**

1. **Rounding-at-boundary (question's scenario):** `consumed` starts at 0. Attacker picks `units = ceil(maxAssets * WAD / buyerPrice)`. Then `buyerAssets = floor(units * buyerPrice / WAD) = maxAssets` exactly. The check `newConsumed <= maxAssets` passes. The buyer receives `units = ceil(maxAssets * WAD / buyerPrice)` credit — one unit more than `floor(maxAssets * WAD / buyerPrice)`.

2. **Zero-asset bypass (test's scenario, more severe):** `consumed` is already at `maxAssets`. Attacker calls `take(units=1, ...)` with a tick where `buyerPrice < WAD` and `1 * buyerPrice < WAD`. Then `buyerAssets = 0`, `newConsumed = maxAssets + 0 = maxAssets`, check passes, and the buyer gains 1 unit of credit paying zero assets.

The protocol's own NatSpec at line 94 explicitly documents this:

> "It is possible to give units to a fully consumed assets-based buy offer with price < 1." [3](#0-2) 

**Why existing checks fail:** The `ConsumedAssets` check operates on the already-rounded-down `buyerAssets`, not on the actual units being credited. There is no check that `units <= maxAssets * WAD / buyerPrice`. The `reduceOnly` flag is unrelated. No other guard prevents this.

**Attacker inputs:** `offer.buy = true`, `offer.maxAssets > 0`, `offer.tick` set such that `buyerPrice < WAD`, `units` chosen to make `mulDivDown` round down to `<= maxAssets`.

**Authorization:** The taker (attacker) needs no special authorization to call `take()` — they just need to be `msg.sender` or authorized by the taker address they pass. To subsequently call `withdraw()` on behalf of the maker/buyer, the attacker uses `EcrecoverAuthorizer.setIsAuthorized()` to obtain `isAuthorized[maker][attacker] = true`, then calls `withdraw(market, units, maker, attacker)`. [4](#0-3) [5](#0-4) 

### Impact Explanation

The buyer (maker) accumulates credit (`buyerPos.credit`) in excess of what `maxAssets` was intended to cap. Once the seller repays their matching debt, `withdrawable` increases by `units`, and the buyer can call `withdraw()` to redeem the extra unit(s) as loan tokens. In the zero-asset bypass case, the buyer gains credit paying literally zero tokens. The `maxAssets` invariant — that a maker's total asset exposure is bounded by `offer.maxAssets` — is broken. [6](#0-5) 

### Likelihood Explanation

**Preconditions:**
- `offer.buy == true` and `offer.maxAssets > 0` (assets-capped buy offer)
- `buyerPrice < WAD` (tick set below `MAX_TICK`, which is the common case for discounted lending)
- Attacker is the taker (seller), which requires no special privilege — any address can be a taker
- For the `withdraw()` step, attacker needs `isAuthorized[maker][attacker]`, obtainable via `EcrecoverAuthorizer` if the maker signed an authorization, or the attacker IS the maker's authorized operator

**Feasibility:** The zero-asset bypass requires only that `units * buyerPrice < WAD`, i.e., `units < WAD / buyerPrice`. For a typical discount price (e.g., `buyerPrice = 0.95e18`), this means `units < ~1.05`, so `units = 1` works. This is trivially achievable. The rounding-at-boundary variant works for any `maxAssets` value where `maxAssets * WAD` is not divisible by `buyerPrice`.

**Repeatability:** The bypass can be repeated indefinitely on a fully-consumed offer as long as `buyerPrice < WAD`, since each call adds 0 to `consumed` while granting 1 unit of credit.

### Recommendation

Track consumed in **units** rather than assets for buy offers, or add an explicit check that the units granted do not exceed `(offer.maxAssets - consumed_before) * WAD / buyerPrice`. Concretely, after computing `buyerAssets`, enforce:

```solidity
require(units <= (offer.maxAssets - consumed[offer.maker][offer.group]) * WAD / buyerPrice, ...);
```

Or, equivalently, cap `units` to `mulDivDown(offer.maxAssets - consumed_before, WAD, buyerPrice)` before proceeding, reverting if the caller supplied more. [7](#0-6) 

### Proof of Concept

The existing test `testBugBuyMaxAssetsBypass` in `test/TakeTest.sol` already proves the zero-asset bypass variant: [8](#0-7) 

It sets `maxAssets = 1`, pre-consumes the offer to `maxAssets`, then calls `take(units=1, ...)` with `tick = MAX_TICK - 16` (so `buyerPrice < WAD`). Assertions confirm:
- `buyerAssets == 0` (no tokens paid)
- `consumed == maxAssets` (cap not incremented)
- `creditOf(lender) > lenderCreditBefore` (free credit granted)
- `debtOf(borrower) > borrowerDebtBefore` (matching debt created)

**Additional fuzz test for the rounding-at-boundary variant:**

```solidity
function testFuzz_MaxAssetsBuyRoundingBypass(uint256 maxAssets, uint256 buyerPrice) public {
    // buyerPrice < WAD, maxAssets > 0
    buyerPrice = bound(buyerPrice, 1, WAD - 1);
    maxAssets = bound(maxAssets, 1, type(uint64).max);

    // units chosen so floor(units * buyerPrice / WAD) == maxAssets
    uint256 units = maxAssets * WAD / buyerPrice + 1; // ceil, may round to maxAssets
    uint256 buyerAssets = units * buyerPrice / WAD;   // off-chain preview
    vm.assume(buyerAssets == maxAssets);              // only run when rounding hits

    // set up offer with matching tick and maxAssets
    // ... (setup omitted for brevity)
    (uint256 gotBuyerAssets,) = take(units, borrower, lenderOffer);

    assertEq(gotBuyerAssets, maxAssets);
    assertEq(midnight.consumed(lender, lenderOffer.group), maxAssets);
    // buyer got `units` credit, but maxAssets / buyerPrice * WAD < units
    assertGt(midnight.creditOf(id, lender), maxAssets * WAD / buyerPrice);
}
```

Expected: test passes (no revert), demonstrating the buyer received more units than `maxAssets` strictly permits.

### Citations

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L363-373)
```text
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);

        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L408-410)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
```

**File:** src/Midnight.sol (L481-499)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-47)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );

        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );

        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
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
