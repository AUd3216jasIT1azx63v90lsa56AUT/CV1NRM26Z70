### Title
Fee-on-Transfer Collateral Token Inflates Recorded Collateral, Enabling Phantom Borrowing - (File: src/Midnight.sol)

### Summary
`supplyCollateral` credits `position[id][onBehalf].collateral[collateralIndex]` with the full nominal `assets` value before calling `SafeTransferLib.safeTransferFrom`, which only verifies the call did not revert and did not return `false`. It performs no balance-delta check. When the collateral token deducts a fee on `transferFrom`, Midnight records more collateral than it actually holds, allowing a borrower to borrow against phantom collateral and create bad debt.

### Finding Description

**Exact code path:**

`supplyCollateral` in `src/Midnight.sol`:

```
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);  // line 533
...
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets); // line 545
``` [1](#0-0) 

`SafeTransferLib.safeTransferFrom` in `src/libraries/SafeTransferLib.sol`:

```solidity
(bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
...
require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
``` [2](#0-1) 

The library only checks for revert and `false` return. A fee-on-transfer token returns `true` while delivering `assets * (1 - fee_rate)` to `address(this)`. No balance snapshot is taken before or after the call.

**Market creation is permissionless.** `touchMarket` validates LLTV tiers, sorted collateral addresses, and `maxLif`, but imposes no restriction on the economic behavior of the collateral token itself. [3](#0-2) 

**Exploit flow:**

1. Attacker (acting as market creator) calls `touchMarket` with a fee-on-transfer token (e.g., 10% fee) as the collateral token. This succeeds because no token behavior is validated.
2. Attacker (acting as borrower) calls `supplyCollateral(market, 0, 1000e18, attacker)`.
3. `_position.collateral[0]` is set to `1000e18` (full nominal).
4. `safeTransferFrom` delivers only `900e18` to Midnight (10% fee deducted).
5. Midnight's actual token balance for that collateral is `900e18`; the recorded position is `1000e18`.
6. Attacker calls `take` on a lender offer, borrowing up to `1000e18 * price * lltv / WAD` in loan tokens — collateralized by `100e18` of phantom collateral.
7. The position is immediately undercollateralized relative to actual held assets. If the attacker does not repay, lenders absorb bad debt.

**Existing protections are insufficient:**
- `SafeTransferLib` does not check received amounts.
- `touchMarket` does not whitelist or validate token behavior.
- The Certora `supplyCollateralEffects` rule models `safeTransferFrom` as a perfect transfer (NONDET), so it does not catch this class of bug. [4](#0-3) 

### Impact Explanation

The invariant `position[id][borrower].collateral[i] <= actual collateralToken balance held by Midnight for that market` is broken. The borrower borrows against collateral that does not exist in the contract. When the position is liquidated, the seized collateral is worth less than the debt repaid, and the shortfall is socialized as bad debt among lenders via the loss-factor mechanism.

### Likelihood Explanation

**Preconditions:**
- A fee-on-transfer token must be used as a collateral token. Market creation is permissionless, so the attacker can create such a market themselves.
- The attacker must be able to acquire the fee-on-transfer token and have a lender willing to fill a borrow offer in that market (the attacker can also be the lender, or lure external lenders).

**Feasibility:** High. Fee-on-transfer tokens are common (e.g., USDT with fee enabled, PAXG, STA, tokens with protocol fees). The attack requires no special privilege, no oracle manipulation, and no reentrancy. It is repeatable: each `supplyCollateral` call inflates the position by `assets * fee_rate`.

### Recommendation

Record the actual received amount using a balance-before/balance-after pattern in `supplyCollateral`:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Alternatively, document that fee-on-transfer tokens are unsupported and add a token behavior check (e.g., a dry-run balance check) in `touchMarket` during market creation.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {Midnight} from "../src/Midnight.sol";
import {Market, CollateralParams} from "../src/interfaces/IMidnight.sol";

/// Fee-on-transfer ERC20: deducts feeRate (in bps) on every transferFrom.
contract FeeToken is ERC20 {
    uint256 public feeRate; // e.g. 1000 = 10%
    constructor(uint256 _feeRate) ERC20("FeeToken","FT") { feeRate = _feeRate; }
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        uint256 fee = amount * feeRate / 10000;
        uint256 received = amount - fee;
        _burn(from, amount);
        _mint(to, received);
        return true;
    }
}

contract FeeOnTransferCollateralTest is Test {
    Midnight midnight;
    FeeToken feeToken;
    Market market;

    function setUp() public {
        midnight = new Midnight(address(this), address(this), address(this), address(this));
        feeToken = new FeeToken(1000); // 10% fee
        // build market with feeToken as collateral
        // ... (set lltv, maxLif, oracle, maturity, loanToken as in BaseTest)
    }

    function testFeeOnTransferInflatesCollateral(uint256 feeRate) public {
        feeRate = bound(feeRate, 100, 5000); // 1% to 50%
        feeToken = new FeeToken(feeRate);

        uint256 assets = 1000e18;
        deal(address(feeToken), address(this), assets);
        feeToken.approve(address(midnight), assets);

        uint256 balBefore = feeToken.balanceOf(address(midnight));
        midnight.supplyCollateral(market, 0, assets, address(this));
        uint256 balAfter = feeToken.balanceOf(address(midnight));

        uint256 actualReceived = balAfter - balBefore;
        uint256 recordedCollateral = midnight.collateral(keccak256(abi.encode(market)), address(this), 0);

        // ASSERTION: recorded collateral exceeds actual balance — invariant broken
        assertGt(recordedCollateral, actualReceived, "phantom collateral exists");
        assertEq(recordedCollateral, assets, "recorded full nominal");
        assertEq(actualReceived, assets * (10000 - feeRate) / 10000, "only net received");
    }
}
```

**Expected result:** `recordedCollateral > actualReceived` for any `feeRate > 0`, confirming the invariant violation. A follow-on stateful test can then show the borrower successfully taking a loan offer and the protocol holding insufficient collateral to cover the debt.

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

**File:** certora/specs/BalanceEffects.spec (L207-219)
```text
/// supplyCollateral increases onBehalf's collateral by exactly assets,
/// and only changes position[id][onBehalf].collateral[collateralIndex].
rule supplyCollateralEffects(env e, Midnight.Market market, uint256 collateralIndex, uint256 assets, address onBehalf, bytes32 anyId, address anyUser, uint256 anyIndex) {
    bytes32 id = toId(e, market);

    uint256 collateralBefore = collateral(id, onBehalf, collateralIndex);
    uint256 otherCollateralBefore = collateral(anyId, anyUser, anyIndex);

    supplyCollateral(e, market, collateralIndex, assets, onBehalf);

    assert collateral(id, onBehalf, collateralIndex) == collateralBefore + assets;
    assert anyUser != onBehalf || anyId != id || anyIndex != collateralIndex => collateral(anyId, anyUser, anyIndex) == otherCollateralBefore;
}
```
