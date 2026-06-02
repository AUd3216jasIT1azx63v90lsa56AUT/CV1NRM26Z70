Audit Report

## Title
Fee-on-Transfer Loan Token Inflates `claimableSettlementFee` Beyond Actual Contract Receipts - (File: src/Midnight.sol / src/libraries/SafeTransferLib.sol)

## Summary
In `Midnight.sol`'s `take` function, `claimableSettlementFee[offer.market.loanToken]` is incremented by the nominal `buyerAssets - sellerAssets` before the inbound transfer executes. `SafeTransferLib.safeTransferFrom` performs no balance-before/balance-after check. When the market's `loanToken` is a fee-on-transfer token, the contract receives strictly less than the nominal amount, permanently inflating `claimableSettlementFee` relative to actual holdings. Subsequent `claimSettlementFee` calls will attempt to transfer the inflated amount, either reverting (DoS) or silently draining lender-withdrawable funds co-mingled in the same ERC-20 balance.

## Finding Description

**Confirmed code path:**

1. `src/Midnight.sol` line 418 — state written unconditionally before any external call: [1](#0-0) 

2. `src/Midnight.sol` line 455 — inbound transfer pulls `buyerAssets - sellerAssets` from `payer` after state update: [2](#0-1) 

3. `src/libraries/SafeTransferLib.sol` lines 27–33 — only checks revert propagation and boolean return; no balance delta verification: [3](#0-2) 

4. `src/Midnight.sol` lines 305–309 — `claimSettlementFee` transfers the full recorded amount directly from contract balance: [4](#0-3) 

**Root cause:** `safeTransferFrom` does not verify the actual amount received by `address(this)`. A fee-on-transfer token deducts a fee silently while returning `true`, so the call succeeds but the contract receives `(buyerAssets - sellerAssets) * (1 - fee_rate)`. Because `claimableSettlementFee` was already incremented by the full nominal amount at line 418, the accounting is permanently wrong by `(buyerAssets - sellerAssets) * fee_rate` per `take`.

**Formal verification explicitly excludes this case.** `certora/specs/Solvency.spec` line 31 states the ERC-20 summary assumes no fee-taking, and the `pendingFeeReceipt` ghost mechanism (lines 140–151) clears only when the exact nominal amount is received — a condition that is never met with fee-on-transfer tokens: [5](#0-4) [6](#0-5) 

The `tokenBalanceCorrect` strong invariant is therefore not proven for fee-on-transfer tokens: [7](#0-6) 

**Why existing checks fail:** The only checks on the transfer are revert-propagation and the boolean return value. There is no `balanceOf(address(this))` snapshot before/after the transfer. The `claimableSettlementFee` increment at line 418 is unconditional and precedes the transfer.

## Impact Explanation

After each such `take`, `claimableSettlementFee[loanToken]` exceeds the contract's actual token balance attributable to settlement fees by `(buyerAssets - sellerAssets) * fee_rate`. When the protocol owner calls `claimSettlementFee`, it will attempt to transfer more tokens than the contract holds for that purpose. Since all loan token balances (withdrawable lender assets, settlement fees) are co-mingled in the same ERC-20 balance, the shortfall is covered by lender-withdrawable funds — constituting direct theft of lender assets. Alternatively, if the balance is insufficient, `claimSettlementFee` reverts, causing a DoS on fee collection. This violates the core solvency invariant and matches the `live_context.json` best bug classes of "protocol insolvency" and "credit/debt accounting corruption." [8](#0-7) 

## Likelihood Explanation

**Preconditions:**

1. A market exists whose `loanToken` is a fee-on-transfer token. Market creation is permissionless and accepts an arbitrary loan token with no type restriction: [9](#0-8) [10](#0-9) 

2. `live_context.json` line 389 explicitly lists "token charges fee" as an external behavior to test, confirming it is in scope: [11](#0-10) 

3. `SECURITY.md` contains no exclusion for fee-on-transfer or non-standard tokens: [12](#0-11) 

Both preconditions are fully attacker-controllable. No malicious token owner is required — the fee is a built-in property of the token. The attack is repeatable on every `take` in such a market, compounding the discrepancy linearly. No privileged access is required.

## Recommendation

Replace the unconditional nominal increment with a balance-snapshot approach: record `balanceOf(address(this))` before the `safeTransferFrom` call and increment `claimableSettlementFee` by the actual delta (`balanceAfter - balanceBefore`) rather than the nominal `buyerAssets - sellerAssets`. Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as loan tokens (e.g., via a protocol-level allowlist or a check in `touchMarket`).

## Proof of Concept

**Minimal Foundry fork test plan:**

1. Deploy a mock ERC-20 with a 1% transfer fee (deducted from the received amount).
2. Create a market with this token as `loanToken` (permissionless via `touchMarket`).
3. Set a non-zero settlement fee via `setDefaultSettlementFee`.
4. Execute a `take` with `units > 0` such that `buyerAssets - sellerAssets > 0`.
5. Assert: `midnight.claimableSettlementFee(feeToken) > feeToken.balanceOf(address(midnight))` — the recorded fee exceeds the actual balance increase.
6. Call `claimSettlementFee` for the full `claimableSettlementFee` amount and observe either revert (if no other balance) or drainage of lender-withdrawable funds (if lender deposits exist).

The discrepancy equals `(buyerAssets - sellerAssets) * fee_rate` per `take` and accumulates linearly across repeated calls.

### Citations

**File:** src/Midnight.sol (L305-310)
```text
    function claimSettlementFee(address token, uint256 amount, address receiver) external {
        require(msg.sender == feeClaimer, OnlyFeeClaimer());
        claimableSettlementFee[token] -= amount;
        emit EventsLib.ClaimSettlementFee(msg.sender, token, amount, receiver);
        SafeTransferLib.safeTransfer(token, receiver, amount);
    }
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

**File:** src/libraries/SafeTransferLib.sol (L27-33)
```text
        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
```

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```

**File:** certora/specs/Solvency.spec (L140-151)
```text
// Settlement fee receipts pending settlement: claimableSettlementFee is incremented in take before
// the inbound fee transfer happens, so we track the gap and clear it in CVL_transferFrom.
persistent ghost mapping(address => mathint) pendingFeeReceipt {
    init_state axiom (forall address token. pendingFeeReceipt[token] == 0);
}

hook Sstore claimableSettlementFee[KEY address token] uint256 newVal (uint256 oldVal) {
    // Except for claimSettlementFee, the claimableSettlementFee is non-decreasing, see WithdrawableMonotonicity.spec.
    if (newVal > oldVal) {
        pendingFeeReceipt[token] = pendingFeeReceipt[token] + newVal - oldVal;
    }
}
```

**File:** certora/specs/Solvency.spec (L162-163)
```text
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
```

**File:** live_context.json (L15-17)
```json
      "permissionless market creation",
      "fixed maturity per market",
      "arbitrary loan token",
```

**File:** live_context.json (L53-66)
```json
    "best_bug_classes": [
      "direct loss of user funds",
      "protocol insolvency",
      "bad debt creation",
      "unauthorized collateral withdrawal",
      "unauthorized collateral seizure",
      "permanent or long-term fund freeze",
      "liquidation bypass",
      "healthy-account liquidation",
      "offer replay or overfill",
      "gate or ratifier bypass",
      "credit/debt accounting corruption",
      "callback or multicall state corruption"
    ]
```

**File:** live_context.json (L385-394)
```json
    "external_behavior": [
      "callback reverts",
      "callback reenters",
      "token returns false",
      "token charges fee",
      "token rebases",
      "token has 6/8/18/27 decimals",
      "receiver is contract",
      "payer is different from msg.sender"
    ]
```

**File:** SECURITY.md (L18-26)
```markdown
### Smart Contracts / Blockchain DLT

- Incorrect data supplied by third-party oracles.
- Impacts requiring basic economic and governance attacks (e.g. 51% attack).
- Lack of liquidity impacts.
- Impacts from Sybil attacks.
- Impacts involving centralization risks.

Note: This does not exclude oracle manipulation/flash-loan attacks.
```
