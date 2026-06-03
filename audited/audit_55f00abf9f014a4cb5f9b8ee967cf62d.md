All code references in the submitted report are accurate. Here is the validation result:

---

Audit Report

## Title
Permissionless `touchMarket` Allows Front-Running of `setDefaultSettlementFee`, Permanently Initializing Markets with Stale Lower Fees - (File: src/Midnight.sol)

## Summary
`touchMarket` is unconditionally `public` with no access control and snapshots `defaultSettlementFeeCbp[loanToken]` into a new market's `settlementFeeCbp0..6` at creation time. An unprivileged attacker who observes a pending `setDefaultSettlementFee` transaction can front-run it by calling `touchMarket` first, causing the market to be permanently initialized with the old lower fee schedule. All subsequent `take` calls in that market pay a lower settlement fee than the feeSetter intended, reducing protocol fee revenue until the feeSetter manually intervenes per-market.

## Finding Description
`touchMarket` (`src/Midnight.sol:755`) is `public` with no caller restriction. When `marketState[id].tickSpacing == 0`, it reads `defaultSettlementFeeCbp[market.loanToken]` and copies it verbatim into the new market's persistent storage:

```solidity
// src/Midnight.sol:777-784
uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
_marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
// ... through index 6
```

`setDefaultSettlementFee` (`src/Midnight.sol:277-285`) is feeSetter-only and updates `defaultSettlementFeeCbp[loanToken][index]` with no timelock and no retroactive propagation to already-created markets.

`take` (`src/Midnight.sol:347`) calls `touchMarket(offer.market)` internally, then at line 360 calls `settlementFee(id, timeToMaturity)`. The `settlementFee` function (`src/Midnight.sol:963-980`) reads exclusively from the market's snapshotted `settlementFeeCbp*` storage fields — never from `defaultSettlementFeeCbp`. Once a market is created, its fee schedule is fixed in its own storage.

**Exploit flow:**
1. feeSetter broadcasts `setDefaultSettlementFee(loanToken, index, higherFee)` to the mempool.
2. Attacker observes the pending transaction and broadcasts `touchMarket(market)` (where `market.loanToken == loanToken`) with higher gas priority.
3. Attacker's `touchMarket` mines first: market is created with `settlementFeeCbp* = old_lower_fee`.
4. feeSetter's `setDefaultSettlementFee` mines: `defaultSettlementFeeCbp[loanToken][index]` is updated to `higherFee`, but the already-created market is unaffected.
5. All `take` calls in that market use `old_lower_fee` for settlement fee computation, accruing less `claimableSettlementFee` to the protocol than intended.

**Why existing checks fail:**
- No access control on `touchMarket` — any address can create any valid market.
- No atomicity mechanism between fee updates and market creation; `multicall` (`src/Midnight.sol:211-220`) uses `delegatecall` and could allow the feeSetter to batch `setDefaultSettlementFee` + `touchMarket`, but this does not prevent an attacker from front-running the entire multicall transaction.
- The Certora rule `newMarketSettlementFeesMatchDefault` (`certora/specs/SettlementFeeBoundaries.spec:67-77`) only asserts fees match at the instant of creation within a single atomic execution; it does not model or prevent the mempool race condition.
- The feeSetter can remediate via `setMarketSettlementFee` (`src/Midnight.sol:258-275`), but cannot prevent the initial exploit window, and the attack is repeatable for every fee increase attempt across any new market parameters.

## Impact Explanation
The created market permanently holds a lower settlement fee than the current protocol default. Every `take` in that market accrues less `claimableSettlementFee` to the protocol than intended. The state divergence between `defaultSettlementFeeCbp[loanToken][*]` and `marketState[id].settlementFeeCbp*` is concrete and measurable. The attacker (as taker or maker) benefits from reduced fee cost on all trades in that market until the feeSetter manually intervenes with `setMarketSettlementFee`. The attack is repeatable: for every fee increase the feeSetter attempts, the attacker can front-run with a new market (different maturity or collateral params yields a different `id`), systematically suppressing protocol fee revenue across all new markets for a given loan token.

## Likelihood Explanation
Preconditions: (1) feeSetter broadcasts a `setDefaultSettlementFee` transaction raising fees for a loanToken; (2) attacker has mempool visibility (standard MEV infrastructure available on any public EVM chain). Both are realistic. The attack requires no special role, no capital beyond gas, and no victim mistake. The attacker controls all inputs to `touchMarket` and can construct any valid market parameters. The attack is repeatable for every fee increase attempt.

## Recommendation
Restrict `touchMarket` to `internal` visibility, or add an access-control gate (e.g., allowlist of callers or a feeSetter-signed permit). Alternatively, introduce a two-step market creation flow where the fee snapshot is taken atomically with the fee update, or allow the feeSetter to push updated default fees retroactively to existing markets via a batch function. At minimum, document that `setDefaultSettlementFee` should always be batched with `touchMarket` via `multicall` and that the window between the two calls is exploitable.

## Proof of Concept
1. Deploy `Midnight` with `feeSetter = alice`.
2. Call `setDefaultSettlementFee(loanToken, 0, lowFee)` as alice to set an initial low fee.
3. Construct a valid `Market` struct with `loanToken` and any valid `maturity` and `collateralParams`.
4. As alice, submit `setDefaultSettlementFee(loanToken, 0, highFee)` to the mempool.
5. As attacker (bob), observe the pending tx and submit `touchMarket(market)` with higher gas.
6. Bob's tx mines first: `marketState[id].settlementFeeCbp0 == lowFee / CBP`.
7. Alice's tx mines: `defaultSettlementFeeCbp[loanToken][0] == highFee / CBP`, but `marketState[id].settlementFeeCbp0` is unchanged.
8. Call `settlementFee(id, 0)` — returns `lowFee`, not `highFee`.
9. Confirm `take` in this market accrues settlement fee at `lowFee` rate.