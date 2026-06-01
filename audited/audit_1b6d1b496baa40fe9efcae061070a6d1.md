The critical evidence is now assembled. Let me verify the Solvency spec assumption and the live_context note about fee-on-transfer tokens.

### Title
Fee-on-transfer loan token causes seller credit destruction without full token delivery in `take()` - (File: `src/libraries/SafeTransferLib.sol`)

### Summary
`SafeTransferLib.safeTransferFrom` only validates the boolean return value of `transferFrom` and does not verify the actual amount received by the destination. When a market's `loanToken` is a fee-on-transfer token, `take()` applies `sellerPos.credit -= sellerCreditDecrease` and `claimableSettlementFee += buyerAssets - sellerAssets` in full before the transfers execute, but the seller's receiver and the contract each receive less than the accounting records, permanently destroying credit value without corresponding token delivery and breaking the solvency invariant.

### Finding Description
**Exact code path:**

In `src/Midnight.sol`, `take()` with `offer.buy == false` (sell offer):

- Line 375: `seller = offer.maker`, `buyer = taker`
- Line 422: `payer = msg.sender` (the taker, when `buyerCallback == address(0)`)
- Line 423: `receiver = offer.receiverIfMakerIsSeller`

State mutations are committed unconditionally before any transfer:

```solidity
// src/Midnight.sol:412-414
sellerPos.pendingFee -= sellerPendingFeeDecrease;
sellerPos.credit     -= UtilsLib.toUint128(sellerCreditDecrease);  // full debit
sellerPos.debt       += UtilsLib.toUint128(sellerDebtIncrease);    // full increase
```

```solidity
// src/Midnight.sol:418
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets; // full credit
```

Then the transfers execute:

```solidity
// src/Midnight.sol:455-456
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver,      sellerAssets);
```

`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol:24-34`) calls `transferFrom` and checks only the boolean return value:

```solidity
require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
```

There is no balance-before/after check. For a fee-on-transfer token with fee rate `f`:
- Contract receives `(buyerAssets - sellerAssets) × (1 - f)` but `claimableSettlementFee` was increased by the full `buyerAssets - sellerAssets`
- Seller's receiver receives `sellerAssets × (1 - f)` but `sellerPos.credit` was reduced by the full `sellerCreditDecrease`

**Attacker-controlled inputs:**
A market creator (unprivileged role) calls `touchMarket` with `market.loanToken` set to a fee-on-transfer ERC20. `touchMarket` (`src/Midnight.sol:755-791`) has no restriction on the loan token type. Any taker who subsequently calls `take()` on a sell offer in that market triggers the mismatch.

**Why existing checks fail:**
The Certora Solvency spec (`certora/specs/Solvency.spec:31`) explicitly assumes "no fee taking from sender or receiver" — this is a verification assumption, not a protocol enforcement. The `live_context.json:233` flags "fee-on-transfer... should be tested if not explicitly excluded" as an open concern, and there is no explicit exclusion anywhere in the contract code. No balance-delta check exists in `SafeTransferLib` or in `take()`.

### Impact Explanation
Every time `take()` is called on a sell offer in a fee-on-transfer loan token market:
1. The seller's credit is reduced by the full `sellerCreditDecrease` but their receiver receives only `sellerAssets × (1 - f)` tokens — the difference `sellerAssets × f` is permanently lost to the token's fee mechanism.
2. `claimableSettlementFee` is overstated by `(buyerAssets - sellerAssets) × f`, meaning the fee claimer will eventually be unable to claim the full recorded amount, breaking the core solvency invariant: "contract token balances must cover withdrawable assets, collateral claims, credit redemptions, and accrued fees."

### Likelihood Explanation
Preconditions: (1) a market exists with a fee-on-transfer loan token — any unprivileged address can create such a market via `touchMarket`; (2) a sell offer exists on that market; (3) a taker calls `take()`. No special privilege is required beyond market creation. The issue is repeatable on every `take()` call in such a market and scales linearly with volume.

### Recommendation
Add a balance-before/after check around each `safeTransferFrom` call in `take()` to verify the actual received amount matches the expected amount, and revert if it does not. Alternatively, explicitly document and enforce (via a registry or allowlist) that only standard ERC20 tokens with no transfer fees may be used as loan tokens, and add a check in `touchMarket` or at the point of first use.

### Proof of Concept
```solidity
// Foundry differential fuzz test
contract FeeOnTransferLoanTokenTest is Test {
    // Deploy FeeToken: 1% fee on every transferFrom, deducted from recipient
    // Deploy Midnight, create market with FeeToken as loanToken
    // Setup: seller (offer.maker) has credit = C via prior supply
    // Setup: taker has FeeToken balance >= buyerAssets

    function testSellerCreditDestroyedWithoutFullDelivery(uint256 units) public {
        // Arrange
        uint256 creditBefore   = midnight.creditOf(id, seller);
        uint256 balanceBefore  = feeToken.balanceOf(sellerReceiver);
        uint256 contractBefore = feeToken.balanceOf(address(midnight));

        // Act: taker fills sell offer
        vm.prank(taker);
        (uint256 buyerAssets, uint256 sellerAssets) = midnight.take(sellOffer, ...);

        // Assert: seller credit reduced by full sellerCreditDecrease
        uint256 creditAfter  = midnight.creditOf(id, seller);
        uint256 creditDelta  = creditBefore - creditAfter; // == sellerCreditDecrease

        // Assert: seller received less than sellerAssets (fee deducted)
        uint256 balanceDelta = feeToken.balanceOf(sellerReceiver) - balanceBefore;
        assertLt(balanceDelta, sellerAssets, "seller received less than sellerAssets");

        // Assert: contract received less than (buyerAssets - sellerAssets)
        uint256 contractDelta = feeToken.balanceOf(address(midnight)) - contractBefore;
        assertLt(contractDelta, buyerAssets - sellerAssets, "contract underfunded vs claimableSettlementFee");

        // Assert: claimableSettlementFee overstated vs actual balance increase
        assertGt(
            midnight.claimableSettlementFee(address(feeToken)),
            contractDelta,
            "solvency invariant broken: claimableSettlementFee > actual balance"
        );
    }
}
```

Expected assertions all pass: seller's token balance delta is strictly less than `sellerAssets`; contract balance delta is strictly less than `buyerAssets - sellerAssets`; `claimableSettlementFee` exceeds the contract's actual token balance increase. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L412-418)
```text
        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);

        _marketState.totalUnits =
            UtilsLib.toUint128(_marketState.totalUnits + buyerCreditIncrease - sellerCreditDecrease);
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

**File:** src/libraries/SafeTransferLib.sol (L24-34)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
    }
```

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```

**File:** live_context.json (L232-233)
```json
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
```
