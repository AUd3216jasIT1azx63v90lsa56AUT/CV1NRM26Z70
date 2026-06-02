Audit Report

## Title
`TreeTooHigh` Revert Escapes `RatifierFail` Wrapper When Taker Supplies `proof.length = 21` - (File: `src/ratifiers/EcrecoverRatifier.sol`)

## Summary
`EcrecoverRatifier.isRatified` calls `HashLib.isLeaf` before `HashLib.offerTreeTypeHash(proof.length)`. Because `isLeaf` imposes no upper bound on `proof.length`, a taker can craft `ratifierData` with a 21-element proof and a matching attacker-controlled root that satisfies the Merkle check, causing execution to reach `offerTreeTypeHash(21)`, which unconditionally reverts with `TreeTooHigh`. This revert propagates raw through `Midnight.take()`, bypassing the `RatifierFail` wrapper, breaking the protocol invariant that all ratifier failures surface as `RatifierFail`.

## Finding Description
In `EcrecoverRatifier.isRatified`, the execution order is:

```solidity
// line 37
require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
// line 38
require(!isRootCanceled[offer.maker][root], RootCanceled());
// line 39
bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
```

`HashLib.isLeaf` (line 58) only checks `leafIndex >> proof.length == 0`. With `leafIndex = 0`, this evaluates to `0 >> 21 = 0`, which passes for any `proof.length` — there is no `proof.length <= 20` guard. The loop at lines 60–62 runs 21 times over the attacker-supplied `proof` array, producing a deterministic `currentHash`. The attacker sets `root = currentHash` in `ratifierData`, making `isLeaf` return `true`.

Execution then reaches line 39. `HashLib.offerTreeTypeHash(21)` enters the `else` branch (line 35 of `HashLib.sol`), finds no matching `if` for height 21, and executes `revert TreeTooHigh()` at line 46.

The `isRootCanceled` check at line 38 passes because the attacker's crafted root has never been canceled. The signature check at lines 42–44 is never reached.

In `Midnight.take` at line 356:
```solidity
require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```
The `require` only emits `RatifierFail` when `isRatified` **returns** a wrong value. When `isRatified` **reverts**, the revert bubbles through `take()` unchanged as `TreeTooHigh`, not `RatifierFail`.

## Impact Explanation
The protocol invariant that all ratifier failures surface as `RatifierFail` is broken. Smart-contract integrators, aggregators, or multicall sequences that catch `RatifierFail` for graceful fallback will not catch `TreeTooHigh`, causing unexpected revert propagation and broken control flow. The internal `HashLib.TreeTooHigh` error selector is exposed to callers, leaking implementation details. No persistent state change occurs and no funds are at risk, making this a low-severity integrity/invariant violation.

## Likelihood Explanation
Preconditions: any valid offer using `EcrecoverRatifier` as its ratifier. The taker needs only to craft `ratifierData` with a 21-element proof and a matching root — no privileged access, no special state, no tokens required. The computation is pure off-chain arithmetic. Repeatable on every block against any offer using this ratifier.

## Recommendation
Add a `proof.length <= 20` guard in `HashLib.isLeaf` (or at the top of `EcrecoverRatifier.isRatified` before calling `isLeaf`) so that oversized proofs are rejected with `InvalidProof()` before reaching `offerTreeTypeHash`. For example, in `isLeaf`:
```solidity
require(proof.length <= 20 && leafIndex >> proof.length == 0, LeafIndexOutOfRange());
```
Alternatively, validate `proof.length` explicitly in `EcrecoverRatifier.isRatified` before the `isLeaf` call.

## Proof of Concept
1. Deploy `Midnight` and `EcrecoverRatifier`. Create a valid offer with `offer.ratifier = address(ecrecoverRatifier)`.
2. Off-chain: compute `leafHash = HashLib.hashOffer(offer)`. Choose `proof = new bytes32[](21)` (any 21 values, e.g., all zeros). Compute the Merkle root by iterating `hashNode` 21 times from `leafHash` with `leafIndex = 0`.
3. Encode `ratifierData = abi.encode(sig, root, 0, proof)` where `root` is the computed value and `sig` is any dummy signature.
4. Call `Midnight.take(offer, ratifierData, ...)`.
5. Observe the transaction reverts with selector `TreeTooHigh()` (from `HashLib`) rather than `RatifierFail()` (from `IMidnight`).
6. Confirm: a try/catch on `RatifierFail` does not catch the revert.