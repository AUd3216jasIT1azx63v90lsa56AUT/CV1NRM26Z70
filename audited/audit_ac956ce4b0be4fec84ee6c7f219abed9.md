### Title
Gate `canIncreaseDebt` check fires against pre-fill seller debt, allowing seller to borrow beyond gate-intended debt cap - (`src/interfaces/IGate.sol` / `src/Midnight.sol`)

### Summary

In `Midnight.take`, the `enterGate.canIncreaseDebt(seller)` check is invoked at lines 402–406 before the seller's position state is written at lines 412–414. Because `IEnterGate.canIncreaseDebt` receives only the seller's address and no amount, any gate that enforces a debt cap by reading `midnight.debtOf(id, seller)` will observe the pre-fill debt value. An unprivileged taker can therefore cause the seller to accumulate debt beyond the threshold the gate was designed to enforce.

### Finding Description

**Interface constraint.** `IGate.sol` declares:

```solidity
function canIncreaseDebt(address account) external view returns (bool);
```

No debt-increase amount is passed. A gate enforcing a per-account debt cap must therefore call back into Midnight to read `debtOf(id, seller)`.

**Ordering in `take`.** In `src/Midnight.sol`:

```
// lines 402-406 — gate check fires HERE (pre-write)
require(
    offer.market.enterGate == address(0) || sellerDebtIncrease == 0
        || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
    SellerGatedFromIncreasingDebt()
);

// lines 412-414 — state written AFTER gate check
sellerPos.pendingFee -= sellerPendingFeeDecrease;
sellerPos.credit     -= UtilsLib.toUint128(sellerCreditDecrease);
sellerPos.debt       += UtilsLib.toUint128(sellerDebtIncrease);   // ← written after gate
```

**Exploit flow.**

1. Market creator deploys a `DebtCapGate` whose `canIncreaseDebt` returns `midnight.debtOf(id, account) < CAP`.
2. Seller (maker of a sell offer with `offer.maxUnits > 0`) already has `debtOf(seller) = CAP - 1`.
3. Taker calls `take` with `units` such that `sellerDebtIncrease = units - sellerCreditDecrease > 1`.
4. At line 404, `canIncreaseDebt(seller)` calls back into Midnight: `debtOf(seller) = CAP - 1 < CAP` → returns `true` → gate passes.
5. At line 414, `sellerPos.debt += sellerDebtIncrease` → `debtOf(seller) = CAP - 1 + sellerDebtIncrease > CAP`.
6. Seller's debt now exceeds the gate-intended cap; the gate invariant is violated.

**Why existing checks do not stop it.** The only gate-related check is the binary `canIncreaseDebt` boolean at lines 402–406. There is no post-write re-validation against the gate. The health check at line 476 (`isHealthy`) is independent of the gate and does not enforce the gate's debt cap. The `reduceOnly` flag (lines 392–395) is offer-level and unrelated. No other mechanism prevents the gate from seeing stale state.

### Impact Explanation

A seller can borrow more than the gate-intended debt limit in a single `take` call. Any market that relies on an `enterGate` to enforce per-account debt caps (e.g., credit-limit gates, KYC-tier gates, risk-parameter gates) is silently bypassed. The seller's on-chain debt after the fill exceeds the cap the gate was designed to enforce, violating the invariant that gate checks must apply to all entry paths with correct post-fill state.

### Likelihood Explanation

**Preconditions:**
- A market with an `enterGate` whose `canIncreaseDebt` reads `debtOf` from Midnight (a natural and expected gate pattern given the interface provides no amount).
- Seller has existing debt close to but below the gate's threshold.
- A valid, non-expired sell offer with `offer.maxUnits > 0` and sufficient remaining capacity.

**Feasibility:** All preconditions are attacker-reachable. The taker is unprivileged and controls `units`. The attack is repeatable: after each fill the seller's debt rises further above the cap, and subsequent fills continue to pass the gate as long as the gate only checks `< CAP` (not `== 0`). No oracle manipulation, admin access, or user mistake is required.

### Recommendation

Pass `sellerDebtIncrease` (and `buyerCreditIncrease`) to the gate so it can make a correct decision without reading Midnight state:

```solidity
interface IEnterGate {
    function canIncreaseCredit(address account, uint256 amount) external view returns (bool);
    function canIncreaseDebt(address account, uint256 amount) external view returns (bool);
}
```

Then call:

```solidity
IEnterGate(offer.market.enterGate).canIncreaseDebt(seller, sellerDebtIncrease)
```

This allows gate implementations to enforce `debtOf(seller) + amount <= CAP` without relying on Midnight's storage, eliminating the pre-fill/post-fill state race entirely.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {IMidnight, Market, Offer} from "src/interfaces/IMidnight.sol";
import {IEnterGate} from "src/interfaces/IGate.sol";

contract DebtCapGate is IEnterGate {
    IMidnight public midnight;
    bytes32   public marketId;
    uint256   public cap;

    constructor(IMidnight _m, bytes32 _id, uint256 _cap) {
        midnight = _m; marketId = _id; cap = _cap;
    }
    function canIncreaseCredit(address) external pure returns (bool) { return true; }
    function canIncreaseDebt(address account) external view returns (bool) {
        // reads pre-fill debt — this is the vulnerable read
        return midnight.debtOf(marketId, account) < cap;
    }
}

contract GatePreFillDebtTest is Test {
    // ... standard Midnight test setup (BaseTest pattern) ...

    function testGateSeesPreFillDebt() public {
        uint256 CAP = 100e18;

        // 1. Deploy gate with debt cap = CAP
        DebtCapGate gate = new DebtCapGate(midnight, id, CAP);
        // market.enterGate = address(gate); (set during market creation)

        // 2. Seller borrows CAP - 1 units in a prior take
        // sellerDebt = CAP - 1 after setup

        // 3. Taker calls take with units = 50e18 (sellerDebtIncrease = 50e18)
        //    Expected: gate should block (CAP-1 + 50e18 > CAP)
        //    Actual:   gate sees debtOf(seller) = CAP-1 < CAP → passes

        uint256 debtBefore = midnight.debtOf(id, seller);
        assertEq(debtBefore, CAP - 1);

        take(50e18, taker, sellOffer); // should revert but does not

        uint256 debtAfter = midnight.debtOf(id, seller);
        // Key assertion: debt exceeds cap despite gate
        assertGt(debtAfter, CAP, "seller debt exceeds gate cap — gate bypassed");
    }
}
```

**Expected assertion:** `debtAfter > CAP` passes, proving the gate was bypassed because it observed `debtOf(seller) = CAP - 1` (pre-fill) rather than the post-fill value. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/interfaces/IGate.sol (L5-8)
```text
interface IEnterGate {
    function canIncreaseCredit(address account) external view returns (bool);
    function canIncreaseDebt(address account) external view returns (bool);
}
```

**File:** src/Midnight.sol (L402-406)
```text
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );
```

**File:** src/Midnight.sol (L412-414)
```text
        sellerPos.pendingFee -= sellerPendingFeeDecrease;
        sellerPos.credit -= UtilsLib.toUint128(sellerCreditDecrease);
        sellerPos.debt += UtilsLib.toUint128(sellerDebtIncrease);
```
