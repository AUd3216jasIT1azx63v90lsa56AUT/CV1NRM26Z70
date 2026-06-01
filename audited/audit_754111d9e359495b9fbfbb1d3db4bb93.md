### Title
Fee-on-transfer collateral token causes `position.collateral` over-decrement relative to receiver proceeds in `withdrawCollateral` - (File: src/Midnight.sol)

### Summary
`withdrawCollateral` decrements `position.collateral[collateralIndex]` by the full `assets` parameter and then calls `SafeTransferLib.safeTransfer(collateralToken, receiver, assets)`, which invokes `token.transfer(receiver, assets)` without verifying the actual amount credited to `receiver`. When the collateral token is fee-on-transfer, the receiver receives only `assets*(1-fee)` while the position ledger is reduced by the full `assets`, permanently destroying the difference. No existing check in the function or in `SafeTransferLib` detects or prevents this discrepancy.

### Finding Description

**Code path:**

`supplyCollateral` (line 533, 545):
```
_position.collateral[collateralIndex] = toUint128(oldCollateral + assets);  // records full assets
safeTransferFrom(collateralToken, msg.sender, address(this), assets);        // receives assets*(1-fee)
```

`withdrawCollateral` (lines 561–572):
```
uint256 newCollateral = _position.collateral[collateralIndex] - assets;      // decrements by full assets
_position.collateral[collateralIndex] = toUint128(newCollateral);
...
SafeTransferLib.safeTransfer(collateralToken, receiver, assets);             // sends assets; receiver gets assets*(1-fee)
```

`SafeTransferLib.safeTransfer` (lines 15, 21):
```
(bool success, bytes memory returndata) = token.call(
    abi.encodeCall(IERC20.transfer, (to, value)));
require(returndata.length == 0 || abi.decode(returndata, (bool)), ...);
```
The library only checks the boolean return value of `transfer`; it does not measure the balance delta of `receiver`.

**Root cause:** Both `supplyCollateral` and `withdrawCollateral` use the caller-supplied `assets` value as the canonical accounting unit without performing a balance-before/balance-after check. For a fee-on-transfer token:

- At supply: the contract records `assets` in `position.collateral` but its actual token balance increases by only `assets*(1-fee)`.
- At withdrawal: the contract decrements `position.collateral` by `assets` and calls `transfer(receiver, assets)`. The token contract deducts the fee in-flight, so `receiver` receives `assets*(1-fee)` while the contract's balance falls by `assets`.

**Attacker-controlled inputs:** `assets` (any nonzero value), `collateralToken` (any fee-on-transfer ERC20 accepted as a collateral param in the market), `receiver` (any address).

**Why existing checks fail:**
- `isHealthy` (line 568) checks the post-decrement collateral value against debt; it does not compare contract balance to recorded collateral.
- `SafeTransferLib` validates only the boolean return of `transfer`, not the net amount received.
- There is no whitelist or fee-on-transfer guard anywhere in the collateral supply/withdraw path. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

Every `withdrawCollateral` call with a fee-on-transfer collateral token results in the borrower's on-chain position being reduced by `assets` while the receiver receives only `assets*(1-fee)`. The fee amount is permanently lost to the borrower on each withdrawal. Cumulatively, repeated withdrawals drain the borrower's effective collateral recovery below what was deposited, violating the invariant that a borrower can recover exactly the collateral they supplied. Additionally, because `supplyCollateral` also records `assets` but receives only `assets*(1-fee)`, the contract's actual token balance is structurally less than the sum of all recorded `position.collateral` values, meaning a full withdrawal by all borrowers is impossible and later withdrawers may face reverts. [4](#0-3) 

### Likelihood Explanation

**Preconditions:**
1. A market is created with a fee-on-transfer ERC20 as one of its `collateralParams[i].token` entries. Market creation is permissionless.
2. A borrower supplies and then withdraws that collateral token.

**Feasibility:** Fully reachable by any unprivileged borrower. No admin action, oracle manipulation, or special privilege is required. The borrower only needs to interact with a market whose collateral token charges a transfer fee. Fee-on-transfer tokens (e.g., tokens with deflationary mechanics or protocol fees) are common in DeFi. The bug is triggered on every single `withdrawCollateral` call and is therefore repeatable without limit. [5](#0-4) 

### Recommendation

Perform a balance-before/balance-after check in `withdrawCollateral` (and symmetrically in `supplyCollateral`) to measure the actual amount transferred, and use that measured delta for both the position accounting update and the transfer amount:

```solidity
// withdrawCollateral: measure actual outflow
uint256 balanceBefore = IERC20(collateralToken).balanceOf(receiver);
SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
uint256 actualReceived = IERC20(collateralToken).balanceOf(receiver) - balanceBefore;
require(actualReceived == assets, FeeOnTransferNotSupported());
```

Alternatively, document and enforce at market creation that fee-on-transfer tokens are not permitted as collateral tokens, and add an explicit revert guard (e.g., a balance-delta check in `supplyCollateral` that reverts if received < assets). [2](#0-1) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
// ... standard test imports

contract FeeOnTransferToken is ERC20 {
    uint256 public constant FEE_BPS = 100; // 1% fee
    address public feeCollector;
    constructor(address _fc) ERC20("FOT","FOT") { feeCollector = _fc; }
    function transfer(address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * FEE_BPS / 10000;
        super.transfer(feeCollector, fee);
        super.transfer(to, amount - fee);
        return true;
    }
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * FEE_BPS / 10000;
        super.transferFrom(from, feeCollector, fee);
        super.transferFrom(from, to, amount - fee);
        return true;
    }
}

contract FeeOnTransferCollateralTest is Test {
    Midnight midnight;
    FeeOnTransferToken fotToken;
    address borrower = address(0xB0B);
    address receiver = address(0xBEEF);
    address feeCollector = address(0xFEE);

    function setUp() public {
        midnight = new Midnight();
        fotToken = new FeeOnTransferToken(feeCollector);
        fotToken.mint(borrower, 10_000e18);
    }

    function test_feeOnTransferCollateralWithdrawLoss() public {
        // Build market with fotToken as collateral
        Market memory market = _buildMarket(address(fotToken));
        uint256 supplyAmount = 1000e18;

        vm.startPrank(borrower);
        fotToken.approve(address(midnight), type(uint256).max);

        // Supply: protocol records supplyAmount but receives supplyAmount*(1-fee)
        midnight.supplyCollateral(market, 0, supplyAmount, borrower);

        uint256 recordedCollateral = midnight.position(/*id*/, borrower).collateral[0];
        assertEq(recordedCollateral, supplyAmount); // records full amount

        // Withdraw: position decremented by supplyAmount, receiver gets supplyAmount*(1-fee)
        uint256 receiverBalBefore = fotToken.balanceOf(receiver);
        midnight.withdrawCollateral(market, 0, supplyAmount, borrower, receiver);
        uint256 receiverBalAfter = fotToken.balanceOf(receiver);

        uint256 actualReceived = receiverBalAfter - receiverBalBefore;

        // KEY ASSERTIONS:
        // 1. Receiver got less than assets
        assertLt(actualReceived, supplyAmount);
        // 2. Position was decremented by full assets (now 0)
        assertEq(midnight.position(/*id*/, borrower).collateral[0], 0);
        // 3. The fee was lost (not returned to borrower)
        uint256 expectedFee = supplyAmount * 100 / 10000;
        assertEq(supplyAmount - actualReceived, expectedFee);
        vm.stopPrank();
    }
}
```

**Expected assertions:** `actualReceived < supplyAmount` (receiver gets `990e18` not `1000e18`), `position.collateral[0] == 0` (fully decremented), fee of `10e18` permanently lost to `feeCollector`. [6](#0-5) [7](#0-6)

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

**File:** src/Midnight.sol (L549-573)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 newCollateral = _position.collateral[collateralIndex] - assets;
        _position.collateral[collateralIndex] = UtilsLib.toUint128(newCollateral);

        if (newCollateral == 0 && assets > 0) {
            _position.collateralBitmap = _position.collateralBitmap.clearBit(collateralIndex);
        }

        require(isHealthy(market, id, onBehalf), UnhealthyBorrower());

        emit EventsLib.WithdrawCollateral(msg.sender, id, collateralToken, assets, onBehalf, receiver);

        SafeTransferLib.safeTransfer(collateralToken, receiver, assets);
    }
```

**File:** src/libraries/SafeTransferLib.sol (L12-22)
```text
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
```
