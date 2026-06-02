All code claims verified against the actual source. Here is the result:

---

Audit Report

## Title
Fee-on-Transfer `loanToken` Overcredits `claimableSettlementFee` Relative to Actual Balance - (File: `src/Midnight.sol`)

## Summary
In `take()`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the full nominal spread `buyerAssets - sellerAssets` at line 418, before the actual `safeTransferFrom` at line 455. When `loanToken` is a fee-on-transfer token, only `(buyerAssets - sellerAssets) * (1 - fee%)` tokens arrive at the contract, creating a permanent and compounding gap between the accounting entry and the real balance. Because `touchMarket` imposes no restriction on `loanToken`, any unprivileged actor can trigger this condition.

## Finding Description
**Root cause:** `take()` writes the accounting entry at line 418 unconditionally using the nominal transfer amount, then performs the actual token transfer at line 455. For a fee-on-transfer token with fee rate `f`, the contract receives `(buyerAssets - sellerAssets) * (1 - f)` but `claimableSettlementFee` is credited the full `buyerAssets - sellerAssets`. The deficit per `take()` call is `(buyerAssets - sellerAssets) * f`, compounding across every subsequent `take()` in the market's lifetime.

**Exact code path:**
- Line 418 writes the accounting entry: `claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets`
- Lines 455–456 perform the actual transfers afterward, with line 455 sending `buyerAssets - sellerAssets` to the contract (reduced by the token's fee) and line 456 sending `sellerAssets` directly to `receiver`

**Why existing checks fail:** `touchMarket()` (lines 755–791) validates maturity, collateral params, LLTV tiers, and collateral sort order, but performs zero validation on `loanToken` — any address is accepted. There is no token whitelist, no `transferFrom`-and-verify pattern, and no balance-before/after check anywhere in the `take()` flow.

**`claimSettlementFee` transfer path:** When `feeClaimer` calls `claimSettlementFee(feeToken, claimableSettlementFee[feeToken], receiver)`, the `safeTransfer` at line 309 attempts to send the full overcredited amount, which exceeds the actual contract balance for that token.

## Impact Explanation
Two concrete outcomes:

1. **Fee claimer DoS / loss of protocol revenue:** If the contract's total `feeToken` balance is less than `claimableSettlementFee[feeToken]`, the `safeTransfer` at line 309 reverts. The fee claimer cannot withdraw the full recorded amount; accumulated protocol fees are permanently unclaimable for that token.

2. **Solvency violation / theft from other users:** If other `feeToken` balance exists in the contract (e.g., from lenders' deposits or borrowers' collateral denominated in the same token across any market), the transfer succeeds by consuming tokens that belong to other users, directly breaking the core invariant `balance >= collateral + withdrawable + claimableSettlementFee`.

Both outcomes constitute protocol insolvency for the affected token. The second outcome is direct theft of user funds.

## Likelihood Explanation
- **Preconditions:** Deploy any fee-on-transfer ERC20 (trivial, no privileged access required) and call `touchMarket(market)` with `market.loanToken = address(feeToken)` — fully permissionless.
- **Trigger:** Any taker interacting with an offer in that market calls `take()`. No victim mistake, no oracle manipulation, no governance action required.
- **Repeatability:** Every `take()` call widens the deficit by `spread * fee_rate`. The effect is deterministic and compounds indefinitely.
- **Attacker profile:** Unprivileged external user. No leaked keys, no privileged roles.

## Recommendation
Replace the nominal-amount accounting with a balance-before/after pattern in `take()`. Before the `safeTransferFrom` at line 455, record `uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this))`. After the transfer, compute `uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore` and use `received` (not `buyerAssets - sellerAssets`) as the increment to `claimableSettlementFee`. This eliminates the discrepancy for fee-on-transfer tokens while remaining correct for standard ERC20s. Alternatively, explicitly whitelist `loanToken` addresses in `touchMarket()` to exclude fee-on-transfer tokens from the protocol entirely.

## Proof of Concept
1. Deploy a standard ERC20 with a 1% transfer fee (fee deducted from recipient on every `transferFrom`).
2. Call `touchMarket(market)` with `market.loanToken = address(feeToken)` — succeeds with no restriction.
3. Post a buy offer and call `take()` with `units` such that `buyerAssets - sellerAssets = 1000e18`.
4. Observe: `claimableSettlementFee[feeToken]` increases by `1000e18`, but the contract only receives `990e18`.
5. Repeat step 3 ten times: `claimableSettlementFee[feeToken] = 10000e18`, actual balance from fees = `9900e18`.
6. Call `claimSettlementFee(feeToken, 10000e18, receiver)` — `safeTransfer` reverts (DoS path), or if the contract holds `≥100e18` of `feeToken` from other users, those tokens are transferred to `receiver` instead (theft path). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** src/Midnight.sol (L305-309)
```text
    function claimSettlementFee(address token, uint256 amount, address receiver) external {
        require(msg.sender == feeClaimer, OnlyFeeClaimer());
        claimableSettlementFee[token] -= amount;
        emit EventsLib.ClaimSettlementFee(msg.sender, token, amount, receiver);
        SafeTransferLib.safeTransfer(token, receiver, amount);
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L755-791)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
            for (uint256 i = 0; i < market.collateralParams.length; i++) {
                address collateralToken = market.collateralParams[i].token;
                require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
                uint256 lltv = market.collateralParams[i].lltv;
                require(isLltvAllowed(lltv), LltvNotAllowed());
                require(
                    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
                        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
                    InvalidMaxLif()
                );
                previousCollateralToken = collateralToken;
            }

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```
