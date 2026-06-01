### Title
Fee-on-Transfer Collateral Token Inflates Accounting, Causing Partial DoS on `withdrawCollateral` - (File: src/Midnight.sol)

### Summary
`supplyCollateral` credits `position[id][onBehalf].collateral[collateralIndex]` with the full `assets` argument before calling `safeTransferFrom`, but a fee-on-transfer collateral token causes Midnight to receive only `assets * (1 - fee_rate)` tokens. When the borrower later calls `withdrawCollateral(assets)`, `safeTransfer` attempts to send the full recorded `assets` amount, which exceeds the actual contract balance, causing a revert. The accounting overcount is permanent and unrecoverable for the fee portion.

### Finding Description
**Exact code path:**

`supplyCollateral` at [1](#0-0)  writes `_position.collateral[collateralIndex] = oldCollateral + assets` (line 533) **before** the external call `SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets)` (line 545). With a fee-on-transfer token charging rate `f`, Midnight receives `assets * (1 - f)` but records `assets`.

`withdrawCollateral` at [2](#0-1)  decrements accounting by `assets` (line 562) and then calls `SafeTransferLib.safeTransfer(collateralToken, receiver, assets)` (line 572). If `assets` equals the full recorded balance, the transfer requests more tokens than Midnight holds, causing a revert.

**Why no existing check stops it:**

`touchMarket` at [3](#0-2)  validates only maturity, collateral count, sort order, LLTV tier, and maxLif. There is no check that the collateral token is not fee-on-transfer. Market creation is permissionless — any address can call `touchMarket` (or trigger it implicitly via `supplyCollateral`) with any ERC20 as collateral.

The Certora solvency spec at [4](#0-3)  explicitly assumes "no fee taking from sender or receiver" as a formal verification precondition, but this assumption is **not enforced on-chain** anywhere in the protocol.

**Exploit flow:**
1. Attacker deploys `FeeCollateral` ERC20 with 0.5% transfer fee.
2. Attacker (as borrower) calls `supplyCollateral(market, 0, 1000, borrower)`:
   - Line 533: `_position.collateral[0]` → 1000 (accounting).
   - Line 545: `safeTransferFrom` transfers 1000 from borrower; Midnight receives 995.
3. Attacker calls `withdrawCollateral(market, 0, 1000, borrower, receiver)`:
   - Line 562: accounting decremented to 0.
   - Line 572: `safeTransfer(collateralToken, receiver, 1000)` — Midnight holds only 995 → **revert**.
4. Borrower can withdraw at most 995 (the actual balance), but accounting permanently records 5 as outstanding collateral with no backing tokens.

### Impact Explanation
The borrower cannot withdraw the full amount recorded in their accounting. The fee-inflated portion (5 tokens in the example) is permanently frozen: it exists in `position[id][borrower].collateral[collateralIndex]` but has no corresponding token balance in the contract. Additionally, the inflated collateral accounting allows the borrower to borrow against phantom collateral, violating the core solvency invariant that contract token balances must cover all collateral claims.

### Likelihood Explanation
`touchMarket` is permissionless and imposes no token-type restrictions. Any borrower can create a market with a fee-on-transfer collateral token and trigger this state. The precondition is solely that the collateral token charges a transfer fee — a common pattern (e.g., USDT with fees enabled, STA, PAXG). The condition is deterministic and repeatable: every `supplyCollateral` call with such a token inflates accounting by exactly the fee amount.

### Recommendation
Measure the actual received amount by comparing the contract's token balance before and after `safeTransferFrom`, and credit only the delta:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Alternatively, explicitly disallow fee-on-transfer tokens by adding a balance-check assertion in `touchMarket` or `supplyCollateral` and reverting if `received != assets`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {ERC20} from "solmate/tokens/ERC20.sol";

// Fee-on-transfer token: deducts 0.5% on every transfer
contract FeeCollateral is ERC20("FeeCol", "FC", 18) {
    uint256 public constant FEE_BPS = 50; // 0.5%
    function mint(address to, uint256 amount) external { _mint(to, amount); }
    function transfer(address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * FEE_BPS / 10_000;
        return super.transfer(to, amount - fee);
    }
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * FEE_BPS / 10_000;
        return super.transferFrom(from, to, amount - fee);
    }
}

contract FeeOnTransferCollateralTest is Test {
    // ... standard Midnight test setup ...

    function testFeeOnTransferCollateralDoS() public {
        FeeCollateral feeToken = new FeeCollateral();
        // Build market with feeToken as collateral (valid LLTV, maxLif, sorted)
        Market memory market = _buildMarket(address(feeToken));
        
        address borrower = makeAddr("borrower");
        uint256 supplyAmount = 1000e18;
        
        feeToken.mint(borrower, supplyAmount);
        vm.prank(borrower);
        feeToken.approve(address(midnight), supplyAmount);
        
        vm.prank(borrower);
        midnight.supplyCollateral(market, 0, supplyAmount, borrower);
        
        // Accounting records full supplyAmount
        assertEq(midnight.collateral(toId(market), borrower, 0), supplyAmount);
        
        // Midnight actually received only 995e18 (0.5% fee deducted)
        uint256 actualReceived = feeToken.balanceOf(address(midnight));
        assertLt(actualReceived, supplyAmount); // 995e18 < 1000e18
        
        // Attempting to withdraw the full recorded amount reverts
        vm.prank(borrower);
        vm.expectRevert(); // safeTransfer fails: insufficient balance
        midnight.withdrawCollateral(market, 0, supplyAmount, borrower, borrower);
        
        // Borrower can only withdraw up to actualReceived
        vm.prank(borrower);
        midnight.withdrawCollateral(market, 0, actualReceived, borrower, borrower);
        
        // 5e18 worth of accounting is permanently stuck with no backing tokens
        assertEq(midnight.collateral(toId(market), borrower, 0), supplyAmount - actualReceived);
        assertEq(feeToken.balanceOf(address(midnight)), 0);
    }
}
```

**Expected assertions:**
- `midnight.collateral(...) == 1000e18` after supply (inflated accounting). [5](#0-4) 
- `feeToken.balanceOf(midnight) == 995e18` (actual received). [6](#0-5) 
- `withdrawCollateral(1000e18)` reverts. [7](#0-6) 
- After withdrawing 995e18, `midnight.collateral(...) == 5e18` with `feeToken.balanceOf(midnight) == 0` — phantom accounting confirmed. [8](#0-7)

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

**File:** certora/specs/Solvency.spec (L31-33)
```text
    // Assume ERC20 tokens transfer correctly: no fee taking from sender or receiver, no rebasing, no blacklisting, no transfer limits.
    function _.transfer(address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, e.msg.sender, a, v) expect(bool);
    function _.transferFrom(address src, address a, uint256 v) external with(env e) => CVL_transferFrom(e, calledContract, src, a, v) expect(bool);
```
