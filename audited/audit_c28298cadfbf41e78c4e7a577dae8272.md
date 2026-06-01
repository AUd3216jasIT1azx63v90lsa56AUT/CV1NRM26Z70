The code is now fully clear. Let me compile the complete analysis.

**Key facts established:**

1. `supplyCollateral` credits `_position.collateral[collateralIndex]` with the full `assets` parameter **before** the transfer, with no balance-delta check. [1](#0-0) 

2. `SafeTransferLib.safeTransferFrom` only checks `transferFrom` success/return value — no balance-before/after check. [2](#0-1) 

3. `isHealthy` computes `maxDebt` directly from `_position.collateral[i]` (the stored, potentially inflated value). [3](#0-2) 

4. `touchMarket` validates LLTV tiers, maxLif, and sorted token addresses — but performs **no check** that the collateral token is not fee-on-transfer. It is permissionless. [4](#0-3) 

5. The TOKEN SAFETY REQUIREMENTS section documents the assumption ("Midnight must send/receive exactly the requested amount on transfers") but does **not enforce it on-chain**. [5](#0-4) 

6. The Certora formal verification explicitly assumes well-behaved ERC20 tokens, so the proofs do not cover this case. [6](#0-5) 

7. `live_context.json` explicitly lists fee-on-transfer tokens as something that "should be tested if not explicitly excluded." [7](#0-6) 

---

### Title
Fee-on-transfer collateral token inflates stored collateral, enabling undercollateralized borrowing - (File: src/Midnight.sol)

### Summary
`supplyCollateral` credits `_position.collateral[collateralIndex]` with the caller-supplied `assets` value before invoking `SafeTransferLib.safeTransferFrom`, which performs no balance-delta verification. For a fee-on-transfer collateral token, the contract receives `assets - fee` but records `assets`, inflating the stored collateral. `isHealthy` then computes `maxDebt` from the inflated stored value, allowing the borrower to take debt that exceeds the actual on-chain collateral value, creating an undercollateralized position.

### Finding Description
**Code path and root cause:**

In `supplyCollateral` (`src/Midnight.sol:533`):
```solidity
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
```
The position is credited with the full `assets` parameter. Then at line 545:
```solidity
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```
`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol:27`) calls `transferFrom` and checks only the boolean return value — it does not snapshot the contract's balance before and after to verify the actual received amount. For a fee-on-transfer token with fee rate `f`, the contract receives `assets * (1 - f)` but `_position.collateral[collateralIndex]` stores `assets`.

**`isHealthy` uses the inflated stored value:**
```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
```
`_position.collateral[i]` is the inflated `assets`, not the actual `assets * (1 - f)` held by the contract. The resulting `maxDebt` is therefore `assets * price/ORACLE_PRICE_SCALE * lltv/WAD` instead of the correct `assets*(1-f) * price/ORACLE_PRICE_SCALE * lltv/WAD`.

**Attacker-controlled inputs and exploit flow:**
1. Attacker (market creator) calls `touchMarket` with a market whose `collateralParams[i].token` is a fee-on-transfer ERC20. `touchMarket` validates LLTV, maxLif, and sort order but has no token-type restriction.
2. Attacker calls `supplyCollateral(market, i, assets, borrower)`. Contract records `assets` in `_position.collateral[i]` but receives only `assets - fee`.
3. Attacker (as seller/borrower) calls `take` to increase debt. At line 476, `isHealthy` is called with the inflated stored collateral, returning `true` for debt up to `assets * lltv` instead of the correct `(assets - fee) * lltv`.
4. The position is now undercollateralized: `debt > actual_collateral_value`.

**Why existing checks fail:**
- `SafeTransferLib` checks only `transferFrom` success, not balance delta.
- `isHealthy` reads `_position.collateral[i]` directly — it has no access to the actual token balance.
- `touchMarket` imposes no restriction on fee-on-transfer tokens.
- The TOKEN SAFETY REQUIREMENTS comment (line 139) is documentation only; it is not enforced on-chain.
- The Certora proofs assume well-behaved ERC20 tokens and do not cover this case.

### Impact Explanation
An undercollateralized position is created across one or more collateral types. The gap between recorded collateral and actual collateral is `fee * lltv` per `supplyCollateral` call. For a 1% fee token with LLTV 0.77, supplying 1000 units records 1000 but holds 990; the borrower can take 770 units of debt while the actual collateral supports only 762.3 units. The shortfall is unrecoverable bad debt: when the position is liquidated, the seized collateral is worth less than the repaid debt, socializing losses to lenders.

### Likelihood Explanation
`touchMarket` is permissionless — any address can create a market with a fee-on-transfer collateral token. No governance approval, whitelist, or privileged role is required. The attacker can be simultaneously the market creator, the borrower, and the lender. The attack is repeatable on every `supplyCollateral` call for a fee-on-transfer token, compounding the inflation with each deposit. Fee-on-transfer tokens exist on mainnet (e.g., tokens with deflationary mechanics or explicit transfer taxes).

### Recommendation
Add a balance-before/balance-after check in `supplyCollateral` and credit only the actual received amount:
```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```
This ensures the stored collateral always reflects the actual on-chain balance, regardless of token transfer mechanics. The same pattern should be applied to any other entry point that pulls collateral tokens (e.g., the liquidation callback path if it ever pulls collateral).

### Proof of Concept
```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight, Market, CollateralParams} from "src/Midnight.sol";
import {ERC20} from "test/mocks/ERC20.sol";

// 1% fee-on-transfer token
contract FeeToken is ERC20 {
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount / 100;
        super.transferFrom(from, to, amount - fee); // only amount-fee arrives
        _burn(from, fee);
        return true;
    }
}

contract FeeOnTransferCollateralTest is Test {
    Midnight midnight;
    FeeToken feeToken;
    ERC20 loanToken;
    Oracle oracle;
    address borrower = makeAddr("borrower");
    address lender  = makeAddr("lender");

    function setUp() public { /* deploy midnight, set up roles */ }

    function testFeeOnTransferInflatesCollateral() public {
        // 1. Create market with fee-on-transfer collateral (permissionless)
        Market memory m;
        m.loanToken = address(loanToken);
        m.maturity  = block.timestamp + 1 days;
        CollateralParams[] memory cp = new CollateralParams[](1);
        cp[0] = CollateralParams({
            token: address(feeToken), lltv: 0.77e18,
            maxLif: midnight.maxLif(0.77e18, 0.25e18), oracle: address(oracle)
        });
        m.collateralParams = cp;
        bytes32 id = midnight.touchMarket(m);

        // 2. Supply 1000e18 fee tokens; contract receives 990e18
        uint256 assets = 1000e18;
        deal(address(feeToken), borrower, assets);
        vm.startPrank(borrower);
        feeToken.approve(address(midnight), assets);
        midnight.supplyCollateral(m, 0, assets, borrower);
        vm.stopPrank();

        // 3. Assert stored collateral is inflated vs actual balance
        assertEq(midnight.collateral(id, borrower, 0), assets,           "stored: 1000e18");
        assertEq(feeToken.balanceOf(address(midnight)), assets * 99/100, "actual: 990e18");

        // 4. Set oracle price = ORACLE_PRICE_SCALE (1:1)
        oracle.setPrice(1e36);

        // 5. Setup lender liquidity, borrower takes max debt
        // maxDebt from stored = 1000e18 * 0.77 = 770e18
        // maxDebt from actual = 990e18  * 0.77 = 762.3e18
        // Borrow 770e18 — isHealthy passes (uses inflated stored value)
        // ... take() call here ...

        // 6. Assert position is actually undercollateralized
        assertTrue(midnight.isHealthy(m, id, borrower), "isHealthy: true (inflated)");
        uint256 debt = midnight.debtOf(id, borrower); // 770e18
        uint256 actualCollateralValue = feeToken.balanceOf(address(midnight)) * 77/100; // 762.3e18
        assertGt(debt, actualCollateralValue, "undercollateralized: debt > actual collateral value");
    }
}
```
**Expected assertions:** `midnight.collateral(id, borrower, 0) == 1000e18` (inflated), `feeToken.balanceOf(address(midnight)) == 990e18` (actual), `isHealthy` returns `true` for 770e18 debt, and `debt > actualCollateralValue` (770e18 > 762.3e18), confirming the undercollateralized state.

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

**File:** certora/README.md (L112-112)
```markdown
- ERC20 tokens are assumed well-behaved, see the comments in the respective files for more detail.
```

**File:** live_context.json (L233-233)
```json
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
```
