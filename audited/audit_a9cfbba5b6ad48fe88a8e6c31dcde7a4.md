### Title
Permissionless `touchMarket` Enables Front-Running of `setDefaultSettlementFee` to Permanently Capture Zero-Fee Markets — (File: `src/Midnight.sol`)

---

### Summary

`touchMarket` is a public, permissionless function that creates a market by snapshotting the **current** `defaultSettlementFeeCbp` values at creation time. Because Solidity mappings default to zero, any market created before the fee setter calls `setDefaultSettlementFee` is permanently initialized with zero settlement fees. An attacker can front-run the fee setter's `setDefaultSettlementFee` transaction to force a target market into existence with zero fees. The fee setter's transaction then only affects future markets; the already-created market retains zero fees until the fee setter separately calls `setMarketSettlementFee` — and all trades that execute in the window pay zero protocol fees.

---

### Finding Description

**Root cause — `touchMarket` snapshots defaults at creation time:** [1](#0-0) 

```solidity
MarketState storage _marketState = marketState[id];
_marketState.tickSpacing = DEFAULT_TICK_SPACING;
uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
_marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
_marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
// ... (all 7 breakpoints)
_marketState.continuousFee = defaultContinuousFee[market.loanToken];
```

`defaultSettlementFeeCbp` is a plain mapping: [2](#0-1) 

Solidity initializes all mapping entries to zero. Until the fee setter calls `setDefaultSettlementFee`, every `defaultSettlementFeeCbp[loanToken][i]` is `0`.

**`touchMarket` is fully public — no access control:** [3](#0-2) 

**`setDefaultSettlementFee` only affects markets created after it executes:** [4](#0-3) 

There is no retroactive update to already-created markets.

**Exploit path:**

1. Fee setter submits `setDefaultSettlementFee(loanToken, index, newFee)` to the mempool.
2. Attacker observes the pending transaction and submits `touchMarket(market)` — using any valid `Market` struct with `loanToken` — at a higher gas price.
3. Attacker's transaction lands first: market is created with all `settlementFeeCbpN = 0` and `continuousFee = 0`.
4. Fee setter's transaction lands: defaults are updated, but only for markets created **after** this point.
5. All trades in the attacker's market execute with zero settlement fee: [5](#0-4) 

```solidity
uint256 _settlementFee = settlementFee(id, timeToMaturity);
uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
uint256 buyerPrice = sellerPrice + _settlementFee;
```

With `_settlementFee = 0`, `claimableSettlementFee` receives nothing: [6](#0-5) 

```solidity
claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets; // = 0
```

6. Protocol permanently loses settlement fees on every trade in that market until the fee setter discovers the issue and calls `setMarketSettlementFee`.

---

### Impact Explanation

Settlement fees are the protocol's primary revenue stream. Every trade in a zero-fee market contributes nothing to `claimableSettlementFee`. Fees lost on trades that execute before the fee setter's remediation call are **permanently unrecoverable** — there is no mechanism to retroactively collect fees on past trades. For a high-volume market (e.g., a popular loan token / collateral pair near maturity), this represents a direct, quantifiable financial loss to the protocol. The fee setter also incurs extra gas to discover and fix the per-market fee with `setMarketSettlementFee`.

---

### Likelihood Explanation

The attack requires no privileged access. Any Ethereum address can call `touchMarket`. The attacker only needs to:
- Watch the mempool for a `setDefaultSettlementFee` transaction.
- Construct a valid `Market` struct using the target `loanToken` (parameters are observable on-chain or from off-chain offer data).
- Submit `touchMarket` with a higher gas price.

This is a standard mempool front-run with no capital requirement beyond gas. The attack is especially easy at protocol launch, when default fees are zero and the fee setter is expected to configure them.

---

### Recommendation

The fee setter should use `multicall` to atomically set default fees and create the market in a single transaction, eliminating the front-running window:

```solidity
bytes[] memory calls = new bytes[](2);
calls[0] = abi.encodeCall(IMidnight.setDefaultSettlementFee, (loanToken, 6, 0.005e18));
calls[1] = abi.encodeCall(IMidnight.touchMarket, (market));
midnight.multicall(calls);
``` [7](#0-6) 

Alternatively, emit a dedicated event when a market is created with zero fees so off-chain monitoring can alert the fee setter immediately.

---

### Proof of Concept

```
1. Deploy Midnight. defaultSettlementFeeCbp[loanToken] = [0,0,0,0,0,0,0] (mapping default).

2. Fee setter submits:
   setDefaultSettlementFee(loanToken, 6, 0.005e18)  // gas price: 10 gwei

3. Attacker observes the pending tx and submits:
   touchMarket(Market{loanToken, collateralParams, maturity, ...})  // gas price: 15 gwei

4. Attacker's tx mines first:
   marketState[id].settlementFeeCbp6 = 0  (copied from zero default)
   marketState[id].tickSpacing = 4        (market now exists)

5. Fee setter's tx mines:
   defaultSettlementFeeCbp[loanToken][6] = 5000  (0.005e18 / CBP)
   — but marketState[id] is unchanged.

6. Trades execute in the market:
   settlementFee(id, timeToMaturity) = 0
   claimableSettlementFee[loanToken] += 0  (no revenue)

7. Protocol has permanently lost settlement fees on all trades until
   feeSetter calls setMarketSettlementFee(id, 6, 0.005e18).
```

### Citations

**File:** src/Midnight.sol (L193-193)
```text
    mapping(address loanToken => uint16[7]) public defaultSettlementFeeCbp;
```

**File:** src/Midnight.sol (L211-220)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
    }
```

**File:** src/Midnight.sol (L277-284)
```text
    function setDefaultSettlementFee(address loanToken, uint256 index, uint256 newSettlementFee) external {
        require(msg.sender == feeSetter, OnlyFeeSetter());
        require(index <= 6, InvalidFeeIndex());
        require(newSettlementFee <= maxSettlementFee(index), SettlementFeeTooHigh());
        require(newSettlementFee % CBP == 0, FeeNotMultipleOfFeeCbp());
        // forge-lint: disable-next-item(unsafe-typecast) as newSettlementFee <= maxSettlementFee <= uint16.max * CBP
        defaultSettlementFeeCbp[loanToken][index] = uint16(newSettlementFee / CBP);
        emit EventsLib.SetDefaultSettlementFee(loanToken, index, newSettlementFee);
```

**File:** src/Midnight.sol (L360-362)
```text
        uint256 _settlementFee = settlementFee(id, timeToMaturity);
        uint256 sellerPrice = offer.buy ? offerPrice - _settlementFee : offerPrice;
        uint256 buyerPrice = sellerPrice + _settlementFee;
```

**File:** src/Midnight.sol (L418-418)
```text
        claimableSettlementFee[offer.market.loanToken] += buyerAssets - sellerAssets;
```

**File:** src/Midnight.sol (L755-757)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
```

**File:** src/Midnight.sol (L775-786)
```text
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
```
