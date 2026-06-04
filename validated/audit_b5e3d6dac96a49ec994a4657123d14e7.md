### Title
Pre-Signed Authorization Bypass: `Midnight.setIsAuthorized` Revocation Does Not Invalidate Pending `EcrecoverAuthorizer` Signatures — (File: `src/periphery/EcrecoverAuthorizer.sol`)

---

### Summary

A user who revokes an authorized address's access by calling `Midnight.setIsAuthorized(authorized, false, onBehalf)` directly does not invalidate any pre-signed `Authorization` structs held in `EcrecoverAuthorizer`. Because the nonce in `EcrecoverAuthorizer` is entirely decoupled from Midnight's authorization state, a previously-signed authorization with a future deadline can be submitted by anyone after the revocation, re-granting the revoked address full access to the user's positions.

---

### Finding Description

**Vulnerability class:** Auth / State transition — incomplete revocation of delegated authority.

**Root cause:** `EcrecoverAuthorizer` maintains its own `nonce` mapping per authorizer. The only way to advance this nonce is to submit a valid authorization through `EcrecoverAuthorizer.setIsAuthorized`. Calling `Midnight.setIsAuthorized` directly has zero effect on `EcrecoverAuthorizer.nonce`.

**Code path:**

Step 1 — User authorizes `EcrecoverAuthorizer` in Midnight (required prerequisite): [1](#0-0) 

Step 2 — User signs an `Authorization` struct granting address B access, with a future deadline. The signed message is handed to B (or broadcast): [2](#0-1) 

Step 3 — User later decides to revoke B by calling `Midnight.setIsAuthorized(B, false, user)` directly. This sets `isAuthorized[user][B] = false` in Midnight but does **not** touch `EcrecoverAuthorizer.nonce[user]`, which remains at 0: [1](#0-0) 

Step 4 — B (or any third party holding the signed message) submits the pre-signed authorization to `EcrecoverAuthorizer.setIsAuthorized`. The checks all pass:
- `block.timestamp <= deadline` ✓ (deadline is in the future)
- `nonce[user] == 0` ✓ (never incremented by the direct Midnight call)
- Signature valid ✓
- `isAuthorized[user][EcrecoverAuthorizer]` is still `true` ✓ [3](#0-2) 

Step 5 — `EcrecoverAuthorizer` calls `Midnight.setIsAuthorized(B, true, user)`, re-granting B full authorization: [4](#0-3) 

The permissionless nature of `EcrecoverAuthorizer.setIsAuthorized` (confirmed by the test `testEcrecoverAuthorizerPermissionless`) means B does not even need to submit it themselves — any front-runner or MEV bot can do so: [5](#0-4) 

---

### Impact Explanation

Once re-authorized, B can invoke any Midnight function on behalf of the user, including:
- `withdraw` — drain all credit/loan-token positions
- `withdrawCollateral` — remove all collateral
- `setIsAuthorized` — grant further addresses access on the user's behalf
- `setConsumed` — cancel all of the user's open offers

The user believes the revocation is complete and may take no further protective action, leaving their positions fully exposed.

---

### Likelihood Explanation

The preconditions are realistic and common:

1. The user previously used `EcrecoverAuthorizer` to delegate to a smart contract or third party (a normal workflow).
2. The signed authorization has a long deadline (typical for UX reasons — the test itself uses `+1 days`, but production integrations often use weeks or months).
3. The user revokes via `Midnight.setIsAuthorized` directly (the most natural revocation path, and the one documented in the interface).
4. There is no `cancelAuthorization` or `incrementNonce` function in `EcrecoverAuthorizer` to invalidate the pre-signed message without submitting another authorization first. [6](#0-5) 

---

### Recommendation

Add a standalone nonce-increment (or explicit cancel) function to `EcrecoverAuthorizer` so users can invalidate all outstanding pre-signed authorizations without needing to submit a new one:

```solidity
function invalidateNonce() external {
    nonce[msg.sender]++;
    emit NonceInvalidated(msg.sender, nonce[msg.sender]);
}
```

Additionally, document clearly that revoking authorization via `Midnight.setIsAuthorized` directly does **not** invalidate pre-signed `EcrecoverAuthorizer` messages, and that users must also advance their nonce in `EcrecoverAuthorizer` to achieve a complete revocation.

---

### Proof of Concept

```
1. vm.prank(user);
   midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, user);
   // EcrecoverAuthorizer is now authorized to act for `user`

2. Authorization memory auth = Authorization({
       authorizer: user,
       authorized: attacker,
       isAuthorized: true,
       nonce: 0,                          // ecrecoverAuthorizer.nonce(user) == 0
       deadline: block.timestamp + 365 days
   });
   Signature memory sig = sign(auth, userPrivKey);
   // Signed message handed to `attacker`

3. vm.prank(user);
   midnight.setIsAuthorized(attacker, false, user);
   // User believes attacker is revoked
   // ecrecoverAuthorizer.nonce(user) is still 0

4. vm.prank(attacker);
   ecrecoverAuthorizer.setIsAuthorized(auth, sig);
   // Passes: nonce==0, deadline not expired, sig valid
   // Calls Midnight.setIsAuthorized(attacker, true, user)

5. assert(midnight.isAuthorized(user, attacker) == true);
   // Attacker is re-authorized — revocation bypassed
``` [3](#0-2) [1](#0-0)

### Citations

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** src/periphery/interfaces/IEcrecoverAuthorizer.sol (L11-17)
```text
struct Authorization {
    address authorizer;
    address authorized;
    bool isAuthorized;
    uint256 nonce;
    uint256 deadline;
}
```

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
