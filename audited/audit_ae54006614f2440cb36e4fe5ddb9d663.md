### Title
`tickToPrice(0)` Returns Zero, Allowing Takers to Acquire Credit Units for Free from Sell Offers at Tick 0 — (File: `src/libraries/TickLib.sol`)

---

### Summary

`TickLib.tickToPrice(0)` returns `0` due to rounding in the two-step `divHalfDownUnchecked` computation. Tick `0` is a valid, always-accessible tick (passes both the range check and the tick-spacing modulo check). When a maker publishes a sell offer at tick `0`, the `take()` function computes `sellerAssets = 0`, meaning the maker receives zero loan tokens while surrendering credit units (or taking on debt). A taker can exploit any such offer to acquire credit units for free.

---

### Finding Description

**Root cause — `tickToPrice(0) = 0`:** [1](#0-0) 

At tick `0` the exponent argument is `LN_ONE_PLUS_DELTA × (2910 − 0) ≈ 14.514e18`, giving `wExp(14.514e18) ≈ 2.009e24`. The intermediate result is:

```
step1 = divHalfDownUnchecked(1e36, 1e18 + 2.009e24)
      = (1e36 + ~1.005e24) / 2.009e24
      ≈ 4.978e11
```

Then:

```
step2 = divHalfDownUnchecked(4.978e11, 1e12)
      = (4.978e11 + 4.999e11) / 1e12
      = 9.977e11 / 1e12
      = 0   ← integer division truncates
```

Final result: `0 * PRICE_ROUNDING_STEP = 0`.

Tick `1` gives `step1 ≈ 5.025e11`, `step2 = 1`, price `= 1e12`. So tick `0` is the sole tick that collapses to price `0`, and the jump from tick `1` (price `1e-6 WAD`) to tick `0` (price `0`) is undocumented. The file comment states *"Minimum representable price increment in WAD (1e-6 WAD)"*, implying the floor is `1e-6 WAD`, not `0`. [2](#0-1) 

**Tick 0 is always accessible:** [3](#0-2) 

`0 % tickSpacing == 0` for every positive `tickSpacing`, so tick `0` always passes the accessibility check.

**Zero-price propagation in `take()`:** [4](#0-3) 

For a sell offer (`offer.buy = false`) at tick `0`:

```
offerPrice  = tickToPrice(0) = 0
sellerPrice = offerPrice     = 0          // line 361
buyerPrice  = 0 + _settlementFee          // line 362
sellerAssets = units.mulDivUp(0, WAD) = 0 // line 364
```

Even when `_settlementFee > 0`, `sellerAssets` is always `0`. The maker receives **zero loan tokens** regardless of the settlement fee level.

The token transfer that pays the maker: [5](#0-4) 

transfers `sellerAssets = 0` to the maker's receiver.

Meanwhile, the taker's credit position is updated normally: [6](#0-5) 

The taker gains `buyerCreditIncrease` credit units backed by the maker's debt, paying at most `_settlementFee` per unit (which goes entirely to the protocol, not the maker).

---

### Impact Explanation

A maker who publishes a sell offer at tick `0` — whether by mistake, through a misconfigured smart contract, or because they misread the tick-to-price mapping — will have their offer taken for zero consideration:

- **Maker loss:** surrenders credit units (or takes on new debt) and receives `0` loan tokens. If the maker had existing credit, that credit is destroyed with no compensation. If the maker had no credit, they take on debt they cannot service (they received nothing to repay it with), leading to eventual liquidation and collateral seizure.
- **Taker gain:** acquires credit units redeemable at maturity for real loan tokens, paying only the settlement fee (if any) to the protocol — not to the maker.

The protocol's accounting remains internally consistent (maker debt = taker credit), but the maker suffers a direct, unrecoverable financial loss.

---

### Likelihood Explanation

- Tick `0` is always accessible; no configuration can block it.
- The `PRICE_ROUNDING_STEP` comment misleads integrators into believing the minimum price is `1e-6 WAD`, not `0`.
- Off-chain routing software or smart-contract makers that iterate over the tick grid starting from `0` will silently produce a zero-price offer.
- A taker monitoring the mempool or off-chain offer feed for tick-`0` sell offers can front-run any legitimate taker and drain the maker's position at zero cost.

---

### Recommendation

Add a zero-price guard in `tickToPrice` or in `take()`:

**Option A — enforce minimum price in `tickToPrice`:**
```solidity
function tickToPrice(uint256 tick) internal pure returns (uint256) {
    require(tick <= MAX_TICK, TickOutOfRange());
    unchecked {
        uint256 price = uint256(1e36)
            .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
            .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        require(price > 0, PriceTooLow()); // or return PRICE_ROUNDING_STEP if price == 0
        return price;
    }
}
```

**Option B — guard in `take()`:**
```solidity
uint256 offerPrice = TickLib.tickToPrice(offer.tick);
require(offerPrice > 0, ZeroPrice());
```

Either approach prevents zero-price offers from being created or executed.

---

### Proof of Concept

```solidity
// Setup: market with default (zero) settlement fees
// Maker (Alice) has 1000 credit units in the market
// Alice publishes a sell offer at tick 0, maxUnits = 1000

// Attacker (Bob) calls:
midnight.take(
    Offer({
        market: market,
        tick: 0,          // tickToPrice(0) == 0
        buy: false,       // sell offer: maker=seller, taker=buyer
        maker: alice,
        ...
    }),
    ratifierData,
    1000,                 // units
    bob,
    bob,
    address(0),
    ""
);

// Result:
// offerPrice  = 0
// sellerAssets = 0  → Alice receives 0 loan tokens
// buyerAssets  = 0  → Bob pays 0 loan tokens
// Alice's credit decreases by 1000 units
// Bob's credit increases by 1000 units
// Bob redeems 1000 loan tokens at maturity; Alice received nothing
``` [1](#0-0) [4](#0-3) [7](#0-6)

### Citations

**File:** src/libraries/TickLib.sol (L7-8)
```text
// Minimum representable price increment in WAD (1e-6 WAD). Tick prices are rounded to multiples of this value.
uint256 constant PRICE_ROUNDING_STEP = 1e12;
```

**File:** src/libraries/TickLib.sol (L44-52)
```text
    function tickToPrice(uint256 tick) internal pure returns (uint256) {
        require(tick <= MAX_TICK, TickOutOfRange());
        unchecked {
            // forge-lint: disable-next-item(unsafe-typecast)
            return uint256(1e36)
                    .divHalfDownUnchecked(1e18 + wExp(LN_ONE_PLUS_DELTA * (int256(MAX_TICK / 2) - int256(tick))))
                    .divHalfDownUnchecked(PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP;
        }
    }
```

**File:** src/Midnight.sol (L351-351)
```text
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
```

**File:** src/Midnight.sol (L358-364)
```text
        uint256 offerPrice = TickLib.tickToPrice(offer.tick);
        uint256 timeToMaturity = UtilsLib.zeroFloorSub(offer.market.maturity, block.timestamp);
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
        uint256 buyerAssets = offer.buy ? units.mulDivDown(buyerPrice, WAD) : units.mulDivUp(buyerPrice, WAD);
        uint256 sellerAssets = offer.buy ? units.mulDivDown(sellerPrice, WAD) : units.mulDivUp(sellerPrice, WAD);
```

**File:** src/Midnight.sol (L408-410)
```text
        buyerPos.debt -= UtilsLib.toUint128(units - buyerCreditIncrease);
        buyerPos.pendingFee += buyerPendingFeeIncrease;
        buyerPos.credit += UtilsLib.toUint128(buyerCreditIncrease);
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```
