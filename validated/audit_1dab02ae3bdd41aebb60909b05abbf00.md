### Title
Missing Nonce Invalidation in `EcrecoverAuthorizer` Allows Forced Execution of Pending Signed Authorizations — (`src/periphery/EcrecoverAuthorizer.sol`)

---

### Summary

`EcrecoverAuthorizer` uses a sequential nonce to prevent replay of signed `Authorization` structs, but provides no function to invalidate or skip a nonce. Because `setIsAuthorized` is fully permissionless (any caller can submit a valid signed authorization), a signer who wants to cancel a pending authorization before its deadline has no on-chain mechanism to do so. This is the direct analog of the PayrollManager nonce-invalidation bug.

---

### Finding Description

`EcrecoverAuthorizer` tracks a per-authorizer sequential nonce:

```solidity
mapping(address => uint256) public nonce;
``` [1](#0-0) 

The only function that increments this nonce is `setIsAuthorized`, which is **permissionless** — any `msg.sender` can call it with a valid signed `Authorization`:

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
    require(authorization.nonce == nonce[authorization.authorizer]++, InvalidNonce());
    ...
``` [2](#0-1) 

This permissionless behavior is explicitly confirmed by the test `testEcrecoverAuthorizerPermissionless`, which shows a third party (`otherLender`) successfully submitting another user's signed authorization: [3](#0-2) 

There is **no** `invalidateNonce()`, `cancelAuthorization()`, or equivalent function in either the implementation or the interface: [4](#0-3) 

This is in direct contrast to `EcrecoverRatifier`, which provides `cancelRoot()` so a maker can revoke a Merkle root before it is used: [5](#0-4) 

`EcrecoverAuthorizer` has no equivalent escape hatch.

---

### Impact Explanation

A signed `Authorization` grants the `authorized` address the ability to act on behalf of the `authorizer` across all of Midnight's state-mutating functions: `take`, `repay`, `supplyCollateral`, `withdrawCollateral`, `setIsAuthorized` (for further delegation), and `setConsumed`. If an authorizer signs and broadcasts (or shares off-chain) an authorization they later want to cancel, they cannot prevent its execution before the deadline. An adversary (or the authorized address itself) can execute the authorization at any time within the deadline window, gaining full delegated control over the authorizer's Midnight positions — enabling theft of collateral, forced debt creation, or further privilege escalation.

---

### Likelihood Explanation

The scenario is realistic: a user signs an authorization for a smart contract integration or a counterparty, then discovers the target address is wrong or malicious, or the integration is compromised. Since `setIsAuthorized` requires no caller authentication and the signed payload is observable in the mempool or shared off-chain, any party holding the signature can execute it. The only partial mitigation is a short `deadline`, but the protocol does not enforce any maximum deadline, and users routinely sign with generous deadlines for UX reasons.

---

### Recommendation

Add a nonce-invalidation function analogous to `EcrecoverRatifier.cancelRoot()`:

```solidity
function invalidateNonce() external {
    nonce[msg.sender]++;
    emit NonceInvalidated(msg.sender, nonce[msg.sender]);
}
```

This allows an authorizer to atomically skip their current nonce, rendering any outstanding signed authorization for that nonce permanently invalid, without requiring execution of the authorization itself. The interface `IEcrecoverAuthorizer` should be updated accordingly. [6](#0-5) 

---

### Proof of Concept

1. Alice calls `midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, alice)` — authorizing the `EcrecoverAuthorizer` to act on her behalf.
2. Alice signs an `Authorization{authorizer: alice, authorized: bob, isAuthorized: true, nonce: 0, deadline: block.timestamp + 7 days}`.
3. Alice broadcasts the signed authorization to the mempool (or shares it with Bob directly).
4. Alice realizes Bob is malicious and wants to cancel. She has **no function to call** to invalidate nonce `0`.
5. Bob (or any third party) calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)` — the call succeeds, `midnight.isAuthorized(alice, bob)` becomes `true`.
6. Bob now has full delegated access to Alice's Midnight positions and can drain her collateral via `withdrawCollateral` or create debt via `take`.

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

**File:** src/periphery/interfaces/IEcrecoverAuthorizer.sol (L26-44)
```text
interface IEcrecoverAuthorizer {
    /// ERRORS ///
    error Expired();
    error InvalidNonce();
    error InvalidSignature();
    error Unauthorized();

    /// EVENTS ///
    event SetIsAuthorized(
        address indexed caller, address indexed authorizer, address indexed authorized, bool isAuthorized, uint256 nonce
    );

    /// STORAGE GETTERS ///
    function MIDNIGHT() external view returns (address);
    function nonce(address authorizer) external view returns (uint256);

    /// FUNCTIONS ///
    function setIsAuthorized(Authorization memory authorization, Signature memory signature) external;
}
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```
