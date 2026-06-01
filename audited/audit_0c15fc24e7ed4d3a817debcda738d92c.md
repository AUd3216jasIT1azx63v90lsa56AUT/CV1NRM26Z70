### Title
Cross-Chain Signature Replay via Shared Chain ID in EcrecoverRatifier - (File: src/ratifiers/EcrecoverRatifier.sol)

### Summary

`EcrecoverRatifier.isRatified` computes its EIP-712 domain separator using only `block.chainid` and `address(this)`. When two chains share the same chain ID (e.g., testnets, forks that did not update their chain ID) and the ratifier is deployed at the same address on both, a signature produced on chain A is cryptographically indistinguishable from a valid signature on chain B. An attacker can replay the maker's chain-A signature on chain B to fill the offer without the maker's intent, exhausting the maker's group budget on chain B and blocking legitimate fills there.

### Finding Description

**Root cause — line 40 of `src/ratifiers/EcrecoverRatifier.sol`:**

```solidity
bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
``` [1](#0-0) 

The domain separator is the only chain-binding element in the signature. It is identical on any two chains that share the same `block.chainid` value and where `EcrecoverRatifier` is deployed at the same address.

**`hashOffer` adds no chain binding.** `HashLib.hashOffer` encodes the full `Offer` struct (market, maker, tick, group, ratifier, etc.) but includes no chain ID and no Midnight contract address: [2](#0-1) 

**`Midnight.toId` uses `INITIAL_CHAIN_ID`, not `block.chainid`.** Market IDs are computed with the chain ID captured at construction time: [3](#0-2) 

So if Midnight is also deployed at the same address on both chains, the market IDs are identical, making the replayed offer structurally valid on chain B.

**Exploit flow:**

1. Maker signs an offer tree root on chain A. The signature covers `keccak256(EIP712_DOMAIN_TYPEHASH || chainId_A || ratifier_addr)`.
2. Attacker observes the signature off-chain.
3. On chain B (same chain ID, same ratifier address), attacker calls `Midnight.take(offer, ratifierData, ...)` with the chain-A signature.
4. `isRatified` recomputes `domainSeparator = keccak256(EIP712_DOMAIN_TYPEHASH || chainId_B || ratifier_addr)`. Since `chainId_A == chainId_B` and `ratifier_addr` is the same, the separator is identical.
5. `ecrecover` returns the maker's address; all checks pass.
6. `consumed[offer.maker][offer.group]` on chain B is incremented, consuming the maker's budget.

**Existing checks that do NOT stop this:**

- `require(msg.sender == MIDNIGHT, NotMidnight())` — attacker calls through the legitimate Midnight on chain B. [4](#0-3) 
- `require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized())` — if the maker has authorized the ratifier on chain B (same setup), this passes. [5](#0-4) 
- `require(!isRootCanceled[offer.maker][root], RootCanceled())` — `isRootCanceled` is chain-B-local storage; the maker has not canceled on chain B. [6](#0-5) 
- The code comment only acknowledges the hard-fork case (chain ID *changes*), not the same-chain-ID case. [7](#0-6) 

### Impact Explanation

On chain B, the attacker fills the maker's offer without the maker's consent. Each replay increments `consumed[maker][group]` on chain B. Once `maxUnits` or `maxAssets` is reached, the maker's group budget is exhausted and all legitimate takers on chain B are blocked with `ConsumedUnits` / `ConsumedAssets`. The maker's funds on chain B (credit or collateral) are transferred to the attacker's chosen receiver without authorization. [8](#0-7) 

### Likelihood Explanation

**Preconditions:**
1. Two chains share the same `block.chainid` — common for testnets, local devnets, and historical forks (e.g., ETH/ETC at the split).
2. `EcrecoverRatifier` deployed at the same address on both chains — trivially achieved with CREATE2 or the same deployer nonce.
3. Midnight deployed at the same address on both chains — same mechanism.
4. Maker has authorized the ratifier on chain B — likely if the maker uses the same wallet and setup on both chains.

All four preconditions are realistic in testnet/staging environments and in any multi-chain deployment using deterministic addresses. The attack is repeatable until the maker cancels the root on chain B or the budget is exhausted.

### Recommendation

Bind the domain separator to the Midnight contract address in addition to the ratifier address, or include the Midnight address inside the signed offer struct. Concretely, replace the domain separator computation with:

```solidity
bytes32 domainSeparator = keccak256(
    abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this), MIDNIGHT)
);
```

This ensures that even if two chains share the same chain ID and ratifier address, a different Midnight deployment address (or a different `MIDNIGHT` immutable) produces a different domain separator, breaking cross-chain replay.

Alternatively, include `block.chainid` and the Midnight address inside `hashOffer` so the leaf hash itself is chain-specific.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {EcrecoverRatifier} from "src/ratifiers/EcrecoverRatifier.sol";
import {HashLib} from "src/ratifiers/libraries/HashLib.sol";
import {Offer} from "src/interfaces/IMidnight.sol";
import {Signature, EIP712_DOMAIN_TYPEHASH} from "src/ratifiers/interfaces/IEcrecoverRatifier.sol";

contract CrossChainReplayTest is Test {
    // Both chains share chain ID 1337
    uint256 constant SHARED_CHAIN_ID = 1337;

    Midnight midnightA;
    Midnight midnightB;
    EcrecoverRatifier ratifierA;
    EcrecoverRatifier ratifierB; // deployed at same address via CREATE2

    uint256 makerKey = 0xA11CE;
    address maker;

    function setUp() public {
        vm.chainId(SHARED_CHAIN_ID);
        maker = vm.addr(makerKey);

        // Deploy both Midnights and ratifiers at deterministic addresses
        // (simulate same address via etch or CREATE2)
        midnightA = new Midnight(...);
        ratifierA = new EcrecoverRatifier(address(midnightA));

        midnightB = new Midnight(...);
        // Etch ratifierB at the same address as ratifierA
        vm.etch(address(ratifierA), address(new EcrecoverRatifier(address(midnightB))).code);
        ratifierB = EcrecoverRatifier(address(ratifierA));

        // Maker authorizes ratifier on BOTH chains
        vm.prank(maker);
        midnightA.setIsAuthorized(address(ratifierA), true, maker);
        vm.prank(maker);
        midnightB.setIsAuthorized(address(ratifierB), true, maker);
    }

    function testReplay() public {
        // Maker signs offer on chain A
        Offer memory offer;
        offer.maker = maker;
        offer.ratifier = address(ratifierA);
        offer.maxUnits = 100;
        // ... fill other fields ...

        bytes32 root = HashLib.hashOffer(offer);
        bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(0), root));
        bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, SHARED_CHAIN_ID, address(ratifierA)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(makerKey, digest);
        bytes memory ratifierData = abi.encode(Signature(v, r, s), root, 0, new bytes32[](0));

        // Attacker replays on chain B — same chain ID, same ratifier address
        uint256 consumedBefore = midnightB.consumed(maker, offer.group);
        vm.prank(address(this)); // attacker
        midnightB.take(offer, ratifierData, 1, address(this), address(this), address(0), "");
        uint256 consumedAfter = midnightB.consumed(maker, offer.group);

        // Assert: consumed increased on chain B without maker's intent
        assertGt(consumedAfter, consumedBefore, "replay succeeded: budget consumed on chain B");
    }
}
```

**Expected assertion:** `consumedAfter > consumedBefore` — the replay succeeds, consuming the maker's group budget on chain B without the maker signing anything on chain B.

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L10-11)
```text
/// @dev If block.chainid changes (hard fork), the EIP-712 domain separator changes and previously signed offers are
/// no longer valid.
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L34-34)
```text
        require(msg.sender == MIDNIGHT, NotMidnight());
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L38-38)
```text
        require(!isRootCanceled[offer.maker][root], RootCanceled());
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L39-41)
```text
        bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
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

**File:** src/Midnight.sol (L871-873)
```text
    function toId(Market memory market) public view returns (bytes32) {
        return IdLib.toId(market, INITIAL_CHAIN_ID, address(this));
    }
```
