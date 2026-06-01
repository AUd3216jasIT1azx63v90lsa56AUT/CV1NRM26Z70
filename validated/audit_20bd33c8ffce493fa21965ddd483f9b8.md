Audit Report

## Title
Permissionless `touchMarket` Allows Front-Running of `setDefaultSettlementFee`, Permanently Initializing Markets with Stale Lower Fees - (File: src/Midnight.sol)

## Summary
`touchMarket` is a public, permissionless function that snapshots `defaultSettlementFeeCbp[loanToken]` into a new market's `settlementFeeCbp0..6` at creation time with no access control or atomicity guarantee relative to fee updates. An attacker who observes a pending `setDefaultSettlementFee` transaction in the mempool can front-run it by calling `touchMarket` first, causing the market to be permanently initialized with the old lower fee. All subsequent `take` calls in that market pay a lower settlement fee than the feeSetter intended, reducing protocol fee revenue until the feeSetter manually intervenes.

## Finding Description

**Root cause and code path:**

`touchMarket` (`src/Midnight.sol:755-791`) is unconditionally `public` with no caller restriction. When `marketState[id].tickSpacing == 0` (market not yet initialized), it reads the current `defaultSettlementFeeCbp[market.loanToken]` array and copies it verbatim into the new market's persistent state:

```solidity
// src/Midnight.sol:777-784
uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
_marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
_marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
// ... through index 6
```

`setDefaultSettlementFee` (`src/Midnight.sol:277-285`) is feeSetter-only and updates `defaultSettlementFeeCbp[loanToken][index]` immediately with no timelock and no retroactive propagation to already-created markets.

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
- The feeSetter can remediate via `setMarketSettlementFee` (`src/Midnight.sol:258-275`), but cannot prevent the initial exploit window.

## Impact Explanation
The created market permanently holds a lower settlement fee than the current protocol default. Every `take` in that market accrues less `claimableSettlementFee` to the protocol than intended. The state divergence between `defaultSettlementFeeCbp[loanToken][*]` and `marketState[id].settlementFeeCbp*` is concrete and measurable. The attacker (as taker or maker) benefits from reduced fee cost on all trades in that market until the feeSetter manually intervenes with `setMarketSettlementFee`. The attack is repeatable: for every fee increase the feeSetter attempts, the attacker can front-run with a new market (different maturity or collateral params yields a different `id`), systematically suppressing protocol fee revenue across all new markets for a given loan token.

## Likelihood Explanation
Preconditions: (1) feeSetter broadcasts a `setDefaultSettlementFee` transaction raising fees for a loanToken; (2) attacker has mempool visibility (standard MEV infrastructure available on any public EVM chain). Both are realistic. The attack requires no special role, no capital beyond gas, and no victim mistake. The attacker controls all inputs to `touchMarket` and can construct any valid market parameters. The attack is repeatable for every fee increase attempt.

## Recommendation
The most robust fix is to use `multicall` atomically from the feeSetter side: call `setDefaultSettlementFee` and immediately `touchMarket` for all anticipated markets in the same transaction. However, this cannot cover all possible future market parameter combinations.

A stronger fix is to add a feeSetter-controlled mechanism to update the settlement fees of an existing market to match the current default (a "sync to default" function), or to add a timelock/commit-reveal scheme to `setDefaultSettlementFee` so that the new fee is not readable from `defaultSettlementFeeCbp` until after the timelock expires, eliminating the front-running window.

Alternatively, `touchMarket` could accept an optional expected fee array and revert if `defaultSettlementFeeCbp` has changed since the caller's snapshot, allowing callers to enforce atomicity at the application layer.

## Proof of Concept
Minimal Foundry fork test:

```solidity
// 1. feeSetter sets default fee to 0 (initial state)
midnight.setDefaultSettlementFee(loanToken, 1, 0);

// 2. feeSetter broadcasts setDefaultSettlementFee(loanToken, 1, higherFee) — attacker sees it in mempool

// 3. Attacker front-runs: touchMarket with loanToken, any valid maturity/collateral params
bytes32 id = midnight.touchMarket(market); // mines before feeSetter tx

// 4. feeSetter tx mines: defaultSettlementFeeCbp[loanToken][1] = higherFee
midnight.setDefaultSettlementFee(loanToken, 1, higherFee);

// 5. Assert: market still has old fee
(,,,, , uint16 cbp1,,,,,,,) = midnight.marketState(id);
assertEq(uint256(cbp1) * CBP, 0, "market has stale lower fee");
assertEq(midnight.defaultSettlementFeeCbp(loanToken, 1), higherFee / CBP, "default updated");
// All take() calls in this market use the stale lower fee
```