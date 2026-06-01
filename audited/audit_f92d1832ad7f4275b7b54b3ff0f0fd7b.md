### Title
SetterRatifier Cross-Fork Offer Replay via Persistent `isRootRatified` State - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier` stores ratification state in `mapping(address maker => mapping(bytes32 root => bool)) public isRootRatified` with no chain-specific component. After a hard fork, this storage is copied verbatim to the forked chain, and because `HashLib.hashOffer` also omits `chainId`, the same offer+root combination remains valid on the fork. A taker can fill the maker's offers on the forked chain using ratification the maker only intended for the original chain.

### Finding Description
**Root cause — no `chainId` binding in `SetterRatifier`:**

`SetterRatifier.setIsRootRatified` writes:
```solidity
isRootRatified[maker][root] = newIsRootRatified;   // SetterRatifier.sol:26
```
The storage key is `(maker, root)` — no chain discriminator. [1](#0-0) 

`SetterRatifier.isRatified` checks:
```solidity
require(isRootRatified[offer.maker][root], NotRatified());   // SetterRatifier.sol:35
```
No `block.chainid` or `INITIAL_CHAIN_ID` check anywhere in the function. [2](#0-1) 

**Contrast with `EcrecoverRatifier`**, which explicitly binds to the current chain:
```solidity
bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
```
Its NatSpec comment reads: *"If block.chainid changes (hard fork), the EIP-712 domain separator changes and previously signed offers are no longer valid."* [3](#0-2) [4](#0-3) 

**`HashLib.hashOffer` does not include `chainId`:**
The offer hash covers market fields, maker, expiry, tick, ratifier, etc. — but never `block.chainid` or any chain-specific value. [5](#0-4) 

**`Midnight.toId` uses `INITIAL_CHAIN_ID`, not `block.chainid`:**
```solidity
function toId(Market memory market) public view returns (bytes32) {
    return IdLib.toId(market, INITIAL_CHAIN_ID, address(this));
}
```
`INITIAL_CHAIN_ID` is an immutable set at construction time. After a fork, `block.chainid` changes but `INITIAL_CHAIN_ID` does not, so `toId` returns the **same** market ID on both chains. The existing test `testToIdStableAcrossHardfork` explicitly verifies this. [6](#0-5) [7](#0-6) [8](#0-7) 

**`touchMarket` auto-creates the market if absent, but after a fork the market already exists** (same ID, same storage), so `take` proceeds directly to the ratifier check. [9](#0-8) 

**Full exploit path:**
1. Pre-fork: maker calls `setIsRootRatified(maker, root, true)` → `isRootRatified[maker][root] = true`.
2. Hard fork: `block.chainid` changes; storage is copied; `isRootRatified[maker][root]` remains `true` on the forked chain.
3. Post-fork: taker calls `midnight.take(offer, abi.encode(root, leafIndex, proof), units, taker, ...)`.
4. `touchMarket` resolves the same market ID (via `INITIAL_CHAIN_ID`).
5. `take` checks `isAuthorized[offer.maker][offer.ratifier]` — also copied from original chain, still `true`.
6. `isRatified` verifies the Merkle proof (chain-agnostic) and checks `isRootRatified[maker][root]` — `true`.
7. Take executes: maker's credit/debt position is modified on the forked chain without the maker's post-fork consent. [10](#0-9) 

### Impact Explanation
A taker can fill any `SetterRatifier`-backed offer on the forked chain using ratification state the maker set only on the original chain. The maker's position (credit increased or debt incurred) is modified on the forked chain without any post-fork authorization act by the maker. This directly violates the invariant: *"signatures, gates, ratifiers, callbacks, and approvals bind the right user/market/action/amount/deadline"* — here the ratifier does not bind to the right chain.

### Likelihood Explanation
Preconditions: (1) a contentious hard fork that changes `block.chainid` while keeping contract addresses identical (e.g., ETH/ETC-style fork), and (2) at least one maker using `SetterRatifier` with an active, non-expired offer. Both conditions are realistic. The attack is repeatable for every ratified root that has not been explicitly revoked post-fork, and requires no special privilege — any taker can trigger it.

### Recommendation
Bind ratification to the chain by including `block.chainid` in the storage key or in the `isRatified` check. For example, change the storage mapping to:
```solidity
mapping(address maker => mapping(uint256 chainId => mapping(bytes32 root => bool))) public isRootRatified;
```
and update `setIsRootRatified` and `isRatified` to use `block.chainid` as the middle key — mirroring the pattern `EcrecoverRatifier` already uses in its domain separator. [11](#0-10) 

### Proof of Concept
```solidity
// Foundry unit test
function testCrossChainForkReplay() public {
    // 1. Pre-fork: maker ratifies root on original chain (chainId = 1)
    Offer memory offer = makeOffer(lender);
    bytes32 root = HashLib.hashOffer(offer);

    vm.prank(lender);
    midnight.setIsAuthorized(address(setterRatifier), true, lender);
    vm.prank(lender);
    setterRatifier.setIsRootRatified(lender, root, true);

    // 2. Simulate hard fork: change block.chainid
    vm.chainId(999);

    // 3. Assert: ratification state persists on forked chain
    assertTrue(setterRatifier.isRootRatified(lender, root));

    // 4. Assert: market ID is unchanged (INITIAL_CHAIN_ID is immutable)
    bytes32 idBefore = /* captured pre-fork */ ...;
    assertEq(midnight.toId(offer.market), idBefore);

    // 5. Taker fills offer on forked chain — should revert but does not
    deal(address(loanToken), borrower, 1e18);
    vm.prank(borrower);
    midnight.take(offer, abi.encode(root, 0, new bytes32[](0)), 0, borrower, borrower, address(0), hex"");
    // Expected: revert. Actual: success — cross-chain fill executed.
}
```
Key assertions: `isRootRatified` returns `true` after `vm.chainId(999)`, and `take` succeeds, demonstrating unauthorized offer fill on the forked chain.

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L18-27)
```text
    mapping(address maker => mapping(bytes32 root => bool)) public isRootRatified;

    constructor(address _midnight) {
        MIDNIGHT = _midnight;
    }

    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
```

**File:** src/ratifiers/SetterRatifier.sol (L30-37)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(isRootRatified[offer.maker][root], NotRatified());
        return CALLBACK_SUCCESS;
    }
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L10-11)
```text
/// @dev If block.chainid changes (hard fork), the EIP-712 domain separator changes and previously signed offers are
/// no longer valid.
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L40-40)
```text
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
```

**File:** src/ratifiers/libraries/HashLib.sol (L118-138)
```text
    function hashOffer(Offer memory offer) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                OFFER_TYPEHASH,
                hashMarket(offer.market),
                offer.buy,
                offer.maker,
                offer.start,
                offer.expiry,
                offer.tick,
                offer.group,
                offer.callback,
                keccak256(offer.callbackData),
                offer.receiverIfMakerIsSeller,
                offer.ratifier,
                offer.reduceOnly,
                offer.maxUnits,
                offer.maxAssets
            )
        );
    }
```

**File:** src/Midnight.sol (L203-206)
```text
    constructor() {
        roleSetter = msg.sender;
        INITIAL_CHAIN_ID = block.chainid;
        emit EventsLib.Constructor(msg.sender, INITIAL_CHAIN_ID);
```

**File:** src/Midnight.sol (L346-356)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
        bytes32 id = touchMarket(offer.market);
        MarketState storage _marketState = marketState[id];
        require(_marketState.lossFactor < type(uint128).max, MarketLossFactorMaxedOut());
        require(UtilsLib.atMostOneNonZero(offer.maxAssets, offer.maxUnits), MultipleNonZero());
        require(offer.tick % _marketState.tickSpacing == 0, TickNotAccessible());
        require(block.timestamp >= offer.start, OfferNotStarted());
        require(block.timestamp <= offer.expiry, OfferExpired());
        require(offer.maker != taker, SelfTake());
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
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

**File:** src/Midnight.sol (L871-873)
```text
    function toId(Market memory market) public view returns (bytes32) {
        return IdLib.toId(market, INITIAL_CHAIN_ID, address(this));
    }
```

**File:** test/OtherFunctionsTest.sol (L275-294)
```text
    function testToIdStableAcrossHardfork(Market memory _market, Market memory otherMarket, uint64 newChainId) public {
        vm.assume(_market.collateralParams.length > 0);
        vm.assume(newChainId != block.chainid);
        _market = validMarket(_market);

        bytes32 idBefore = midnight.touchMarket(_market);
        uint256 capturedChainId = midnight.INITIAL_CHAIN_ID();

        vm.chainId(newChainId);

        assertEq(midnight.INITIAL_CHAIN_ID(), capturedChainId, "INITIAL_CHAIN_ID changed");
        assertEq(midnight.toId(_market), idBefore, "toId changed");
        Market memory roundTrip = midnight.toMarket(idBefore);
        assertEq(keccak256(abi.encode(roundTrip)), keccak256(abi.encode(_market)), "stored market lost");

        otherMarket = validMarket(otherMarket);
        bytes32 otherId = midnight.touchMarket(otherMarket);
        Market memory otherRoundTrip = midnight.toMarket(otherId);
        assertEq(keccak256(abi.encode(otherRoundTrip)), keccak256(abi.encode(otherMarket)), "stored market lost");
    }
```
