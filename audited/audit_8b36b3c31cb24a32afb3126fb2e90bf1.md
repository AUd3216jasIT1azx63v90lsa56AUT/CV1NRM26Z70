### Title
`deadline == 0` Always Reverts With `Expired()` Instead of Acting as No-Expiry Sentinel - ([File: src/periphery/EcrecoverAuthorizer.sol])

### Summary
In `EcrecoverAuthorizer.setIsAuthorized`, the deadline check `require(block.timestamp <= authorization.deadline, Expired())` treats `deadline == 0` as immediately expired because `block.timestamp` is always greater than zero on any live chain. A signature user who signs an `Authorization` with `deadline == 0` — expecting it to mean "no expiry" — will always receive an `Expired()` revert. No funds are at risk, but the behavior diverges from the common sentinel convention.

### Finding Description
The exact reachable path is: [1](#0-0) 

```solidity
function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
    require(block.timestamp <= authorization.deadline, Expired());
```

The `Authorization` struct declares `deadline` as a plain `uint256` with no documented special value: [2](#0-1) 

There is no branch or guard that treats `deadline == 0` as a bypass. Because `block.timestamp >= 1` on every real EVM chain, the condition `block.timestamp <= 0` is always `false`, so any call carrying a signature with `deadline == 0` unconditionally reverts with `Expired()`.

Attacker-controlled input: the `authorization.deadline` field inside the signed `Authorization` struct. The signature user controls this value at signing time.

Exploit flow:
1. Signature user constructs `Authorization{..., deadline: 0}` intending "no expiry."
2. Signs it off-chain and submits (or hands to a relayer).
3. `setIsAuthorized` is called; line 25 evaluates `block.timestamp <= 0` → `false` → reverts `Expired()`.
4. The nonce is not consumed (the nonce increment on line 26 is never reached), so the signature is not burned, but the authorization is never applied.

No existing check handles `deadline == 0` as a sentinel; the only protection is the strict `<=` comparison.

### Impact Explanation
No funds are lost and no position is frozen. The concrete impact is a non-critical behavior divergence: a legitimately signed authorization with `deadline == 0` is permanently unusable, silently failing with `Expired()` rather than succeeding or giving a descriptive error. This matches the scoped impact of "non-critical behavior divergence."

### Likelihood Explanation
Any signature user who sets `deadline == 0` — a natural choice for "no expiry" given the field is `uint256` — will trigger this. It is fully repeatable and requires no special chain state. Off-chain tooling or integrations that default unset deadlines to `0` would systematically produce unusable signatures.

### Recommendation
Add an explicit sentinel check before the deadline comparison:

```solidity
if (authorization.deadline != 0) {
    require(block.timestamp <= authorization.deadline, Expired());
}
```

Alternatively, document clearly that `deadline == 0` means "already expired" and that callers must use `type(uint256).max` for no-expiry, and enforce this in any SDK or off-chain tooling.

### Proof of Concept
```solidity
// Foundry unit test
function test_deadline_zero_reverts_expired() public {
    Authorization memory auth = Authorization({
        authorizer: alice,
        authorized: bob,
        isAuthorized: true,
        nonce: 0,
        deadline: 0          // sentinel attempt: "no expiry"
    });
    Signature memory sig = _sign(aliceKey, auth);

    vm.expectRevert(IEcrecoverAuthorizer.Expired.selector);
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);
    // Assert: nonce unchanged (authorization never applied)
    assertEq(ecrecoverAuthorizer.nonce(alice), 0);
}
```

Expected assertion: call reverts with `Expired()` and `nonce[alice]` remains `0`, confirming the authorization is permanently blocked despite a valid signature.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L24-25)
```text
    function setIsAuthorized(Authorization memory authorization, Signature calldata signature) external {
        require(block.timestamp <= authorization.deadline, Expired());
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
