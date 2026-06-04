Looking at the external report's vulnerability class and mapping it to Midnight's architecture:

**Vulnerability class**: Missing validation of market existence/parameters when creating a position, leading to broken state where funds are permanently locked.

**Midnight's design difference**: In TellerV2, bids are stored on-chain with parameters (`bidDefaultDuration`, `paymentCycle`) derived from the market at bid-creation time. If the market doesn't exist, those fields are 0, and the stored bid cannot be updated when the market is later created.

In Midnight, there is no on-chain bid storage. The full `Market` struct is embedded directly in every off-chain `Offer`. The market ID is `keccak256(0xff ++ midnight ++ chainId ++ keccak256(SSTORE2_PREFIX ++ abi.encode(market)))` — derived from the complete `Market` struct. [1](#0-0) 

When `take()` is called, `touchMarket(offer.market)` creates the market using exactly the parameters embedded in the offer. [2](#0-1)  There is no "market created later with different parameters" scenario — the market ID and parameters are always consistent with the offer.

**Checking `touchMarket()` for missing validations**: The function validates `maturity <= block.timestamp + 100 years`, collateral params sorted, LLTV allowed, and maxLif valid. It does NOT check `maturity >= block.timestamp`. <cite repo="Thismortalcoilf/midnight--002" path="

### Citations

**File:** src/libraries/IdLib.sol (L25-31)
```text
    function toId(Market memory market, uint256 chainId, address midnight) internal pure returns (bytes32) {
        return keccak256(
            abi.encodePacked(
                uint8(0xff), midnight, chainId, keccak256(abi.encodePacked(SSTORE2_PREFIX, abi.encode(market)))
            )
        );
    }
```

**File:** src/Midnight.sol (L347-348)
```text
        bytes32 id = touchMarket(offer.market);
        MarketState storage _marketState = marketState[id];
```
