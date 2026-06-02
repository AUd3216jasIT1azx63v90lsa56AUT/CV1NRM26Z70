Audit Report

## Title
Unprivileged `touchMarket` permanently snapshots zero settlement fees when called before `setDefaultSettlementFee` - (File: src/Midnight.sol)

## Summary
`touchMarket` is an unrestricted `public` function that copies `defaultSettlementFeeCbp[market.loanToken]` into a new market's state at creation time with no guard requiring that defaults have been initialized. Because `defaultSettlementFeeCbp` is a plain Solidity mapping that starts at zero for every loan token and the constructor sets no defaults, any caller can invoke `touchMarket` before the `feeSetter` sets non-zero defaults, permanently writing all seven `settlementFeeCbp` breakpoints as zero for that market. The only remediation path is a privileged `setMarketSettlementFee` call by the `feeSetter` for each of the seven breakpoints on each affected market.

## Finding Description
**Root cause:** `touchMarket` (`src/Midnight.sol:755`) is declared `public` with no access restriction. The creation branch (entered when `marketState[id].tickSpacing == 0`) reads `defaultSettlementFeeCbp[market.loanToken]` at lines 777–784 and copies all seven values directly into `_marketState.settlementFeeCbp0`–`settlementFeeCbp6`. No guard checks that any of these values are non-zero, nor that `setDefaultSettlementFee` has ever been called for the loan token.

`defaultSettlementFeeCbp` is declared as `mapping(address loanToken => uint16[7]) public defaultSettlementFeeCbp` (`src/Midnight.sol:193`). Solidity initializes every mapping slot to zero, and the constructor (`src/Midnight.sol:203–207`) sets only `roleSetter = msg.sender` — no default fees are initialized.

**Exploit flow:**
1. Contract is deployed; `defaultSettlementFeeCbp[loanToken]` is `[0,0,0,0,0,0,0]` for every token.
2. Any address calls `touchMarket(market)` where `market.loanToken` is the target token.
3. `touchMarket` enters the creation branch, reads the all-zero array, and writes `settlementFeeCbp0`–`settlementFeeCbp6 = 0` into `marketState[id]`.
4. `feeSetter` later calls `setDefaultSettlementFee(loanToken, i, nonZeroFee)` for `i = 0..6` — this only updates `defaultSettlementFeeCbp[loanToken][index]` (`src/Midnight.sol:283`), not any already-created market's `MarketState`.
5. `settlementFee(id, ttm)` (`src/Midnight.sol:963–980`) returns 0 for all TTM values on the affected market for its entire lifetime.

**Why existing checks fail:** The guards in `touchMarket` (lines 758–771) validate maturity range, collateral list structure, LLTV, and `maxLif`. None check the state of `defaultSettlementFeeCbp`. `setMarketSettlementFee` (`src/Midnight.sol:258`) can correct a specific market but is gated to `feeSetter` (`require(msg.sender == feeSetter, OnlyFeeSetter())`). The Certora rule `newMarketSettlementFeesMatchDefault` (`certora/specs/SettlementFeeBoundaries.spec:67–77`) asserts `marketSettlementFee(id, index) == expectedSettlementFee` after `touchMarket` — this assertion is satisfied trivially when both sides are zero, so the formal spec does not catch this ordering issue.

## Impact Explanation
The affected market collects zero settlement fees on every `take` call for its entire lifetime until the `feeSetter` manually invokes `setMarketSettlementFee` for each of the seven breakpoints. This is a concrete, measurable protocol revenue loss. The protocol's own comment at `src/Midnight.sol:41` states "A default settlement fee (per loan token) is set on new markets" — the design intent is that non-zero defaults precede market creation, but this ordering is not enforced on-chain. The impact is bounded to fee revenue loss (not user fund loss), and is recoverable only through privileged intervention per affected market, making it a medium-severity finding.

## Likelihood Explanation
The precondition — `defaultSettlementFeeCbp[loanToken]` being all-zero — holds for every loan token until `feeSetter` explicitly initializes it. This is the natural state immediately after deployment and for any newly introduced loan token. An attacker can front-run the `feeSetter`'s `setDefaultSettlementFee` transaction, or the condition arises organically if any user creates a market before the `feeSetter` acts. The attack is a single permissionless call requiring no capital, no special role, and no prior state, and is repeatable for any loan token whose defaults have not yet been set.

## Recommendation
Add a check in the creation branch of `touchMarket` that requires the loan token's default settlement fees to have been initialized before a market can be created. One approach is to revert if all seven `defaultSettlementFeeCbp[market.loanToken]` entries are zero (or track initialization with a separate boolean mapping per loan token). Alternatively, restrict the creation branch of `touchMarket` to a trusted caller, or require that `setDefaultSettlementFee` has been called for the loan token (e.g., by checking that at least one breakpoint is non-zero). The Certora rule `newMarketSettlementFeesMatchDefault` should also be strengthened to require that `expectedSettlementFee > 0` for at least one index before asserting equality.

## Proof of Concept
Minimal Foundry test:
1. Deploy `Midnight`.
2. Construct a valid `Market` struct with any `loanToken` (do not call `setDefaultSettlementFee` first).
3. Call `touchMarket(market)` from any EOA.
4. Assert `marketState[id].settlementFeeCbp0 == 0` through `settlementFeeCbp6 == 0`.
5. Call `settlementFee(id, 30 days)` and assert it returns `0`.
6. As `feeSetter`, call `setDefaultSettlementFee(loanToken, i, nonZeroFee)` for `i = 0..6`.
7. Call `settlementFee(id, 30 days)` again and assert it still returns `0` — confirming the market is permanently unaffected by the default update.