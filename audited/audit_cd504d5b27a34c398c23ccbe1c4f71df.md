### Title
Lenders Can Race to Withdraw Before Bad Debt Realization, Unfairly Socializing Losses - (File: src/Midnight.sol)

### Summary

The `withdraw` function in `Midnight.sol` has no lockup period and only applies the already-committed `lossFactor`. Because all borrower positions and oracle prices are public on-chain, a lender can observe that a borrower has bad debt before `liquidate` is called, withdraw their credit from the `withdrawable` pool first, and exit with zero loss — leaving the bad debt to be socialized exclusively among the lenders who did not exit in time.

### Finding Description

**Root cause — two cooperating conditions:**

1. `withdraw()` calls `_updatePosition()`, which applies only the *current* `marketState[id].lossFactor`. Bad debt that exists on-chain (a borrower whose collateral is worth less than their debt) is not yet reflected in `lossFactor` until someone calls `liquidate()`. There is no mechanism that prevents a lender from withdrawing in the window between "bad debt is visible on-chain" and "bad debt is realized via `liquidate`." [1](#0-0) 

2. `_updatePosition()` / `updatePositionView()` computes the post-slash credit using only `marketState[id].lossFactor` as it stands at call time: [2](#0-1) 

3. There is no lockup period, no epoch gate, and no check for pending bad debt in `withdraw`. The protocol itself acknowledges the resulting race in a dev comment but provides no mitigation: [3](#0-2) 

**Bad-debt socialization path (for comparison):**

When `liquidate()` is eventually called, bad debt is socialized by increasing `lossFactor` proportionally to the *remaining* `totalUnits` at that moment: [4](#0-3) 

Because `withdraw()` already decremented `totalUnits` before `liquidate()` ran, the exiting lender's share is permanently removed from the denominator — their portion of the loss falls on whoever remains.

**Exploit flow:**

1. Market M has lenders L1, L2, L3 each with 100 credit units (`totalUnits = 300`).
2. Borrower B1 repays 100 units → `withdrawable = 100`.
3. Borrower B2 has 150 debt units but collateral worth only 100 units at current oracle price → bad debt = 50 units. `liquidate(B2)` has not been called yet.
4. L1 monitors the chain, observes B2's collateral value < B2's debt via public oracle and position data.
5. L1 calls `withdraw(100)`:
   - `_updatePosition` applies current `lossFactor = 0` → no slash
   - `position[id][L1].credit` → 0; `withdrawable` → 0; `totalUnits` → 200
   - L1 receives 100 loan tokens
6. `liquidate(B2)` is called. Bad debt = 50 is socialized over `totalUnits = 200`:
   - `lossFactor` increases; L2 and L3 each lose ~25 credit units
7. L1 avoided the ~33-unit loss they would have borne had they not exited first.

### Impact Explanation

Lenders who actively monitor the chain and withdraw before bad debt is realized suffer zero loss. Passive lenders who remain in the market absorb the full socialized loss, including the share that should have been borne by the exiting lender. This is a direct, measurable transfer of value from passive lenders to sophisticated/monitoring lenders, repeatable across every bad-debt event in every market.

### Likelihood Explanation

- **No privileged access required.** Any lender can call `withdraw`.
- **All information is public.** Borrower debt (`position[id][borrower].debt`), collateral amounts (`position[id][borrower].collateral[i]`), and oracle prices are all readable on-chain.
- **No lockup.** There is zero delay between observing bad debt and executing the withdrawal.
- **Economically rational.** The incentive is direct: exit before `liquidate` is called and keep 100% of credit value instead of absorbing a proportional loss.
- **Realistic market condition.** Any market with multiple borrowers where at least one has repaid (creating non-zero `withdrawable`) and another has gone underwater satisfies the preconditions.

### Recommendation

Implement one of the following mitigations:

1. **Epoch-based withdrawal buffer:** Prevent withdrawals from being settled until the end of a defined epoch, ensuring all bad debt within that epoch is realized before any lender can exit.
2. **Lockup period:** Require lenders to signal withdrawal intent and enforce a delay (e.g., 24–48 hours) before tokens are released, giving liquidators time to realize bad debt first.
3. **Pre-withdrawal bad-debt check:** Before processing a withdrawal, iterate over known unhealthy borrowers and revert (or force-realize bad debt) if any exist — though this has gas and liveness tradeoffs.

### Proof of Concept

```
State before attack:
  totalUnits    = 300  (L1=100, L2=100, L3=100 credit)
  withdrawable  = 100  (B1 repaid)
  lossFactor    = 0
  B2.debt       = 150, B2.collateral value = 100  → bad debt = 50 (not yet realized)

Step 1: L1 calls withdraw(market, 100, L1, L1)
  _updatePosition: lossFactor=0 → no slash → newCredit=100
  position[id][L1].credit  = 0
  withdrawable              = 0
  totalUnits                = 200
  L1 receives 100 tokens ✓

Step 2: Anyone calls liquidate(market, ..., B2, ...)
  badDebt = 50
  lossFactor increases: proportional to 50/200 = 25% of remaining units
  L2.credit slashed by ~25 → ~75
  L3.credit slashed by ~25 → ~75
  L1: already exited, zero impact

Result:
  L1 avoided ~33 units of loss (their fair share of 50 bad debt / 3 lenders)
  L2 and L3 each bear ~25 units instead of ~17 units
  Net transfer: ~8 units per remaining lender from passive to active lender
```

### Citations

**File:** src/Midnight.sol (L27-29)
```text
/// @dev When some assets become withdrawable before maturity (after a repayment or a liquidation), there
/// is an incentive to take resting sell offers with price < 1 and withdraw instantly. Lenders (and the fee claimer)
/// might also race to withdraw first.
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

**File:** src/Midnight.sol (L626-634)
```text
        if (badDebt > 0) {
            // forge-lint: disable-next-item(unsafe-typecast) as badDebt <= _position.debt
            _position.debt -= uint128(badDebt);
            uint256 _totalUnits = _marketState.totalUnits;
            uint256 _lossFactor = _marketState.lossFactor;
            _marketState.lossFactor = UtilsLib.toUint128(
                type(uint128).max - (type(uint128).max - _lossFactor).mulDivDown(_totalUnits - badDebt, _totalUnits)
            );
            _marketState.totalUnits -= UtilsLib.toUint128(badDebt);
```

**File:** src/Midnight.sol (L804-807)
```text
        uint128 _lastLossFactor = _position.lastLossFactor;
        uint256 postSlashCredit = _lastLossFactor < type(uint128).max
            ? credit.mulDivDown(type(uint128).max - marketState[id].lossFactor, type(uint128).max - _lastLossFactor)
            : 0;
```
