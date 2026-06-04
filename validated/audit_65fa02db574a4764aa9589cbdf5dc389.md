### Title
Offer Cancellation via `cancelRoot` Can Be Front-Run by a Taker — (File: src/ratifiers/EcrecoverRatifier.sol)

### Summary

When a maker broadcasts a transaction to cancel an offer tree via `cancelRoot`, any taker monitoring the mempool can front-run that cancellation by calling `take()` with a valid offer from the same tree before the cancellation is mined. This is the direct analog of the reported "recovery cancellation can be front-run" pattern: `cancelRoot` maps to `cancel_recovery`, and `take()` maps to `finish_recovery`. The same race condition exists in `SetterRatifier.setIsRootRatified(maker, root, false)` and `Midnight.setConsumed(group, type(uint256).max, onBehalf)`.

---

### Finding Description

**Root cause — no atomicity between cancellation and execution.**

In `EcrecoverRatifier.sol`, a maker cancels an entire signed offer tree by calling:

```solidity
// src/ratifiers/EcrecoverRatifier.sol L27-31
function cancelRoot(address maker, bytes32 root) external {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootCanceled[maker][root] = true;
    emit CancelRoot(msg.sender, maker, root);
}
``` [1](#0-0) 

The `isRatified` function enforces the cancellation only after it is mined:

```solidity
// src/ratifiers/EcrecoverRatifier.sol L38
require(!isRootCanceled[offer.maker][root], RootCanceled());
``` [2](#0-1) 

Between the moment the maker broadcasts `cancelRoot` and the moment it is included in a block, the `isRootCanceled[maker][root]` flag is still `false`. Any taker who observes the pending `cancelRoot` transaction in the public mempool can submit a `take()` call referencing a valid offer from that tree with a higher gas price, causing it to be mined first.

The `take()` entry point in `Midnight.sol` calls `IRatifier(offer.ratifier).isRatified(...)` which will succeed because the root is not yet canceled:

```solidity
// src/Midnight.sol L355-356
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
``` [3](#0-2) 

The same race condition exists in:
- `SetterRatifier.setIsRootRatified(maker, root, false)` — deactivating a root can be front-run by a taker. [4](#0-3) 
- `Midnight.setConsumed(group, type(uint256).max, onBehalf)` — canceling all offers in a group can be front-run. [5](#0-4) 

---

### Impact Explanation

A maker who cancels because market conditions have moved against their signed price (e.g., the underlying asset price dropped significantly after signing the offer tree) will have their offer executed at the stale, unfavorable price. The financial loss is bounded by `units × |currentPrice − offerPrice|` across the full remaining capacity of the offer. For large offer trees with high `maxUnits` or `maxAssets`, this can be a substantial loss. The maker's intent to prevent execution is defeated.

---

### Likelihood Explanation

MEV bots routinely scan the public mempool for cancellation transactions on DeFi protocols and front-run them. No privileged access is required — any externally owned account or bot can call `take()` with a valid offer and proof. The attacker only needs:
1. A valid `Offer` struct and its Merkle proof (available off-chain from the maker's order book).
2. Sufficient loan token balance to pay `buyerAssets` (or a flash loan).
3. A higher gas price than the maker's `cancelRoot` transaction.

This is a standard, well-automated MEV strategy. Likelihood is high whenever a maker cancels a large or mispriced offer tree.

---

### Recommendation

Document this race condition explicitly in the `cancelRoot` (and `setIsRootRatified` / `setConsumed`) NatSpec so makers are aware. For makers who need guaranteed cancellation, recommend:

1. **Short expiry on offers**: Set `offer.expiry` to a near-future timestamp so the window for front-running is minimal.
2. **Atomic cancellation + position protection**: Use `multicall` to bundle `cancelRoot` with any protective action (e.g., `setConsumed`) in a single transaction, reducing (but not eliminating) the race window.
3. **Private mempool / flashbots**: Submit cancellation transactions via a private relay (e.g., Flashbots `eth_sendPrivateTransaction`) to prevent mempool visibility.

A protocol-level fix would require a commit-reveal or time-lock on offer execution, which conflicts with the protocol's design goals.

---

### Proof of Concept

**Setup:**
- Maker signs an offer tree containing `offer` (e.g., sell 1,000,000 units at tick T) and publishes the root off-chain.
- Market conditions shift: the offer is now significantly below fair value.
- Maker broadcasts: `cancelRoot(maker, root)` with gas price G.

**Attack:**
1. MEV bot observes the pending `cancelRoot` in the mempool.
2. Bot constructs a `take()` call: `take(offer, ratifierData, 1_000_000, bot, address(0), address(0), "")` where `ratifierData` encodes the valid Merkle proof for `offer` under `root`.
3. Bot submits `take()` with gas price G+1.
4. Block is mined: `take()` executes first (root still not canceled), then `cancelRoot` executes.

**Result:** The maker's offer is fully consumed at the stale price before the cancellation takes effect. The maker suffers a loss equal to the price difference times units filled. The `cancelRoot` succeeds but has no effect since `consumed[maker][group]` now equals `maxUnits`. [1](#0-0) [6](#0-5)

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L33-45)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (Signature memory sig, bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (Signature, bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(!isRootCanceled[offer.maker][root], RootCanceled());
        bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
        address _signer = ecrecover(digest, sig.v, sig.r, sig.s);
        require(_signer != address(0), InvalidSignature());
        require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
        return CALLBACK_SUCCESS;
```

**File:** src/Midnight.sol (L337-356)
```text
    function take(
        Offer memory offer,
        bytes memory ratifierData,
        uint256 units,
        address taker,
        address receiverIfTakerIsSeller,
        address takerCallback,
        bytes memory takerCallbackData
    ) external returns (uint256, uint256) {
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

**File:** src/Midnight.sol (L722-728)
```text
    /// @dev Passing type(uint256).max cancels all offers in the group (and never reverts).
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```
