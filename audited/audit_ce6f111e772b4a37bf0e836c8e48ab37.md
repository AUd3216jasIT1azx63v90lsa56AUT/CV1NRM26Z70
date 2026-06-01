Looking at the exact code path in `src/Midnight.sol` and `src/libraries/SafeTransferLib.sol`:

### Title
Fee-on-transfer loanToken inflates `claimableSettlementFee` beyond actual contract balance in `Midnight.take` - (File: `src/Midnight.sol`)

### Summary
`Midnight.take` increments `claimableSettlementFee` by the nominal `buyerAssets - sellerAssets` at line 418 before the actual token transfer at line 455. `SafeTransferLib.safeTransferFrom` only verifies the call's success/return value and never checks the actual received amount. When `loanToken` is a fee-on-transfer token, Midnight receives strictly less than the nominal fee, permanently inflating `claimableSettlementFee` beyond the actual token balance and breaking the solvency invariant.

### Finding Description
**Root cause — unsafe ordering + missing balance check:**

`src/Midnight.sol` line 418 writes accounting before the transfer:
```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
``` [1](#0-0) 

The actual pull of the fee portion happens later at line 455:
```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
``` [2](#0-1) 

`SafeTransferLib.safeTransferFrom` only checks `success` and the boolean return value — it performs no pre/post balance comparison: [3](#0-2) 

**Payer derivation for `offer.buy = false` (sell offer, taker is buyer):**

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;   // = takerCallback
address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
``` [4](#0-3) 

When the attacker supplies a non-zero `takerCallback`, `payer = takerCallback` — an attacker-controlled contract that holds fee-on-transfer tokens.

**Authorization path (precondition, not strictly required):**

`Midnight.take` line 346 allows any address authorized by the taker to call on their behalf:
```solidity
require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
``` [5](#0-4) 

`EcrecoverAuthorizer.setIsAuthorized` can establish `isAuthorized[victim][attacker] = true` given a valid EIP-712 signature from the victim: [6](#0-5) 

**Full exploit flow:**
1. `loanToken` is a fee-on-transfer token (e.g. 1% fee per transfer).
2. `isAuthorized[victim][attacker] = true` (victim signed an authorization).
3. Attacker calls `Midnight.take(sellOffer, ..., taker=victim, takerCallback=attackerContract)`.
4. `buyerCallback = attackerContract`, `payer = attackerContract`.
5. Line 418 executes: `claimableSettlementFee[loanToken] += buyerAssets - sellerAssets` (nominal, e.g. +100).
6. Line 455 executes: `safeTransferFrom(loanToken, attackerContract, midnight, 100)` → Midnight actually receives 99 (1% fee taken by token).
7. `claimableSettlementFee` records 100 but only 99 arrived; gap = 1 per call, accumulates over repeated calls.

**Note:** The authorization is not required for the core bug. Any taker calling `take` in a market whose `loanToken` is fee-on-transfer triggers the same discrepancy, because `payer` defaults to `msg.sender` when no callback is set.

**Existing protections are insufficient:** There is no post-transfer balance check anywhere in `take`, and `SafeTransferLib` is explicitly designed only to handle non-standard return values, not fee-on-transfer semantics. [7](#0-6) 

### Impact Explanation
`claimableSettlementFee[loanToken]` grows faster than `loanToken.balanceOf(address(midnight))`. After enough `take` calls the invariant:

```
claimableSettlementFee[loanToken] ≤ loanToken.balanceOf(midnight) − withdrawable − continuousFeeCredit
```

is violated. The fee claimer's `claimSettlementFee` call will revert (underflow on the token transfer) for the excess amount, and in the worst case the shortfall is covered by tokens that belong to lenders (`withdrawable`) or continuous-fee credit, making those claims unserviceable — protocol insolvency.

### Likelihood Explanation
**Preconditions:**
- A market must exist whose `loanToken` is a fee-on-transfer token (e.g. USDT with fee enabled, or any custom ERC-20 with transfer tax). This is a market-creation choice, not an admin action.
- A sell offer must exist in that market (normal protocol usage).
- For the authorization sub-path: victim must have signed an `EcrecoverAuthorizer` authorization for the attacker — requires victim cooperation. Without authorization, the attacker simply acts as the taker themselves, which is equally sufficient.

**Repeatability:** Every `take` call in such a market widens the gap. The attack is fully repeatable and the discrepancy compounds linearly.

### Recommendation
Replace the nominal accounting update with a post-transfer balance measurement:

```solidity
uint256 balanceBefore = IERC20(offer.market.loanToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
uint256 actualFeeReceived = IERC20(offer.market.loanToken).balanceOf(address(this)) - balanceBefore;
claimableSettlementFee[offer.market.loanToken] += actualFeeReceived;
```

Alternatively, explicitly document and enforce (e.g. via a market-creation check or NatSpec) that fee-on-transfer tokens are not supported as `loanToken`, and add an invariant test that asserts `claimableSettlementFee ≤ balanceOf(midnight) − withdrawable − continuousFeeCredit` after every `take`.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";

// 1% fee-on-transfer ERC-20
contract FeeToken is ERC20 {
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // recipient gets amount-fee
        _burn(from, fee);                           // fee destroyed
        return true;
    }
}

contract FeeOnTransferPoC is Test {
    Midnight midnight;
    FeeToken loanToken;

    function testClaimableSettlementFeeExceedsBalance() public {
        // Setup: create market with loanToken = FeeToken, settlement fee > 0
        // Create sell offer (offer.buy = false) from seller
        // Taker (buyer) calls take directly — no authorization needed

        uint256 balBefore = loanToken.balanceOf(address(midnight));
        uint256 claimBefore = midnight.claimableSettlementFee(address(loanToken));

        // take() with units > 0, settlement fee > 0
        midnight.take(sellOffer, "", units, buyer, receiver, address(0), "");

        uint256 balAfter = loanToken.balanceOf(address(midnight));
        uint256 claimAfter = midnight.claimableSettlementFee(address(loanToken));

        uint256 nominalFee = claimAfter - claimBefore;
        uint256 actualReceived = balAfter - balBefore;

        // Core assertion: accounting exceeds actual balance increase
        assertGt(nominalFee, actualReceived, "claimableSettlementFee inflated");

        // Solvency invariant assertion
        uint256 withdrawable = midnight.withdrawable(id);
        uint256 continuousFeeCredit = midnight.continuousFeeCredit(id);
        assertGt(
            claimAfter,
            loanToken.balanceOf(address(midnight)) - withdrawable - continuousFeeCredit,
            "insolvency: claimable fee exceeds available balance"
        );
    }
}
```

### Citations

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L420-422)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/libraries/SafeTransferLib.sol (L7-34)
```text
library SafeTransferLib {
    error NoCode();
    error TransferFromReturnedFalse();
    error TransferReturnedFalse();

    function safeTransfer(address token, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transfer, (to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferReturnedFalse());
    }

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

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-47)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );

        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );

        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```
