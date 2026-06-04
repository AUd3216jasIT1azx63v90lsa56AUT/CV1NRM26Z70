### Title
Lenders Can Front-Run Bad Debt Liquidations to Avoid Loss Socialization — (File: src/Midnight.sol)

---

### Summary

Midnight socializes bad debt lazily: `liquidate()` increases the global `lossFactor`, and each lender's credit is slashed only at their next `_updatePosition` call. Because `withdraw()` applies only the **current** `lossFactor` before allowing exit, a lender who observes a pending bad-debt `liquidate()` transaction in the mempool can front-run it with `withdraw()`, exiting before `lossFactor` rises. The remaining lenders absorb a disproportionately larger share of the loss.

---

### Finding Description

**Vulnerability class:** State-transition / accounting — lazy loss socialization exploitable via front-running.

**Root cause — `withdraw()` has no guard against pending bad debt:**

`withdraw()` calls `_updatePosition` (line 485), which snapshots the **current** `lossFactor` into `lastLossFactor` (line 843) and slashes credit accordingly. It then reduces `credit`, `withdrawable`, and `totalUnits` (lines 493–495). There is no check that prevents withdrawal when a bad-debt liquidation is observable but not yet mined. [1](#0-0) 

**`liquidate()` computes the new `lossFactor` using `totalUnits` at execution time:**

```
newLossFactor = MAX - (MAX - oldLossFactor) * (totalUnits - badDebt) / totalUnits
``` [2](#0-1) 

If a lender has already withdrawn (reducing `totalUnits`) before this line executes, the denominator is smaller, so the loss factor rises more steeply, and the remaining lenders absorb a larger absolute loss.

**`_updatePosition` only applies the loss factor that exists at call time — it cannot retroactively capture a future `lossFactor` increase:** [3](#0-2) [4](#0-3) 

---

### Impact Explanation

A front-running lender fully escapes their proportional share of bad debt. That share is redistributed entirely to lenders who remain in the market. In a two-lender market where each holds 50 % of credit, the attacker shifts 100 % of their expected loss onto the other lender. In larger markets the effect is proportional but still material. This is a direct, measurable theft of value from honest lenders.

---

### Likelihood Explanation

**Preconditions (all realistic):**

1. **Mempool visibility** — bad-debt `liquidate()` calls are standard transactions, visible to any mempool watcher.
2. **Available `withdrawable` balance** — in any active market where some borrowers have repaid, `withdrawable > 0`. The attacker only needs `withdrawable >= their credit` (or a partial amount) to execute the escape.
3. **No privileged access required** — `withdraw()` is permissionless for the position owner.

The attack is straightforward to automate with a bot that monitors for liquidatable positions with bad debt and submits a higher-gas `withdraw()` ahead of the liquidation.

---

### Recommendation

Before allowing a lender to withdraw, check whether any borrower in the market is currently liquidatable with bad debt. If so, revert until the bad debt is realized first. A simpler and more gas-efficient alternative (matching the infiniFi fix) is to revert `withdraw()` when `marketState[id].lossFactor` has changed since the lender's `lastLossFactor` **and** there exists any position with `debt > 0` that would produce bad debt on liquidation — though this requires an oracle call.

The most practical fix is to require that `liquidate()` (bad-debt path) be called atomically before `withdraw()` in the same block, or to add a one-block withdrawal delay so that any pending bad-debt liquidation in the same block is applied first.

---

### Proof of Concept

**Setup:**
- Market with `totalUnits = 1000`, `withdrawable = 500`, `lossFactor = 0`
- Lender A: `credit = 500`
- Lender B: `credit = 500`
- Borrower C: `debt = 100`, fully undercollateralized → `badDebt = 100`

**Without front-running (honest scenario):**

Liquidator calls `liquidate()`:
```
newLossFactor = MAX - MAX * (1000 - 100) / 1000 = MAX * 0.1
```
At next interaction:
- Lender A credit: `500 * 0.9 = 450` (loses 50)
- Lender B credit: `500 * 0.9 = 450` (loses 50)
- Loss shared equally: 50 each.

**With front-running:**

Step 1 — Lender A sees the liquidation in the mempool and submits `withdraw(500)` with higher gas:
- `_updatePosition`: `lossFactor = 0`, no slash, `lastLossFactor` set to 0
- `credit[A] -= 500` → 0
- `withdrawable -= 500` → 0
- `totalUnits -= 500` → 500
- Lender A receives 500 tokens ✓

Step 2 — `liquidate()` executes:
```
totalUnits = 500, badDebt = 100
newLossFactor = MAX - MAX * (500 - 100) / 500 = MAX * 0.2
totalUnits → 400
```

Step 3 — Lender B's credit at next interaction:
```
newCredit = 500 * (MAX - MAX*0.2) / (MAX - 0) = 500 * 0.8 = 400
```
Lender B loses 100 units — the **entire** bad debt.

**Result:** Lender A escapes with 500 tokens (no loss). Lender B absorbs 100 units of loss instead of 50. Lender A has stolen 50 units of value from Lender B. [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L481-500)
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
    }
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

**File:** src/Midnight.sol (L805-807)
```text
        uint256 postSlashCredit = _lastLossFactor < type(uint128).max
            ? credit.mulDivDown(type(uint128).max - marketState[id].lossFactor, type(uint128).max - _lastLossFactor)
            : 0;
```

**File:** src/Midnight.sol (L842-843)
```text
        _position.credit = newCredit;
        _position.lastLossFactor = marketState[id].lossFactor;
```
