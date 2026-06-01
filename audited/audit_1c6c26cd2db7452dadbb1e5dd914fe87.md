The code is clear. Let me confirm the exact execution path and whether any existing check stops this.

### Title
Fee-on-Transfer Collateral Token Inflates Stored Collateral, Enabling Undercollateralized Borrowing and Bad Debt - (File: src/Midnight.sol)

### Summary
`supplyCollateral` credits `_position.collateral[collateralIndex]` with the full caller-supplied `assets` value before executing the token pull, and `SafeTransferLib.safeTransferFrom` only checks that `transferFrom` returns `true` without verifying the actual balance delta. For a fee-on-transfer collateral token, fewer tokens arrive than are credited, so `isHealthy` computes `maxDebt` from an inflated collateral figure, allowing a borrower to take on debt that exceeds the real collateral value from the moment of deposit.

### Finding Description
**Exact code path:**

`supplyCollateral` in `src/Midnight.sol` lines 531–545:
```
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets); // (line 533) — full assets credited
...
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets); // (line 545) — pull happens after
``` [1](#0-0) 

`SafeTransferLib.safeTransferFrom` in `src/libraries/SafeTransferLib.sol` lines 24–34 only checks that `transferFrom` returns `true` (or no return data). It performs no balance-before/after comparison. [2](#0-1) 

`isHealthy` in `src/Midnight.sol` lines 944–959 reads `_position.collateral[i]` directly from storage to compute `maxDebt`:
```
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
    .mulDivDown(collateralParam.lltv, WAD);
``` [3](#0-2) 

`touchMarket` in `src/Midnight.sol` lines 754–791 validates only that collateral tokens are sorted, LLTV is from an allowed tier, and `maxLif` is valid. There is no check that the collateral token is not fee-on-transfer. [4](#0-3) 

**Attacker-controlled inputs:**
- Deploys a fee-on-transfer ERC20 (e.g., 1% fee on every `transferFrom`)
- Calls `touchMarket` with this token as collateral (permissionless)
- Calls `supplyCollateral(market, 0, 1000e18, borrower)` — `assets = 1000e18`

**State changes:**
- `_position.collateral[0]` is set to `1000e18` (inflated)
- Midnight's actual token balance increases by only `990e18` (after 1% fee)
- `isHealthy` computes `maxDebt` using `1000e18`, not `990e18`
- Borrower takes a sell offer and incurs debt up to `1000e18 * price * lltv / WAD`
- Actual collateral supports only `990e18 * price * lltv / WAD` debt

**Why existing checks fail:**
- `SafeTransferLib` does not measure balance delta — it only checks the boolean return of `transferFrom`
- `touchMarket` has no token-type whitelist
- The Certora `Solvency.spec` `tokenBalanceCorrect` invariant explicitly assumes well-behaved ERC20 tokens (README line 112: "ERC20 tokens are assumed well-behaved"), so the formal verification does not cover this case [5](#0-4) 
- The `BalanceEffects.spec` rule `supplyCollateralEffects` asserts `collateral == collateralBefore + assets` — this is exactly the inflated accounting that the fee-on-transfer token exploits [6](#0-5) 

### Impact Explanation
The position is undercollateralized from the moment of deposit by `fee * price * lltv / WAD` units of debt capacity. Any lender whose buy offer is taken against this position is exposed to bad debt that cannot be recovered through liquidation, because the seized collateral is worth less than the debt repaid. The solvency invariant — that the contract's token balance covers all collateral claims — is broken for the fee-on-transfer collateral token.

### Likelihood Explanation
Market creation is permissionless; any address can deploy a fee-on-transfer ERC20 and register it as collateral with no admin approval. The attacker only needs a willing lender (or can attract one by offering favorable rates on a seemingly legitimate market). The attack is repeatable on every `supplyCollateral` call with a fee-on-transfer token, and the inflation compounds with each additional deposit.

### Recommendation
In `supplyCollateral`, record the contract's collateral token balance before and after the `safeTransferFrom` call and credit only the actual received amount:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

This must be applied consistently to `repay` and any other inbound token pull if fee-on-transfer loan tokens are also in scope. Alternatively, document and enforce at market creation that collateral tokens must not be fee-on-transfer (requires a token registry or explicit validation in `touchMarket`).

### Proof of Concept
```solidity
// Foundry unit test
contract FeeOnTransferCollateral is ERC20 {
    uint256 constant FEE_BPS = 100; // 1%
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * FEE_BPS / 10000;
        super.transferFrom(from, to, amount - fee); // deliver amount - fee
        _burn(from, fee);                           // burn the fee
        return true;
    }
}

function testFeeOnTransferCollateralInflation() public {
    FeeOnTransferCollateral fot = new FeeOnTransferCollateral();
    // Create market with fot as collateral
    Market memory market = ...; // collateralParams[0].token = address(fot)
    midnight.touchMarket(market);
    bytes32 id = midnight.toId(market);

    uint256 supply = 1000e18;
    fot.mint(borrower, supply);
    vm.prank(borrower);
    fot.approve(address(midnight), supply);
    vm.prank(borrower);
    midnight.supplyCollateral(market, 0, supply, borrower);

    // Assert: stored collateral is inflated
    assertEq(midnight.collateral(id, borrower, 0), 1000e18);
    // Assert: actual balance is only 990e18
    assertEq(fot.balanceOf(address(midnight)), 990e18);

    // Borrow to max debt based on inflated collateral
    // ... set up lender offer, take it ...
    // Assert: isHealthy returns true despite actual undercollateralization
    assertTrue(midnight.isHealthy(market, id, borrower));

    // Drop oracle price slightly so actual collateral < debt
    oracle.setPrice(oracle.price() * 990 / 1000);
    // Assert: position is now liquidatable and realizes bad debt
    assertFalse(midnight.isHealthy(market, id, borrower));
    // Liquidate and assert bad debt is realized (lossFactor increases)
}
```

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

**File:** src/Midnight.sol (L757-773)
```text
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
```

**File:** src/Midnight.sol (L948-959)
```text
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

**File:** certora/specs/BalanceEffects.spec (L209-218)
```text
rule supplyCollateralEffects(env e, Midnight.Market market, uint256 collateralIndex, uint256 assets, address onBehalf, bytes32 anyId, address anyUser, uint256 anyIndex) {
    bytes32 id = toId(e, market);

    uint256 collateralBefore = collateral(id, onBehalf, collateralIndex);
    uint256 otherCollateralBefore = collateral(anyId, anyUser, anyIndex);

    supplyCollateral(e, market, collateralIndex, assets, onBehalf);

    assert collateral(id, onBehalf, collateralIndex) == collateralBefore + assets;
    assert anyUser != onBehalf || anyId != id || anyIndex != collateralIndex => collateral(anyId, anyUser, anyIndex) == otherCollateralBefore;
```
