### Title
Offer `consumed` Capacity Can Be Griefed via Front-Running, DOSing Legitimate Takers — (`File: src/Midnight.sol`)

---

### Summary

The `take` function in `Midnight.sol` enforces a strict upper-bound check on `consumed[offer.maker][offer.group]` against `offer.maxUnits` or `offer.maxAssets`. Because any permissionless caller can take a minimal amount from a public offer, an attacker can front-run a legitimate taker's transaction with a dust-sized take, pushing `consumed` just past the threshold the legitimate taker needs, causing their transaction to revert. Critically, `setConsumed` only allows the maker to *increase* consumed (not reset it), so the maker cannot undo the damage without canceling the offer entirely and redeploying with a new group.

---

### Finding Description

**Root cause — strict consumed ceiling with no reset path**

In `take`, the consumed counter is incremented atomically and then checked:

```solidity
// src/Midnight.sol L366-373
uint256 newConsumed;
if (offer.maxAssets > 0) {
    newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
    require(newConsumed <= offer.maxAssets, ConsumedAssets());
} else {
    newConsumed = consumed[offer.maker][offer.group] += units;
    require(newConsumed <= offer.maxUnits, ConsumedUnits());
}
``` [1](#0-0) 

If `newConsumed` exceeds the cap by even 1 unit, the entire transaction reverts. There is no partial-fill or refund path.

**`setConsumed` cannot decrease consumed**

The only maker-controlled escape valve is `setConsumed`, but it enforces a monotone-increase invariant:

```solidity
// src/Midnight.sol L723-728
function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    require(amount >= consumed[onBehalf][group], AlreadyConsumed());
    consumed[onBehalf][group] = amount;
``` [2](#0-1) 

The maker can only set consumed to a value ≥ its current value. Resetting to 0 (or any lower value) is impossible. The only recovery is to set consumed to `type(uint256).max` (canceling the offer) and re-create it under a new group — which the attacker can immediately front-run again.

**Attack path**

1. Maker posts a sell offer (`offer.buy = false`) with `maxUnits = M`. Current `consumed[maker][group] = C`.
2. Legitimate taker broadcasts a `take` for `X = M − C` units (consuming the full remaining capacity).
3. Attacker observes the pending transaction in the mempool and front-runs with `take(..., units = 1, ...)` on the same offer.
4. `consumed` becomes `C + 1`.
5. Legitimate taker's transaction executes: `newConsumed = C + 1 + X = M + 1 > M` → reverts with `ConsumedUnits()`.
6. Attacker repeats on every retry, permanently preventing the large take.

**Cost of the attack**

For a sell offer (`offer.buy = false`), the attacker is the buyer and pays:

```
buyerAssets = mulDivUp(1, buyerPrice, WAD)
```

`buyerPrice = tickToPrice(tick) + settlementFee`. Since `tickToPrice` rounds to `PRICE_ROUNDING_STEP = 1e12` and `WAD = 1e18`, for any tick with price ≥ 1e12, `buyerAssets = 1` loan-token unit. The attacker spends at most 1 indivisible loan-token unit per front-run — a negligible cost on any ERC-20 with standard 18-decimal precision. [3](#0-2) [4](#0-3) 

---

### Impact Explanation

**Impact: Medium.** An attacker can permanently prevent any single large `take` from succeeding against a targeted offer. The maker is forced to cancel and re-create the offer (new group), which the attacker can front-run again indefinitely. This degrades market liquidity and imposes repeated gas costs on makers. Core protocol functionality (offer execution) is disrupted for targeted offers, though other offers and markets remain unaffected.

---

### Likelihood Explanation

**Likelihood: Medium.** The attack is permissionless — any address can call `take`. The cost per front-run is at most 1 loan-token unit (often effectively 0 for tokens with low per-unit value). The attacker has no direct financial gain, making this a pure griefing vector, but the barrier to execution is extremely low. On any chain with a public mempool (Ethereum mainnet, most L2s), pending `take` transactions are visible and front-runnable.

---

### Recommendation

Introduce a partial-fill tolerance or a "max units per take" cap so that a taker's transaction succeeds for whatever capacity remains, rather than reverting entirely. Concretely:

- In the `maxUnits` branch, cap `units` to `min(units, offer.maxUnits - consumed[offer.maker][offer.group])` before incrementing, and revert only if the resulting fill is 0 (or below a caller-specified minimum).
- Alternatively, allow `setConsumed` to *decrease* consumed (with appropriate access control), giving makers a recovery path without requiring offer cancellation.

---

### Proof of Concept

**Setup:**
- Loan token: `USDC` (6 decimals, 1 unit = 1e-6 USDC)
- Maker posts sell offer: `maxUnits = 1_000_000`, `group = bytes32(0)`, `tick = 2910` (price ≈ 0.5 WAD)
- Current `consumed[maker][0] = 0`

**Steps:**

1. Legitimate taker broadcasts `take(offer, ..., units = 1_000_000, ...)`.
2. Attacker sees the pending tx and calls `take(offer, ..., units = 1, ...)` with higher gas.
   - `buyerAssets = mulDivUp(1, ~0.5e18 + settlementFee, 1e18) = 1` (1 USDC unit = $0.000001)
   - `consumed[maker][0]` becomes `1`.
3. Legitimate taker's tx executes: `newConsumed = 1 + 1_000_000 = 1_000_001 > 1_000_000` → reverts `ConsumedUnits()`.
4. Maker calls `setConsumed(0, type(uint256).max, maker)` to cancel, re-creates offer with `group = bytes32(1)`.
5. Attacker repeats step 2 on the new offer. Cycle continues indefinitely. [1](#0-0) [2](#0-1) [5](#0-4)

### Citations

**File:** src/Midnight.sol (L366-373)
```text
        uint256 newConsumed;
        if (offer.maxAssets > 0) {
            newConsumed = consumed[offer.maker][offer.group] += offer.buy ? buyerAssets : sellerAssets;
            require(newConsumed <= offer.maxAssets, ConsumedAssets());
        } else {
            newConsumed = consumed[offer.maker][offer.group] += units;
            require(newConsumed <= offer.maxUnits, ConsumedUnits());
        }
```

**File:** src/Midnight.sol (L723-728)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```

**File:** src/libraries/TickLib.sol (L44-51)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
```

**File:** src/libraries/ConstantsLib.sol (L8-8)
```text
uint256 constant WAD = 1e18;
```

**File:** src/interfaces/IMidnight.sol (L22-36)
```text
    Market market;
    bool buy;
    address maker;
    uint256 start;
    uint256 expiry;
    uint256 tick;
    bytes32 group;
    address callback;
    bytes callbackData;
    address receiverIfMakerIsSeller;
    address ratifier;
    bool reduceOnly;
    uint256 maxUnits;
    uint256 maxAssets; // buyerAssets if offer.buy else sellerAssets
}
```
