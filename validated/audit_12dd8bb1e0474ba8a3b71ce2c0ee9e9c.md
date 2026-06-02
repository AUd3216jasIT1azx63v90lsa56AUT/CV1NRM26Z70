All key claims check out against the actual code. Let me verify the `MidnightBundles` try/catch behavior as well.

Audit Report

## Title
Missing `loanToken != address(0)` validation in `touchMarket` permanently freezes all takes on the created market - (File: src/Midnight.sol)

## Summary
`touchMarket` is a public, permissionless function that validates collateral params, LLTV, maxLif, and maturity but performs no check that `market.loanToken != address(0)`. An unprivileged caller can create a market with `loanToken = address(0)`, which succeeds and snapshots all-zero settlement fees. Every subsequent `take()` on this market unconditionally reverts with `SafeTransferLib.NoCode()` at the token transfer step, permanently freezing the market with no recovery path.

## Finding Description

**Root cause — missing check in `touchMarket`:**

`touchMarket` at `src/Midnight.sol:755-791` validates maturity, collateral params (sorted and non-zero via `collateralToken > previousCollateralToken`), LLTV, and maxLif. There is no `require(market.loanToken != address(0))` guard. [1](#0-0) 

The sorted-collateral check (`collateralToken > previousCollateralToken`) enforces non-zero collateral tokens only; it is entirely independent of `loanToken`. [2](#0-1) 

**Fee snapshot with zero values:**

On creation, the market snapshots `defaultSettlementFeeCbp[market.loanToken]`. With `loanToken = address(0)` and no governance call to `setDefaultSettlementFee(address(0), ...)`, all seven breakpoints are zero. [3](#0-2) 

**`take()` reverts unconditionally at token transfer:**

`take()` calls `touchMarket` (line 347, a no-op for an already-created market), performs all position accounting and state mutations, then reaches lines 455–456 where it calls `SafeTransferLib.safeTransferFrom(offer.market.loanToken, ...)` with `offer.market.loanToken = address(0)`. [4](#0-3) 

`SafeTransferLib.safeTransferFrom` begins with `require(token.code.length > 0, NoCode())`. Since `address(0).code.length == 0`, this reverts unconditionally regardless of transfer amount. [5](#0-4) 

**Bundler interaction:**

`MidnightBundles` wraps each `take()` in a `try/catch {}`. The revert is silently swallowed, `filledUnits` never reaches `targetUnits`, and the bundler reverts with `OutOfOffers()`. [6](#0-5) 

**Market cannot be overwritten:**

The `if (marketState[id].tickSpacing == 0)` guard means a market is initialized exactly once. Once created with `loanToken = address(0)`, the market state is permanent and irrecoverable. [7](#0-6) 

## Impact Explanation

Any offers placed on a market created with `loanToken = address(0)` are permanently untakeable. The market ID is deterministic and unique; it cannot be overwritten or repaired. State mutations (position accounting, consumed tracking, `claimableSettlementFee` increment) execute before the revert, but the revert rolls them all back — leaving the market in a permanently frozen state where no take can ever succeed. This constitutes a permanent freeze of user/protocol state, which is an in-scope impact per RESEARCHER.md. [8](#0-7) 

## Likelihood Explanation

`touchMarket` is `public` with no access control. The only inputs required are valid collateral params, a valid LLTV tier, a valid maxLif, and a maturity within range — all trivially constructable. Setting `loanToken = address(0)` requires no privilege. The condition is permanent and repeatable for any distinct `(address(0), maturity, collateralParams)` tuple. The market cannot be overwritten once created. [9](#0-8) 

## Recommendation

Add a non-zero check for `loanToken` at the start of the market creation branch in `touchMarket`:

```solidity
require(market.loanToken != address(0), InvalidLoanToken());
```

This should be placed inside the `if (marketState[id].tickSpacing == 0)` block, alongside the existing maturity and collateral param validations, before any state is written. [10](#0-9) 

## Proof of Concept

Minimal Foundry test:

```solidity
function testTouchMarketZeroLoanToken() public {
    // 1. Build a market with loanToken = address(0) and valid collateral params
    Market memory m;
    m.loanToken = address(0);
    m.maturity = block.timestamp + 365 days;
    m.collateralParams = new CollateralParam[](1);
    m.collateralParams[0].token = address(collateralToken); // any non-zero token
    m.collateralParams[0].lltv = ALLOWED_LLTV;
    m.collateralParams[0].maxLif = maxLif(ALLOWED_LLTV, LIQUIDATION_CURSOR_LOW);

    // 2. Anyone can create the market — no revert
    bytes32 id = midnight.touchMarket(m);
    assertGt(midnight.tickSpacing(id), 0); // market exists

    // 3. Construct a valid offer on this market and attempt take
    Offer memory offer = /* valid signed offer on market m */;
    vm.expectRevert(SafeTransferLib.NoCode.selector);
    midnight.take(offer, hex"", 1e18, taker, address(0), address(0), hex"");
}
```

Expected result: `touchMarket` succeeds (step 2), `take` reverts with `NoCode` (step 3), and no governance action can repair the market. [11](#0-10)

### Citations

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/Midnight.sol (L755-773)
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
```

**File:** src/Midnight.sol (L777-784)
```text
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
```

**File:** src/libraries/SafeTransferLib.sol (L8-8)
```text
    error NoCode();
```

**File:** src/libraries/SafeTransferLib.sol (L24-25)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());
```

**File:** src/periphery/MidnightBundles.sol (L79-88)
```text
            try IMidnight(MIDNIGHT)
                .take(takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(0), address(0), "") returns (
                uint256 resBuyerAssets, uint256
            ) {
                filledUnits += unitsToTake;
                filledBuyerAssets += resBuyerAssets;
            } catch {}
        }

        require(filledUnits == targetUnits, OutOfOffers());
```

**File:** RESEARCHER.md (L14-14)
```markdown
- Permanent lock, freeze, or unrecoverable corruption of user/project state.
```
