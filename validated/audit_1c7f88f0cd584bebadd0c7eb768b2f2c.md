Audit Report

## Title
Zero Settlement Fee Market Creation via Pre-Configuration `touchMarket` Race - (File: src/Midnight.sol / src/periphery/MidnightBundles.sol)

## Summary
The `Midnight` constructor sets only `roleSetter`, leaving `feeSetter = address(0)` and `defaultSettlementFeeCbp[token]` as all-zeros for every token. Because `touchMarket` is a permissionless `public` function that unconditionally snapshots `defaultSettlementFeeCbp[market.loanToken]` into the new `MarketState` with no guard on non-zero values or a configured `feeSetter`, any caller can create a market with all seven settlement-fee breakpoints permanently set to zero before the deployer has configured defaults. Every `take` in such a market accrues zero to `claimableSettlementFee[loanToken]`, permanently destroying protocol fee revenue for those trades.

## Finding Description

**Root cause — constructor**

The constructor at `src/Midnight.sol:203-207` sets only `roleSetter = msg.sender` and `INITIAL_CHAIN_ID`. `feeSetter` remains `address(0)` and `defaultSettlementFeeCbp[token]` is the zero-initialized mapping default for every token immediately after deployment.

**`touchMarket` — no guard on zero defaults**

`src/Midnight.sol:777-784` unconditionally copies `defaultSettlementFeeCbp[market.loanToken]` into the new `MarketState`:

```solidity
uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
_marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
// ... through index 6
```

There is no `require` that any of the seven values is non-zero, no check that `feeSetter != address(0)`, and no check that defaults have been explicitly configured. The only guards are structural market validity checks (collateral sorting, LLTV, maturity).

**`take` also calls `touchMarket`**

`src/Midnight.sol:347`: `bytes32 id = touchMarket(offer.market);` — any `take` call before fee configuration will also trigger market creation with zero fees.

**`MidnightBundles` entry points**

`src/periphery/MidnightBundles.sol:64`, `131`, `195`, and `266` all call `IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market)` as additional permissionless entry points.

**Settlement fee accrual — zero impact**

`src/Midnight.sol:963-980`: `settlementFee(id, timeToMaturity)` returns `0` when all `settlementFeeCbp0..6` are zero. Consequently in `take` at `src/Midnight.sol:361-364`:

```solidity
uint256 _settlementFee = settlementFee(id, timeToMaturity); // = 0
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 buyerPrice = sellerPrice + _settlementFee; // == sellerPrice
```

So `buyerAssets == sellerAssets`, and `src/Midnight.sol:418`:

```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets; // += 0
```

**Why `setMarketSettlementFee` is insufficient**

`src/Midnight.sol:258-275`: `feeSetter` can correct the market post-creation, but all takes executed before that correction paid zero fees and those fees are permanently unrecoverable.

**Exploit flow**

1. Attacker observes `Midnight` deployment. `feeSetter = address(0)`; `defaultSettlementFeeCbp[loanToken] = [0,0,0,0,0,0,0]`.
2. Before `roleSetter` calls `setFeeSetter` and before `feeSetter` calls `setDefaultSettlementFee`, attacker calls `touchMarket(market)` with a valid `Market` struct (allowed LLTV, sorted collateral params).
3. `touchMarket` creates the market and writes `settlementFeeCbp0..6 = 0` into `marketState[id]`.
4. Every subsequent `take` in this market computes `_settlementFee = 0`, so `buyerAssets - sellerAssets = 0`, and `claimableSettlementFee[loanToken]` never accumulates for those takes.
5. Even after `feeSetter` calls `setMarketSettlementFee` to correct the market, all prior takes are unrecoverable.

## Impact Explanation
Any market created before `feeSetter` configures defaults for its `loanToken` will have all seven settlement-fee breakpoints permanently set to zero. Every `take` in that market pays zero settlement fee, meaning `claimableSettlementFee[loanToken]` never accumulates for those trades. The protocol permanently loses all settlement-fee revenue for the lifetime of affected takes. This is a direct, concrete loss of protocol revenue — not a hypothetical — triggered by an unprivileged user with no special access.

## Likelihood Explanation
The precondition is a deployment-ordering race: `feeSetter = address(0)` immediately after construction, and `defaultSettlementFeeCbp` is all-zeros for every token until explicitly configured via two separate privileged transactions (`setFeeSetter` then `setDefaultSettlementFee`). Any attacker monitoring the mempool can front-run these setup transactions. The attack is repeatable for every new `loanToken` whose defaults have not yet been set, requires no special privilege, and can be triggered via `touchMarket` directly, via `take`, or via any of the four `MidnightBundles` bundle functions.

## Recommendation
Add a guard in `touchMarket` that prevents market creation before fee defaults are configured. Options include:

1. **Require `feeSetter != address(0)`** before creating a new market — ensures the role has been assigned before any market can be created.
2. **Require at least one non-zero settlement fee breakpoint** in `defaultSettlementFeeCbp[market.loanToken]` before snapshotting into `MarketState`.
3. **Atomic deployment**: Use `multicall` to atomically call `setFeeSetter` and `setDefaultSettlementFee` for all expected loan tokens in the same transaction as deployment (or immediately after via a deployment script that bundles all setup). This is an operational mitigation but does not fix the on-chain invariant.

The most robust fix is option 1 or 2 enforced at the contract level in `touchMarket`.

## Proof of Concept
```solidity
// 1. Deploy Midnight — feeSetter = address(0), defaultSettlementFeeCbp[USDC] = [0,0,0,0,0,0,0]
Midnight midnight = new Midnight();

// 2. Attacker calls touchMarket before any fee setup
Market memory market = Market({
    loanToken: USDC,
    maturity: block.timestamp + 30 days,
    collateralParams: validCollateralParams, // sorted, valid LLTV
    // ...
});
bytes32 id = midnight.touchMarket(market);

// 3. Verify: all settlement fee cbps are zero
uint16[7] memory fees = midnight.settlementFeeCbps(id);
// fees == [0, 0, 0, 0, 0, 0, 0]

// 4. Admin sets up feeSetter and calls setDefaultSettlementFee — too late for this market
midnight.setFeeSetter(feeSetterAddr); // roleSetter tx
// feeSetter calls setDefaultSettlementFee — only affects future markets

// 5. feeSetter calls setMarketSettlementFee to patch this market
midnight.setMarketSettlementFee(id, 6, 5000 * CBP); // fixes future takes

// 6. All takes between steps 2 and 5 paid zero settlement fee
// claimableSettlementFee[USDC] == 0 for those takes — permanently lost
```