Based on my investigation of the codebase, all critical code paths check out exactly as claimed.

**Verified facts:**

- [1](#0-0)  — `claimableSettlementFee` is incremented with the full nominal `buyerAssets - sellerAssets` before any transfer.
- [2](#0-1)  — The actual `safeTransferFrom` calls happen after the accounting write.
- [3](#0-2)  — `safeTransferFrom` only checks call success and return value; no balance snapshot is taken.
- [4](#0-3)  — The Certora spec explicitly assumes no fee-on-transfer behavior; `CVL_transferFrom` credits the full `value` to `dest` unconditionally.
- [5](#0-4)  — The ghost function confirms the formal proof does not model fee deductions.
- `SECURITY.md` and `RESEARCHER.md` contain no exclusion for fee-on-transfer token scenarios. [6](#0-5) 

---

Audit Report

## Title
Fee-on-Transfer `loanToken` Causes `claimableSettlementFee` Overcount, Breaking Solvency Invariant - (File: src/Midnight.sol)

## Summary
In `take()`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the full nominal spread (`buyerAssets - sellerAssets`) before the inbound transfer executes. When `loanToken` is a fee-on-transfer ERC20, Midnight receives fewer tokens than recorded, creating a persistent accounting shortfall. The Certora solvency proof explicitly excludes this case via its `CVL_transferFrom` ghost assumption.

## Finding Description
**Root cause:** `src/Midnight.sol` line 418 writes the full nominal fee to `claimableSettlementFee` before any transfer:

```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

Lines 455–456 then execute the transfers via `SafeTransferLib.safeTransferFrom`, which (lines 24–34 of `src/libraries/SafeTransferLib.sol`) only verifies call success and the boolean return value — no balance-before/after snapshot is taken. A fee-on-transfer token silently delivers fewer tokens than requested, and the library cannot detect this.

**Exploit flow:**
1. Deploy a fee-on-transfer ERC20 (e.g., 1% fee on every `transferFrom`).
2. Call `touchMarket` with `market.loanToken = feeToken` (permissionless).
3. Sign a buy offer via `EcrecoverRatifier` committing to `market.loanToken`.
4. Call `take()`:
   - `claimableSettlementFee[feeToken] += (buyerAssets - sellerAssets)` — full nominal amount recorded.
   - `safeTransferFrom(feeToken, payer, address(this), buyerAssets - sellerAssets)` — Midnight receives only `(buyerAssets - sellerAssets) * (1 - fee_rate)`.
5. Post-call: `claimableSettlementFee[feeToken] > feeToken.balanceOf(address(midnight))` for the settlement-fee portion.

**Why existing checks fail:**
- `SafeTransferLib` performs no received-amount verification.
- No token whitelist or fee-on-transfer guard exists in `take()` or `touchMarket`.
- The Certora `Solvency.spec` `tokenBalanceCorrect` invariant is proven only under the explicit assumption at line 31: *"Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver"*. The `CVL_transferFrom` ghost credits the full `value` to `dest` regardless of actual fees.

## Impact Explanation
`claimableSettlementFee[feeToken]` accumulates a nominal value exceeding tokens actually held by Midnight for that purpose. When `feeClaimer` calls `claimSettlementFee`, the subsequent `safeTransfer` pulls tokens from Midnight's balance that belong to other obligations (lender withdrawable credit, collateral, other fee accumulations). Repeated `take()` calls amplify the shortfall linearly, enabling the feeClaimer to drain more tokens than legitimately held for settlement fees, directly causing insolvency for lenders attempting to `withdraw`.

## Likelihood Explanation
Market creation via `touchMarket` is fully permissionless — any unprivileged user can create a market with a fee-on-transfer `loanToken`. The market creator and offer maker can be the same address. Fee-on-transfer tokens exist in production. No admin action is required; the shortfall grows cumulatively with every `take()` call on the affected market.

## Recommendation
Measure the actual received amount using a balance snapshot around the inbound transfer:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += received;
```

Alternatively, document fee-on-transfer tokens as explicitly unsupported and add a guard in `touchMarket` (e.g., a round-trip transfer check) to reject such tokens at market creation time.

## Proof of Concept
1. Deploy `FeeToken` (ERC20 with 1% fee on `transferFrom`).
2. Call `midnight.touchMarket(market)` where `market.loanToken = address(feeToken)`.
3. Sign a buy offer; call `midnight.take(offer, ...)` with `buyerAssets - sellerAssets = 1000`.
4. Assert: `midnight.claimableSettlementFee(address(feeToken)) == 1000` but `feeToken.balanceOf(address(midnight)) == 990`.
5. Repeat N times; shortfall grows to `10 * N`.
6. Call `claimSettlementFee(feeToken, 1000 * N, receiver)` — the transfer succeeds by pulling tokens belonging to lenders, demonstrating insolvency.

### Citations

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
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

**File:** certora/specs/Solvency.spec (L43-58)
```text
function CVL_transferFrom(env e, address token, address src, address dest, uint256 value) returns bool {
    if (tokenBalances[token][src] < value || tokenBalances[token][dest] + value >= 2 ^ 256) {
        revert();
    }

    // Non-deterministically set success, which allows to simulate permissions.
    bool success;
    if (success) {
        tokenBalances[token][src] = assert_uint256(tokenBalances[token][src] - value);
        tokenBalances[token][dest] = assert_uint256(tokenBalances[token][dest] + value);
    
        // Settle pending settlement fee receipts only on the exact fee transfer expected by take().
        if (dest == currentContract && pendingFeeReceipt[token] == to_mathint(value)) {
            pendingFeeReceipt[token] = 0;
        }
    }
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
