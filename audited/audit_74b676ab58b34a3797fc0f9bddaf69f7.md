Audit Report

## Title
Signature Malleability in `setIsAuthorized` Enables Nonce-Griefing Front-Run - (File: `src/periphery/EcrecoverAuthorizer.sol`)

## Summary
`EcrecoverAuthorizer.setIsAuthorized` passes `(v, r, s)` directly to `ecrecover` at line 31 with no check that `s` is in the lower half of the secp256k1 curve order. Because secp256k1 is symmetric, a second valid signature `(v', r, n−s)` exists for every digest and recovers to the same address. Since the function is permissionless and the nonce is consumed atomically on every successful call, an attacker who observes a signed authorization in the mempool can front-run the maker with the malleable form, consuming the nonce and causing the maker's original transaction to revert with `InvalidNonce`.

## Finding Description

**Root cause — missing s-value bound check:**

`EcrecoverAuthorizer.setIsAuthorized` at line 31 calls `ecrecover` with no guard on `s`: [1](#0-0) 

There is no `require(uint256(signature.s) <= SECP256K1_N_DIV_2)`. The EVM `ecrecover` precompile accepts both `(v, r, s)` and `(v', r, n−s)` (where `v'` flips the recovery bit between 27 and 28) as valid signatures for the same digest, both recovering to the same signer address.

**Nonce consumed atomically:** [2](#0-1) 

Every successful call increments the nonce. Once the malleable submission lands, the original `(v, r, s)` over the same `authorization` struct (encoding `nonce = 0`) fails the nonce check permanently.

**Permissionless submission confirmed:**

The function has no `msg.sender` restriction, and `testEcrecoverAuthorizerPermissionless` explicitly confirms any caller may submit any `(authorization, signature)` pair: [3](#0-2) 

**Exploit flow:**

1. Maker signs `Authorization{authorizer, authorized, isAuthorized, nonce=0, deadline}` producing `(v, r, s)` and broadcasts it (mempool, relayer, API).
2. Attacker computes malleable counterpart: `v' = (v == 27 ? 28 : 27)`, `s' = n − s` where `n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141`.
3. Attacker calls `setIsAuthorized(auth, Signature(v', r, s'))` with the identical `auth` struct.
4. `ecrecover(digest, v', r, s')` returns the same `authorizer` address → all checks pass → nonce increments to 1 → authorization is applied.
5. Maker's original transaction arrives: `authorization.nonce == 0` but `nonce[authorizer] == 1` → reverts `InvalidNonce`.

**Existing checks and why they fail:**

| Check | Stops attack? |
|---|---|
| `Expired()` deadline check | No — attacker uses same `auth` struct |
| `InvalidNonce()` nonce check | No — attacker submits first with nonce=0 |
| `InvalidSignature()` zero-address check | No — malleable sig recovers to same non-zero address |
| `Unauthorized()` signer check | No — same address recovered |
| s-value bound check | **Missing** — root cause | [4](#0-3) 

## Impact Explanation

The attacker cannot alter the authorization parameters (they must use the identical `auth` struct), so the authorization state change itself is applied correctly. The concrete impact is griefing/DoS: the maker's original transaction is permanently invalidated for that nonce. If the maker's authorization is embedded in an atomic multicall or meta-transaction (e.g., authorize-then-supply in one call), the entire operation reverts. The attacker can repeat this for every new nonce the maker attempts to use, as long as they observe the signature before it is mined, creating sustained service degradation for any maker relying on off-chain signature relay.

## Likelihood Explanation

Preconditions are minimal: the attacker only needs to observe the signed authorization before it is mined, which is trivially satisfied via the public mempool or any off-chain relay/API. The malleable signature computation is pure arithmetic requiring no special resources. The attack is repeatable for every nonce the maker generates.

## Recommendation

Add a low-s check before the `ecrecover` call:

```solidity
uint256 constant SECP256K1_N_DIV_2 =
    0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

require(uint256(signature.s) <= SECP256K1_N_DIV_2, InvalidSignature());
address signer = ecrecover(digest, signature.v, signature.r, signature.s);
```

Alternatively, use OpenZeppelin's `ECDSA.recover`, which enforces this bound internally. This is the same fix applied in EIP-2 and enforced by OpenZeppelin since v4. [5](#0-4) 

## Proof of Concept

Minimal Foundry test demonstrating the griefing:

```solidity
function testMalleableSigGriefing() public {
    vm.prank(borrower);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);

    Authorization memory auth = makeAuthorization(borrower, lender, true);
    Signature memory sig = signAuthorization(auth, borrower);

    // Compute malleable counterpart
    uint256 n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
    Signature memory malSig = Signature({
        v: sig.v == 27 ? 28 : 27,
        r: sig.r,
        s: bytes32(n - uint256(sig.s))
    });

    // Attacker front-runs with malleable sig — succeeds
    vm.prank(address(0xdead));
    ecrecoverAuthorizer.setIsAuthorized(auth, malSig);
    assertEq(ecrecoverAuthorizer.nonce(borrower), 1);

    // Maker's original tx now reverts
    vm.expectRevert(IEcrecoverAuthorizer.InvalidNonce.selector);
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);
}
``` [6](#0-5)

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L25-36)
```text
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

**File:** test/SetIsAuthorizedWithSigTest.sol (L41-52)
```text
    function signAuthorization(Authorization memory authorization, address _signer)
        internal
        view
        returns (Signature memory)
    {
        bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, authorization));
        bytes32 domainSeparator =
            keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey[_signer], digest);
        return Signature({v: v, r: r, s: s});
    }
```

**File:** test/SetIsAuthorizedWithSigTest.sol (L74-86)
```text
    function testEcrecoverAuthorizerPermissionless() public {
        vm.prank(borrower);
        midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);
        Authorization memory auth = makeAuthorization(borrower, lender, true);
        Signature memory sig = signAuthorization(auth, borrower);

        // Anyone can submit — no caller auth needed
        vm.prank(otherLender);
        ecrecoverAuthorizer.setIsAuthorized(auth, sig);

        assertEq(midnight.isAuthorized(borrower, lender), true);
        assertEq(ecrecoverAuthorizer.nonce(borrower), 1);
    }
```
