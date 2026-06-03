Audit Report

## Title
Buy-offer `mulDivDown` rounding to zero bypasses `maxAssets` cap and mints unbacked credit — (`src/Midnight.sol`)

## Summary
When a buy offer has `buyerPrice < WAD` (any tick below `MAX_TICK = 5820`), calling `take` with `units = 1` causes `buyerAssets = mulDivDown(1, buyerPrice, WAD) = 0`. The `maxAssets` consumed counter is incremented by zero, so the cap check never triggers regardless of how many times the offer is taken. Each call unconditionally increases the maker's credit and the taker's debt by 1 unit while transferring zero loan tokens, minting unbacked credit that can later be withdrawn to drain other lenders' withdrawable pool.

## Finding Description

**Root cause — `src/Midnight.sol` line 363:**

```solidity
uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
```

For `units = 1` and any `buyerPrice < WAD = 1e18`, integer division floors `1 * buyerPrice / 1e18` to 0. `tickToPrice` in `src/libraries/TickLib.sol` confirms that every tick below `MAX_TICK = 5820` produces a price strictly less than WAD. [1](#0-0) [2](#0-1) 

**Cap bypass — lines 367–369:**

```solidity
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Adding 0 leaves `newConsumed` unchanged. An offer already at `maxAssets` passes the check indefinitely. [3](#0-2) 

**Unbacked credit/debt — lines 382, 384, 410, 414:**

`buyerCreditIncrease` is computed as `zeroFloorSub(units, buyerPos.debt)` and `sellerDebtIncrease` as `units - sellerCreditDecrease` — both derived from `units`, not from `buyerAssets`. With `units = 1` and a fresh buyer position (no debt), `buyerCreditIncrease = 1` and `sellerDebtIncrease = 1`. [4](#0-3) [5](#0-4) 

**`totalUnits` grows without `withdrawable` increasing — line 416:**

`totalUnits` is incremented by `buyerCreditIncrease = 1`, but `withdrawable` is never updated in `take`. The `withdraw` function decrements `withdrawable`, so the attacker's unbacked credit can drain the pool funded by legitimate borrower repayments. [6](#0-5) [7](#0-6) 

**Zero token transfer — lines 455–456:**

Both `safeTransferFrom` calls transfer 0 tokens. No loan tokens enter the contract. [8](#0-7) 

**The protocol's own NatSpec at line 94 explicitly acknowledges this behavior:**

```
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

This documents the rounding edge case but does not mitigate the accounting invariant break or the resulting fund drain. [9](#0-8) 

**Existing guards are insufficient:**
- `require(newConsumed <= offer.maxAssets)` — passes because `newConsumed` is unchanged (adding 0).
- `require(offer.maker != taker)` — trivially satisfied with two attacker-controlled addresses.
- `reduceOnly` — not set in the attack offer.
- Health check on seller — taker supplies collateral before each call, a standard protocol operation.

## Impact Explanation

The maker accumulates unbounded credit without ever depositing loan tokens. When `withdrawable` is non-zero (funded by legitimate borrowers repaying via `repay`, which increments `withdrawable` at line 509), the attacker calls `withdraw` to redeem tokens deposited by other lenders. `totalUnits` grows without a matching increase in `withdrawable`, breaking the core accounting invariant that every credit unit must correspond to a deposited loan token. This constitutes direct theft of lender funds — a critical, in-scope impact. [10](#0-9) 

## Likelihood Explanation

Preconditions are trivially met: any buy offer with `tick < MAX_TICK` (the overwhelming majority of valid ticks, since `MAX_TICK = 5820` corresponds to price = WAD exactly) and `maxAssets > 0` is vulnerable. The attacker controls both maker and taker addresses. The attack is repeatable in a single transaction via `multicall`. No oracle manipulation, admin access, flash loan, or special token behavior is required. The taker must supply collateral to pass the health check, but this is a standard protocol operation and the collateral can be recovered after repaying the artificially created debt using the stolen tokens. [11](#0-10) [12](#0-11) 

## Recommendation

Add a guard in `take` that prevents a zero-asset take from increasing credit or debt when `offer.maxAssets > 0`. The minimal fix is to require that `buyerAssets > 0` whenever `units > 0` and the offer uses an asset-based cap:

```solidity
if (offer.maxAssets > 0) {
    uint256 cappedAssets = offer.buy ? buyerAssets : sellerAssets;
    require(units == 0 || cappedAssets > 0, ZeroAssets());
    newConsumed = consumed[offer.maker][offer.group] += cappedAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
}
```

Alternatively, use `mulDivUp` for `buyerAssets` on buy offers (rounding against the taker/maker rather than flooring to zero), ensuring `buyerAssets >= 1` for any nonzero `units` and nonzero `buyerPrice`. This aligns with the stated rounding policy that assets are rounded against the taker. [13](#0-12) 

## Proof of Concept

**Minimal manual steps:**

1. Deploy Midnight with a standard ERC-20 loan token and collateral token.
2. Attacker creates address `maker` and address `taker`.
3. `maker` creates a buy offer: `tick = 0` (price ≈ 0, well below WAD), `maxAssets = 1e18`, `maxUnits = 0`.
4. `taker` supplies sufficient collateral via `supplyCollateral`.
5. Call `take(offer, ..., units=1, taker, ...)` in a loop (or via `multicall`) N times.
   - Each call: `buyerAssets = mulDivDown(1, tickToPrice(0), WAD) = 0`, `consumed` stays 0, `maker.credit += 1`, `taker.debt += 1`, 0 tokens transferred.
6. A legitimate lender provides 100 tokens via a separate buy offer (normal flow), and a borrower repays 100 units via `repay`. `withdrawable` is now 100.
7. `maker` calls `withdraw(100)`. Receives 100 loan tokens despite having deposited 0.
8. Legitimate lender's `withdraw` call reverts (insufficient `withdrawable`).

**Invariant fuzz test plan:** Assert after every `take` call that `sum(credit[i]) - sum(debt[i]) <= tokensInContract + withdrawable`. The attacker's zero-asset takes will violate this invariant immediately. [14](#0-13)

### Citations

**File:** src/Midnight.sol (L94-94)
```text
/// @dev It is possible to give units to a fully consumed assets-based buy offer with price < 1.
```

**File:** src/Midnight.sol (L211-219)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
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

**File:** src/Midnight.sol (L416-417)
```text
        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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

**File:** src/Midnight.sol (L508-509)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);
```

**File:** src/libraries/TickLib.sol (L6-6)
```text
uint256 constant MAX_TICK = 5820;
```

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```
