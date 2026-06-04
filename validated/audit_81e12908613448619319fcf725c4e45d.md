### Title
Unchecked `takes[0]` Array Access in `MidnightBundles` Causes Unconditional Revert on Empty Input — (File: src/periphery/MidnightBundles.sol)

---

### Summary

All four bundle execution functions in `MidnightBundles.sol` unconditionally access `takes[0]` before any length guard. When `takes` is an empty array, the EVM reverts with an out-of-bounds panic before any meaningful validation or error message is produced. This is the direct structural analog of the reported `solverOps[0]` access in `_verifyAuctioneer()`: an array index-zero dereference with no prior length check.

---

### Finding Description

Every public bundle function derives both the `loanToken` and the market `id` exclusively from `takes[0]` at the very top of the function body, before any loop or guard:

**`buyWithUnitsTargetAndWithdrawCollateral`** [1](#0-0) 

**`supplyCollateralAndSellWithUnitsTarget`** [2](#0-1) 

**`buyWithAssetsTargetAndWithdrawCollateral`** [3](#0-2) 

**`supplyCollateralAndSellWithAssetsTarget`** [4](#0-3) 

In each case the pattern is identical:

```solidity
address loanToken = takes[0].offer.market.loanToken;   // panics if takes.length == 0
bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);
```

There is no `require(takes.length > 0, ...)` guard anywhere in the contract. [5](#0-4) 

Additionally, after the fill loop, two of the functions re-access `takes[0]` to obtain the `Market` struct for collateral operations: [6](#0-5) [7](#0-6) 

**Concrete reachable path:**

1. A caller invokes `buyWithUnitsTargetAndWithdrawCollateral` with `targetUnits = 0`, a non-empty `collateralWithdrawals` list, and `takes = []` — a semantically valid intent (withdraw collateral, no buying needed).
2. The EVM immediately panics at `takes[0]` with an array-out-of-bounds error (Solidity panic code `0x32`) before any state is read or written.
3. The transaction reverts with an opaque panic rather than the protocol's own `OutOfOffers()` error.

The contract's own NatSpec explicitly states "No-ops are allowed" and "Zero checks are not systematically performed," creating a reasonable expectation that a zero-unit call with an empty `takes` array is a valid no-op. [8](#0-7) 

---

### Impact Explanation

Any caller — including an integrating smart contract that programmatically constructs the `takes` array — that passes an empty `takes` array receives an opaque EVM panic revert. The `collateralWithdrawals` path (which does not logically require any takes when `targetUnits == 0`) is permanently unreachable via these functions with an empty `takes` array. No funds are at risk, but the bundler is rendered non-functional for this input class, and integrators relying on the "no-ops are allowed" contract invariant will encounter unexpected failures.

---

### Likelihood Explanation

The likelihood is **low-to-medium**. The scenario is reachable without any privileged access: any external caller or integrating contract can supply `takes = []`. The contract's own documentation ("No-ops are allowed") and the existence of a `targetUnits` parameter that can be zero both create a reasonable expectation that an empty `takes` array is a valid input. Off-chain order-book integrations that filter out expired/consumed offers before submission could produce an empty array at execution time.

---

### Recommendation

Add an explicit length guard at the top of each affected function, or restructure to accept the `Market` as a separate parameter (as `repayAndWithdrawCollateral` already does) so that the market identity is not derived solely from `takes[0]`:

```solidity
// Option A: guard
require(takes.length > 0, EmptyTakes());

// Option B: accept market explicitly (mirrors repayAndWithdrawCollateral's design)
function buyWithUnitsTargetAndWithdrawCollateral(
    Market memory market,   // <-- explicit
    uint256 targetUnits,
    ...
    Take[] memory takes,
    ...
)
```

`repayAndWithdrawCollateral` already uses the explicit-market pattern and is not affected. [9](#0-8) 

---

### Proof of Concept

```solidity
// Attacker/integrator calls:
Take[] memory emptyTakes = new Take[](0);
CollateralWithdrawal[] memory withdrawals = new CollateralWithdrawal[](1);
withdrawals[0] = CollateralWithdrawal({collateralIndex: 0, assets: 1e18});

// Reverts with Panic(0x32) at takes[0] before any logic executes:
midnightBundles.buyWithUnitsTargetAndWithdrawCollateral(
    0,                  // targetUnits = 0 (no buying needed)
    0,                  // maxBuyerAssets
    msg.sender,         // taker
    emptyPermit,
    emptyTakes,         // takes.length == 0  <-- triggers panic
    withdrawals,
    receiver,
    0,
    address(0)
);
// Expected: proceeds to collateral withdrawal (targetUnits == 0 is satisfied)
// Actual:   EVM Panic(0x32) — array out-of-bounds at takes[0]
```

### Citations

**File:** src/periphery/MidnightBundles.sol (L23-26)
```text
/// @dev Inherits the token safety requirements of Midnight (see Midnight.sol).
/// @dev Unusable with tokens that revert on such a sequence: approve(..., 0); approve(..., type(uint256).max).
/// @dev No-ops are allowed.
/// @dev Zero checks are not systematically performed.
```

**File:** src/periphery/MidnightBundles.sol (L27-65)
```text
contract MidnightBundles is IMidnightBundles {
    using UtilsLib for uint256;

    address public constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address public immutable MIDNIGHT;

    constructor(address _midnight) {
        MIDNIGHT = _midnight;
    }

    /// EXTERNAL ///

    /// @dev The taker must have authorized this bundler and the msg.sender (if different from the taker) on Midnight.
    /// @dev This function should only be called with the same market for all takes.
    /// @dev The collateral transfers always use the first offer's market.
    /// @dev Skips every reason why take can revert (including ones that are not asynchrony related).
    /// @dev Reverts if ConsumableUnitsLib reverts.
    /// @dev If taking an offer reverts, the bundler will completely skip this offer.
    /// @dev This function pulls maxBuyerAssets from the msg.sender and transfers back the remaining tokens at the end.
    /// @dev The msg.sender will pay at most maxBuyerAssets.
    /// @dev Total loan assets transferred from msg.sender is
    /// filledBuyerAssets + filledBuyerAssets * referralFeePct / (WAD - referralFeePct).
    function buyWithUnitsTargetAndWithdrawCollateral(
        uint256 targetUnits,
        uint256 maxBuyerAssets,
        address taker,
        TokenPermit memory loanTokenPermit,
        Take[] memory takes,
        CollateralWithdrawal[] memory collateralWithdrawals,
        address collateralReceiver,
        uint256 referralFeePct,
        address referralFeeRecipient
    ) external {
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);

```

**File:** src/periphery/MidnightBundles.sol (L90-91)
```text
        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralWithdrawals.length; i++) {
```

**File:** src/periphery/MidnightBundles.sol (L129-131)
```text
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);
```

**File:** src/periphery/MidnightBundles.sol (L193-195)
```text
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);
```

**File:** src/periphery/MidnightBundles.sol (L227-228)
```text
        Market memory market = takes[0].offer.market;
        for (uint256 i; i < collateralWithdrawals.length; i++) {
```

**File:** src/periphery/MidnightBundles.sol (L264-266)
```text
        address loanToken = takes[0].offer.market.loanToken;
        // touchMarket to have the correct settlement fees.
        bytes32 id = IMidnight(MIDNIGHT).touchMarket(takes[0].offer.market);
```

**File:** src/periphery/MidnightBundles.sol (L315-348)
```text
    function repayAndWithdrawCollateral(
        Market memory market,
        uint256 assets,
        address onBehalf,
        TokenPermit memory loanTokenPermit,
        CollateralWithdrawal[] memory collateralWithdrawals,
        address collateralReceiver,
        uint256 referralFeePct,
        address referralFeeRecipient
    ) external {
        require(onBehalf == msg.sender || IMidnight(MIDNIGHT).isAuthorized(onBehalf, msg.sender), Unauthorized());
        require(referralFeePct < WAD, PctExceeded());

        address loanToken = market.loanToken;
        uint256 referralFeeAssets = assets.mulDivDown(referralFeePct, WAD);
        uint256 units = assets - referralFeeAssets;
        pullToken(loanToken, msg.sender, assets, loanTokenPermit);
        forceApproveMax(loanToken, MIDNIGHT);

        IMidnight(MIDNIGHT).repay(market, units, onBehalf, address(0), "");

        for (uint256 i; i < collateralWithdrawals.length; i++) {
            IMidnight(MIDNIGHT)
                .withdrawCollateral(
                    market,
                    collateralWithdrawals[i].collateralIndex,
                    collateralWithdrawals[i].assets,
                    onBehalf,
                    collateralReceiver
                );
        }

        if (referralFeeAssets > 0) SafeTransferLib.safeTransfer(loanToken, referralFeeRecipient, referralFeeAssets);
    }
```
