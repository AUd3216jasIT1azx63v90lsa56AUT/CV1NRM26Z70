### Title
Stale Root Ratification in `SetterRatifier` Persists After Account De-authorization — (File: `src/ratifiers/SetterRatifier.sol`)

---

### Summary

`SetterRatifier` allows any account authorized by a maker to ratify a Merkle root of offers on the maker's behalf. When the maker later de-authorizes that account on `Midnight`, the root ratification is **not cleared**. The `isRatified` check only reads `isRootRatified[offer.maker][root]` without verifying that the account which originally set the ratification is still authorized. Offers under the stale root remain executable indefinitely.

---

### Finding Description

**Root cause — `SetterRatifier.sol` lines 24–27 and 30–37:**

```solidity
// Anyone authorized by the maker can ratify a root
function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;
    ...
}

// isRatified only checks the stored flag — no live authorization check
function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    ...
    require(isRootRatified[offer.maker][root], NotRatified());   // ← stale flag, never cleared on de-auth
    return CALLBACK_SUCCESS;
}
``` [1](#0-0) [2](#0-1) 

**Contrast with `EcrecoverRatifier`**, which performs a **live** authorization check at execution time and is therefore not affected:

```solidity
require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
``` [3](#0-2) 

**Attack path:**

1. Maker authorizes `AccountA` on `Midnight` (`isAuthorized[maker][AccountA] = true`).
2. `AccountA` (compromised or malicious) constructs a Merkle tree containing offers at prices unfavorable to the maker (e.g., buy offers at an inflated price) and calls `setIsRootRatified(maker, maliciousRoot, true)`.
3. Maker discovers the compromise and calls `setIsAuthorized(AccountA, false, maker)` — revoking `AccountA`.
4. `isRootRatified[maker][maliciousRoot]` is **never touched** by the de-authorization; it remains `true`.
5. A taker calls `Midnight.take(offer, ratifierData, units, ...)` with an offer from the malicious tree. `SetterRatifier.isRatified` passes because `isRootRatified[maker][maliciousRoot] == true`.
6. `Midnight.take` resolves the payer as the maker (`payer = offer.buy ? buyer : msg.sender` when no callback is set) and pulls `buyerAssets` from the maker via `safeTransferFrom`. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

A compromised authorized account can ratify a root containing offers at any price. After the maker revokes that account, those offers remain live. If the maker has approved `Midnight` to spend their loan token (standard for active makers), the attacker can drain the maker's tokens by taking the malicious buy offers at an inflated price. The maker has no automatic protection — they must also manually call `setIsRootRatified(maker, root, false)` for every root the compromised account set, which they may not know about.

---

### Likelihood Explanation

Moderate. The scenario requires: (a) the maker delegates root management to a separate authorized account (a common operational pattern for hot-wallet or automated market-making setups), (b) that account is compromised or acts maliciously, and (c) the maker revokes the account without also un-ratifying its roots. The maker has no on-chain prompt to do so, and the `SetIsRootRatified` events emitted by the compromised account may go unnoticed. The maker's natural assumption — that revoking an account stops all its effects — is violated. [1](#0-0) 

---

### Recommendation

In `SetterRatifier`, store the address that ratified each root alongside the flag, and verify at execution time that the ratifier is still authorized:

```solidity
mapping(address maker => mapping(bytes32 root => address)) public rootRatifier;

function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    rootRatifier[maker][root] = newIsRootRatified ? msg.sender : address(0);
    ...
}

function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    ...
    address ratifier = rootRatifier[offer.maker][root];
    require(ratifier != address(0), NotRatified());
    require(
        ratifier == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, ratifier),
        Unauthorized()
    );
    return CALLBACK_SUCCESS;
}
```

This mirrors the live-check pattern already used in `EcrecoverRatifier` and ensures that de-authorizing an account immediately invalidates all roots it set.

---

### Proof of Concept

```
1. maker approves Midnight to spend 1,000,000 USDC.
2. maker calls Midnight.setIsAuthorized(accountA, true, maker).
3. accountA (attacker) builds a Merkle tree with one offer:
       Offer { buy: true, maker: maker, tick: MAX_TICK, maxAssets: 1_000_000e6, ... }
   (MAX_TICK → price ≈ WAD, so buyerAssets ≈ units; maker pays full face value for credit)
4. accountA calls SetterRatifier.setIsRootRatified(maker, maliciousRoot, true).
5. maker calls Midnight.setIsAuthorized(accountA, false, maker).
   → isRootRatified[maker][maliciousRoot] is still true.
6. attacker calls Midnight.take(maliciousOffer, ratifierData, units, attacker, ...).
   → SetterRatifier.isRatified passes (stale flag).
   → Midnight pulls buyerAssets from maker via safeTransferFrom.
   → attacker receives sellerAssets; maker's USDC is drained.
```

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L24-27)
```text
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

**File:** src/ratifiers/EcrecoverRatifier.sol (L44-44)
```text
        require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
```

**File:** src/Midnight.sol (L420-423)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
        address receiver = offer.buy ? receiverIfTakerIsSeller : offer.receiverIfMakerIsSeller;
```

**File:** src/Midnight.sol (L455-456)
```text
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```
