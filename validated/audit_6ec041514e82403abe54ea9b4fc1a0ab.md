### Title
No Mechanism to Cancel a Pending Signed Authorization — (`src/periphery/EcrecoverAuthorizer.sol`)

---

### Summary

`EcrecoverAuthorizer` uses a sequential nonce to prevent replay of signed `Authorization` messages. However, the nonce can **only** be incremented by successfully executing `setIsAuthorized`. There is no standalone cancel/increment function. A user who has signed and distributed an `Authorization` message cannot invalidate it before it is executed, even if they change their mind or the signature is leaked.

---

### Finding Description

The `EcrecoverAuthorizer` contract holds a per-user nonce: [1](#0-0) 

The nonce is consumed exclusively inside `setIsAuthorized`: [2](#0-1) 

There is no `cancelNonce`, `incrementNonce`, or equivalent function anywhere in the contract. The only path that advances `nonce[authorizer]` is a successful call to `setIsAuthorized` with a valid signature.

**Exploit flow:**

1. Alice signs `Authorization{authorizer: alice, authorized: attacker, isAuthorized: true, nonce: 0, deadline: T+30days}` and hands it to a third-party service.
2. Alice later revokes the service's on-chain authorization by calling `Midnight.setIsAuthorized(service, false, alice)` directly.
3. Alice also wants to cancel the signed message so it can never be replayed. She **cannot** — there is no function to advance her nonce in `EcrecoverAuthorizer`.
4. At any point before `T+30days`, anyone holding the signature calls `EcrecoverAuthorizer.setIsAuthorized(authorization, sig)`. The nonce check passes (`0 == nonce[alice]++`), the signature is valid, and `Midnight.setIsAuthorized(attacker, true, alice)` is executed, granting the attacker full account access.

Full account access means the attacker can call `take`, `withdraw`, `repay`, `supplyCollateral`, `withdrawCollateral`, `setConsumed`, and `setIsAuthorized` on behalf of Alice. [3](#0-2) 

Note the contrast: `EcrecoverRatifier` — the analogous contract for offer signing — **does** provide a `cancelRoot` function: [4](#0-3) 

`EcrecoverAuthorizer` has no equivalent. [5](#0-4) 

---

### Impact Explanation

An attacker who obtains a signed `Authorization` granting themselves (or a controlled address) access can execute it at any time before the deadline, even after the signer has attempted to revoke it on-chain. This results in **unauthorized full control** of the victim's Midnight account: draining credit via `withdraw`, seizing collateral via `withdrawCollateral`, or manipulating offers. Impact is **High**.

---

### Likelihood Explanation

The scenario is realistic: users routinely sign off-chain messages for third-party integrators, bots, or relayers. If the relationship sours, the user has no recourse to cancel the pending signed message. The deadline field provides a time-bound window, but long deadlines (common for UX convenience) leave a large attack surface. Likelihood is **Medium**.

---

### Recommendation

Add a `cancelNonce` (or `incrementNonce`) function to `EcrecoverAuthorizer` that allows the authorizer — or any address they have authorized on Midnight — to advance their own nonce, invalidating all outstanding signed `Authorization` messages:

```solidity
function cancelNonce(address authorizer) external {
    require(
        authorizer == msg.sender || IMidnight(MIDNIGHT).isAuthorized(authorizer, msg.sender),
        Unauthorized()
    );
    nonce[authorizer]++;
    emit CancelNonce(msg.sender, authorizer, nonce[authorizer]);
}
```

This mirrors the `cancelRoot` pattern already present in `EcrecoverRatifier`. [4](#0-3) 

---

### Proof of Concept

**Preconditions:** Alice has `nonce[alice] == 0` in `EcrecoverAuthorizer`.

1. Alice signs `Authorization{authorizer: alice, authorized: attacker, isAuthorized: true, nonce: 0, deadline: block.timestamp + 30 days}` and shares it with a service.
2. Alice calls `Midnight.setIsAuthorized(service, false, alice)` to revoke on-chain.
3. Alice wants to cancel the signed message — no function exists to do so.
4. Attacker calls `EcrecoverAuthorizer.setIsAuthorized(authorization, sig)`.
5. Check `authorization.nonce == nonce[alice]++` passes (`0 == 0`).
6. `Midnight.setIsAuthorized(attacker, true, alice)` executes.
7. Attacker now has full authorization over Alice's Midnight account and can drain all assets. [6](#0-5)

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L16-48)
```text
contract EcrecoverAuthorizer is IEcrecoverAuthorizer {
    address public immutable MIDNIGHT;
    mapping(address => uint256) public nonce;

    constructor(address _midnight) {
        MIDNIGHT = _midnight;
    }

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

**File:** src/Midnight.sol (L101-111)
```text
/// AUTHORIZATIONS
/// @dev All functions that change the position, consumed and authorization are accessible to the user and to
/// any account that has been authorized. Thus, to scope authorizations one should authorize a smart-contract with
/// scoped behavior.
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
/// - Under which conditions the account can return CALLBACK_SUCCESS when its isRatified function is called.
/// @dev updatePosition and liquidate (for liquidatable users) also impact the position and are permissionless.
///
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```
