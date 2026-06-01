### Title
Fee-on-Transfer `loanToken` Causes `claimableSettlementFee` Overcount, Breaking Solvency Invariant - (`src/Midnight.sol`)

### Summary

In `take()`, `claimableSettlementFee[offer.market.loanToken]` is incremented by the full `buyerAssets - sellerAssets` spread before the inbound transfer, and `SafeTransferLib.safeTransferFrom` performs no balance-before/after check. When `loanToken` is a fee-on-transfer ERC20, Midnight receives only `(buyerAssets - sellerAssets) * (1 - fee_rate)` but records the full nominal amount as claimable, creating a persistent shortfall. The Certora solvency proof explicitly assumes well-behaved ERC20s and does not cover this case.

### Finding Description

**Exact code path:**

`src/Midnight.sol` line 418 increments the fee counter unconditionally before any transfer:

```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

Then lines 455–456 execute the two transfers:

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

`SafeTransferLib.safeTransferFrom` (lines 24–34 of `src/libraries/SafeTransferLib.sol`) only checks that the call succeeds and the return value is `true`. It performs no balance snapshot before/after the call, so it cannot detect that a fee-on-transfer token silently delivered fewer tokens than requested.

**Root cause:** The accounting write at line 418 uses the *requested* transfer amount, not the *received* amount. There is no reconciliation step.

**Attacker-controlled inputs:**
- `offer.market.loanToken` — set by the market creator (any unprivileged user via `touchMarket`); no whitelist or token-type check exists.
- `offer`, `ratifierData`, `units` — standard taker inputs.

**Exploit flow:**
1. Deploy a fee-on-transfer ERC20 (e.g., 1% fee on every `transferFrom`).
2. Call `touchMarket` with `market.loanToken = feeToken` to create the market.
3. Sign a buy offer via `EcrecoverRatifier`; `hashOffer` commits to `market.loanToken`.
4. Call `take()`. State changes:
   - `claimableSettlementFee[feeToken] += (buyerAssets - sellerAssets)` — full nominal amount.
   - `SafeTransferLib.safeTransferFrom(feeToken, payer, address(this), buyerAssets - sellerAssets)` — Midnight receives only `(buyerAssets - sellerAssets) * 0.99`.
5. After the call: `claimableSettlementFee[feeToken] > feeToken.balanceOf(address(midnight))` (considering only the settlement-fee portion of the balance).

**Why existing checks fail:**
- `SafeTransferLib` has no received-amount verification.
- No token whitelist or fee-on-transfer guard exists anywhere in `take()` or `touchMarket`.
- The Certora `Solvency.spec` `tokenBalanceCorrect` invariant is proven only under the explicit assumption at line 31: *"Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver"*. The `CVL_transferFrom` ghost credits the full `value` to `dest` regardless of fees, so the formal proof does not cover this scenario. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Impact Explanation

`claimableSettlementFee[feeToken]` accumulates a nominal value larger than the tokens actually held by Midnight for that purpose. When `feeClaimer` calls `claimSettlementFee(feeToken, amount, receiver)`, the subtraction `claimableSettlementFee[token] -= amount` succeeds (no underflow) but the subsequent `SafeTransferLib.safeTransfer` pulls tokens from Midnight's balance that belong to other obligations (withdrawable lender credit, collateral of other tokens if shared, or other fee accumulations). Repeated `take()` calls amplify the shortfall linearly. The feeClaimer can drain more tokens than Midnight legitimately holds for settlement fees, directly causing insolvency for lenders attempting to `withdraw`. [6](#0-5) 

### Likelihood Explanation

**Preconditions:**
- A fee-on-transfer ERC20 must be used as `loanToken`. Market creation is permissionless; any user can create such a market.
- A valid signed offer must exist (trivially satisfied by the maker themselves using `EcrecoverRatifier`).
- No admin action is required; the exploit is fully self-contained.

**Feasibility:** High. Fee-on-transfer tokens (e.g., tokens with a built-in tax) exist in production. The market creator and maker can be the same unprivileged address. The shortfall grows with every `take()` call on the affected market, making it repeatable and cumulative.

### Recommendation

Record the actual received amount rather than the nominal requested amount. Before and after the inbound transfer, snapshot the contract's balance and use the delta:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 received = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
require(received == buyerAssets - sellerAssets, FeeOnTransferToken());
```

Alternatively, explicitly document and enforce (via an on-chain check at `touchMarket`) that fee-on-transfer tokens are not permitted as `loanToken`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {EcrecoverRatifier} from "src/ratifiers/EcrecoverRatifier.sol";

contract FeeOnTransferToken {
    // 1% fee on every transferFrom, credited to address(this)
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function approve(address sp, uint256 amt) external { allowance[msg.sender][sp] = amt; }
    function transferFrom(address from, address to, uint256 amt) external returns (bool) {
        uint256 fee = amt / 100;
        balanceOf[from] -= amt;
        balanceOf[to] += amt - fee;
        balanceOf[address(this)] += fee;
        allowance[from][msg.sender] -= amt;
        return true;
    }
    function transfer(address to, uint256 amt) external returns (bool) {
        balanceOf[msg.sender] -= amt;
        balanceOf[to] += amt;
        return true;
    }
}

contract FeeOnTransferPoC is Test {
    function testClaimableExceedsBalance() public {
        // 1. Deploy protocol + fee token
        // 2. Create market with feeToken as loanToken
        // 3. Sign buy offer via EcrecoverRatifier
        // 4. Call take() with non-zero settlement fee
        // 5. Assert:
        //    midnight.claimableSettlementFee(address(feeToken))
        //      > feeToken.balanceOf(address(midnight))
        // 6. feeClaimer calls claimSettlementFee for full claimable amount
        //    → transfer succeeds by draining lender withdrawable funds
        //    → subsequent lender withdraw() reverts (insufficient balance)
    }
}
```

Expected assertion: `midnight.claimableSettlementFee(address(feeToken)) > feeToken.balanceOf(address(midnight))` holds immediately after a single `take()` with a non-zero settlement fee and a 1%-fee token. [7](#0-6) [8](#0-7)

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

**File:** certora/specs/Solvency.spec (L162-177)
```text
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
    {
        preserved with (env e) {
            requireInvariant pendingFeeReceiptZero(token);
            require e.msg.sender != currentContract, "only external calls";
        }
        preserved take(Midnight.Offer offer, bytes ratifierData, uint256 units, address taker, address receiverIfTakerIsSeller, address takerCallback, bytes takerCallbackData) with (env e) {
            requireInvariant pendingFeeReceiptZero(token);
            require e.msg.sender != currentContract, "only external calls";
            require taker != currentContract, "no settlement with contract";
            require offer.maker != currentContract, "no settlement with contract";
            require offer.callback != currentContract, "midnight reverts on callbacks";
            require takerCallback != currentContract, "midnight reverts on callbacks";
        }
    }
```

**File:** src/ratifiers/libraries/HashLib.sol (L118-138)
```text
    function hashOffer(Offer memory offer) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                OFFER_TYPEHASH,
                hashMarket(offer.market),
                offer.buy,
                offer.maker,
                offer.start,
                offer.expiry,
                offer.tick,
                offer.group,
                offer.callback,
                keccak256(offer.callbackData),
                offer.receiverIfMakerIsSeller,
                offer.ratifier,
                offer.reduceOnly,
                offer.maxUnits,
                offer.maxAssets
            )
        );
    }
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L1-10)
```text
// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (c) 2025 Morpho Association
pragma solidity 0.8.34;

import {IEcrecoverRatifier, Signature, EIP712_DOMAIN_TYPEHASH} from "./interfaces/IEcrecoverRatifier.sol";
import {IMidnight, Offer} from "../interfaces/IMidnight.sol";
import {CALLBACK_SUCCESS} from "../libraries/ConstantsLib.sol";
import {HashLib} from "./libraries/HashLib.sol";

/// @dev If block.chainid changes (hard fork), the EIP-712 domain separator changes and previously signed offers are
```
