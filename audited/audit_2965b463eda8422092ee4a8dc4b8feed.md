### Title
EcrecoverRatifier Accepts Any Midnight-Authorized Signer as Valid Offer-Tree Signer, Enabling Unauthorized Offer Creation on Behalf of Maker - (File: src/ratifiers/EcrecoverRatifier.sol)

### Summary
`EcrecoverRatifier.isRatified` validates the recovered signer against `IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer)`, which is the same general-purpose authorization mapping used for all Midnight operations. Any account that holds a general Midnight authorization for the maker can therefore construct and sign an arbitrary Merkle tree of offers, have those offers accepted by `Midnight.take`, force the maker into debt, and accumulate credit that can later be redeemed via `withdraw`. The invariant that "signatures bind the right user/market/action/amount/deadline" is broken because offer-signing authority is not scoped separately from general operational authority.

### Finding Description

**Exact code path:**

`EcrecoverRatifier.isRatified` (line 44):
```solidity
require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
``` [1](#0-0) 

`Midnight.take` checks (lines 355–356) that the maker has authorized the ratifier, then delegates all signing validation to the ratifier:
```solidity
require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
``` [2](#0-1) 

The consumed update happens **after** the ratifier check (lines 366–373), so the question's premise about ordering is incorrect — but the core vulnerability is independent of ordering: [3](#0-2) 

**Root cause:** `EcrecoverRatifier` reuses `isAuthorized[maker][signer]` — the same mapping that governs all Midnight operations (position management, `setConsumed`, `setIsAuthorized`, etc.) — as the sole gate for offer-tree signing. There is no separate, scoped signing-key registry in `EcrecoverRatifier`. [4](#0-3) 

**Attacker-controlled inputs:**
- `offer.maker` = victim maker (attacker sets this field freely)
- `offer.buy = false` → `(buyer, seller) = (taker, offer.maker)` → attacker is buyer, maker is seller
- `offer.group`, `offer.maxUnits` = attacker-chosen values
- `ratifierData` = attacker-constructed Merkle tree root + proof + attacker's own signature over that root

**Exploit flow:**

Preconditions:
- `isAuthorized[maker][EcrecoverRatifier] = true` (maker uses EcrecoverRatifier as their ratifier)
- `isAuthorized[maker][attacker] = true` (attacker holds general Midnight authorization for maker — obtained legitimately or via `EcrecoverAuthorizer`)

Steps:
1. Attacker constructs `offer = {maker: maker, buy: false, ratifier: EcrecoverRatifier, group: G, maxUnits: N, ...}`.
2. Attacker builds a Merkle tree with this offer as a leaf and signs the root with their own EOA key.
3. Attacker calls `Midnight.take(offer, ratifierData, units, attacker, ...)`.
4. Line 355 passes: `isAuthorized[maker][EcrecoverRatifier] == true`.
5. `EcrecoverRatifier.isRatified` is called: the offer is a valid leaf (attacker built the tree), root is not canceled, recovered signer = attacker, `isAuthorized[maker][attacker] == true` → **passes**.
6. `consumed[maker][G] += units` — group budget consumed without maker's knowledge.
7. Attacker (buyer) receives `buyerCreditIncrease` credit; maker (seller) receives `sellerDebtIncrease` debt.
8. Attacker calls `withdraw()` once `_marketState.withdrawable >= units` (see Impact).

**Why existing checks fail:**
- Line 355 only verifies the maker authorized the *ratifier contract*, not the *signer*.
- `EcrecoverRatifier` has no separate signing-key registry; it delegates entirely to `isAuthorized`.
- The Midnight documentation (lines 105–109) warns that "other contracts might re-use Midnight's authorization mapping too (e.g. ratifiers and authorizers)" — this is a documented risk, but `EcrecoverRatifier` concretely instantiates it as an exploitable path. [5](#0-4) 

### Impact Explanation

**Immediate (no additional conditions):** The maker is forced into debt they never agreed to; `consumed[maker][group]` is incremented, exhausting the group budget and blocking the maker's own legitimate offers from filling.

**Deferred (requires `withdrawable >= units`):** The attacker's credit can be redeemed via `withdraw()`. `withdrawable` is increased by `repay` or `liquidate` — both of which are reachable: the maker may repay their forced debt, or the attacker can liquidate the maker if the forced debt renders them unhealthy. In a market with pre-existing withdrawable assets the redemption is immediate. [6](#0-5) 

### Likelihood Explanation

**Preconditions:** The maker must have authorized the attacker on Midnight (`isAuthorized[maker][attacker] = true`) and must use `EcrecoverRatifier` as their ratifier. Both are realistic: the authorization may have been granted for a different purpose (e.g., position management via a smart contract), and `EcrecoverRatifier` is the canonical off-chain-signing ratifier. The `EcrecoverAuthorizer` peripheral makes granting such authorizations via signed messages straightforward. [7](#0-6) 

**Repeatability:** The attack can be repeated for any group until the maker revokes the attacker's Midnight authorization. Each repetition forces additional debt and consumes more group budget.

### Recommendation

`EcrecoverRatifier` must maintain its own independent signing-key registry, separate from Midnight's general `isAuthorized` mapping. Replace line 44 with a check against a dedicated `isSigningAuthorized[maker][signer]` mapping that is set only through `EcrecoverRatifier`-specific functions (not inherited from Midnight authorization). The `cancelRoot` function has the same issue and must be updated consistently. [8](#0-7) 

### Proof of Concept

```solidity
// Foundry unit test outline
function test_authorizedSignerCanRatifyArbitraryOffer() public {
    // Setup
    address maker = address(0xA);
    address attacker = address(0xB); // has isAuthorized[maker][attacker] = true

    // Maker authorizes EcrecoverRatifier and attacker on Midnight
    vm.prank(maker);
    midnight.setIsAuthorized(address(ecrecoverRatifier), true, maker);
    vm.prank(maker);
    midnight.setIsAuthorized(attacker, true, maker);

    // Attacker constructs a sell offer (maker=seller, attacker=buyer) with large maxUnits
    Offer memory offer = Offer({
        maker: maker,
        buy: false,
        group: bytes32("group1"),
        maxUnits: 1_000_000e18,
        maxAssets: 0,
        ratifier: address(ecrecoverRatifier),
        // ... other fields
    });

    // Attacker builds Merkle tree with this offer and signs root with attacker's key
    bytes32 root = buildMerkleRoot(offer);
    bytes memory ratifierData = buildRatifierData(attackerPrivKey, root, offer);

    uint256 units = 500_000e18;

    // Attacker calls take — should revert but does not
    vm.prank(attacker);
    midnight.take(offer, ratifierData, units, attacker, address(0), address(0), "");

    // Assertions
    assertEq(midnight.consumed(maker, bytes32("group1")), units); // group budget consumed
    assertGt(midnight.debtOf(marketId, maker), 0);               // maker has unwanted debt
    assertGt(midnight.creditOf(marketId, attacker), 0);          // attacker has credit

    // If market has withdrawable assets, attacker can redeem
    // vm.prank(attacker);
    // midnight.withdraw(market, units, attacker, attacker);
    // assertEq(loanToken.balanceOf(attacker), units);
}
```

Expected assertions: `consumed[maker][group] == units`, `debtOf(maker) > 0`, `creditOf(attacker) > 0`. The test should pass (demonstrating the bug) — a correct implementation would revert at the `isRatified` check.

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L18-46)
```text
contract EcrecoverRatifier is IEcrecoverRatifier {
    address public immutable MIDNIGHT;

    mapping(address maker => mapping(bytes32 root => bool)) public isRootCanceled;

    constructor(address _midnight) {
        MIDNIGHT = _midnight;
    }

    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }

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
    }
```

**File:** src/Midnight.sol (L105-109)
```text
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
/// - Under which conditions the account can return CALLBACK_SUCCESS when its isRatified function is called.
```

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

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

**File:** src/Midnight.sol (L481-499)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-48)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
        require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());

        bytes32 hashStruct = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, hashStruct));
        address signer = ecrecover(digest, signature.v, signature.r, signature.s);
        require(signer != address(0), InvalidSignature());
        require(
            signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
            Unauthorized()
        );

        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );

        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
    }
```
