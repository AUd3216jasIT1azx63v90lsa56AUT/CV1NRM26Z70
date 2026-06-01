### Title
Transitive Authorization Allows Unprivileged Attacker to Ratify Arbitrary Roots on Behalf of Maker - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` guards access with a single-level `isAuthorized[maker][msg.sender]` check, but `Midnight.setIsAuthorized` permits any address already authorized by the maker to grant that same authorization to arbitrary third parties on the maker's behalf. An attacker who is authorized by any one of the maker's existing operators can therefore write `isRootRatified[maker][root] = true` for a root the maker never approved, enabling fills of offers the maker never created.

### Finding Description
**Root cause — `Midnight.setIsAuthorized` (line 731–733):**
```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
```
The guard only requires that `msg.sender` is *already* authorized by `onBehalf`; it does not restrict what the authorized address may do with that power. Crucially, it allows the authorized address to extend the same authorization to any new address, still writing into `isAuthorized[onBehalf][...]`.

**Guard in `SetterRatifier.setIsRootRatified` (line 25):**
```solidity
require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
```
This check is satisfied by *any* address that appears in `isAuthorized[maker]`, regardless of how it got there.

**Exploit path:**
1. `maker` calls `midnight.setIsAuthorized(A, true, maker)` for any legitimate purpose → `isAuthorized[maker][A] = true`.
2. `A` (or attacker who controls A) calls `midnight.setIsAuthorized(B, true, maker)`. The guard `isAuthorized[maker][A]` is true, so it passes → `isAuthorized[maker][B] = true`.
3. `B` (attacker) calls `setterRatifier.setIsRootRatified(maker, attacker_root, true)`. The guard `isAuthorized[maker][B]` is true, so it passes → `isRootRatified[maker][attacker_root] = true`.
4. Attacker constructs any `Offer` whose `HashLib.hashOffer` is a leaf under `attacker_root` and calls `midnight.take(...)` with the matching Merkle proof. `isRatified` returns `CALLBACK_SUCCESS` because `isRootRatified[offer.maker][root]` is true.

No existing check stops step 2: the Certora spec `onlyAuthorizedCanChangeIsAuthorized` only asserts that the *caller* is authorized by the authorizer, which A is — it does not prevent A from delegating further. The `SetterRatifier` has no additional guard beyond the single `isAuthorized` lookup.

### Impact Explanation
An attacker who can compromise or collude with any single address the maker has ever authorized can ratify an arbitrary Merkle root for the maker. This lets the attacker fill offers the maker never signed or approved, draining the maker's supplied liquidity or forcing the maker into positions (borrow/lend) they never intended.

### Likelihood Explanation
The precondition — maker has authorized at least one operator — is the normal operating state for any maker using `SetterRatifier` with delegated management. The attack requires only that the attacker controls or socially engineers one such operator. It is repeatable: the attacker can ratify multiple roots and is not limited to a single exploit. No special privileges, oracle manipulation, or admin access are needed.

### Recommendation
Restrict `setIsAuthorized` so that an authorized address cannot re-delegate on behalf of the original user — only the user themselves (`onBehalf == msg.sender`) should be permitted to add new authorized addresses:
```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender, Unauthorized()); // remove delegated re-authorization
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    ...
}
```
Alternatively, if delegated authorization is intentional, `SetterRatifier.setIsRootRatified` must enforce that `msg.sender` was authorized *directly by the maker* and not via a delegated chain — but this requires tracking authorization provenance, which the current flat `isAuthorized` mapping does not support.

### Proof of Concept
```solidity
// Foundry unit test
function testTransitiveAuthorizationRatifiesArbitraryRoot() public {
    address maker   = makeAddr("maker");
    address operatorA = makeAddr("operatorA");
    address attackerB = makeAddr("attackerB");
    bytes32 maliciousRoot = keccak256("attacker_root");

    // Step 1: maker legitimately authorizes operatorA
    vm.prank(maker);
    midnight.setIsAuthorized(operatorA, true, maker);

    // Step 2: operatorA re-delegates to attackerB on maker's behalf
    vm.prank(operatorA);
    midnight.setIsAuthorized(attackerB, true, maker);

    // Precondition: attackerB is now in isAuthorized[maker]
    assertTrue(midnight.isAuthorized(maker, attackerB));

    // Step 3: attackerB ratifies an arbitrary root for maker
    vm.prank(attackerB);
    setterRatifier.setIsRootRatified(maker, maliciousRoot, true);

    // Assertion: maker's ratification state is corrupted
    assertTrue(setterRatifier.isRootRatified(maker, maliciousRoot));
    // maker never called setIsRootRatified and never signed any offer under maliciousRoot
}
```
Expected: all three calls succeed and the final assertion holds, confirming the invariant violation.