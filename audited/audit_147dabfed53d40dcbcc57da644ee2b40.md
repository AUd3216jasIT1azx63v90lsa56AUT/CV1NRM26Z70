The code confirms the claim. Here is the exact trace:

- `EcrecoverAuthorizer.setIsAuthorized` (line 38–44): emits `SetIsAuthorized(msg.sender, ...)` where `msg.sender` is the relayer.
- It then calls `IMidnight(MIDNIGHT).setIsAuthorized(...)` (line 46–47).
- `Midnight.setIsAuthorized` (line 734): emits `EventsLib.SetIsAuthorized(msg.sender, ...)` where `msg.sender` is now `address(ecrecoverAuthorizer)`, not the original relayer.

This is a real, reproducible, observable divergence.

---

### Title
Midnight.SetIsAuthorized event emits address(ecrecoverAuthorizer) as caller instead of the original relayer - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
When a relayer submits a valid signature through `EcrecoverAuthorizer.setIsAuthorized`, the `EcrecoverAuthorizer.SetIsAuthorized` event correctly records the relayer as `caller` (via `msg.sender`). However, because `EcrecoverAuthorizer` then calls `Midnight.setIsAuthorized` as a contract-to-contract call, the `Midnight.SetIsAuthorized` event records `address(ecrecoverAuthorizer)` as `caller`, permanently losing the original relayer's identity in the core protocol event log. This creates a non-critical but concrete behavior divergence between the two event streams.

### Finding Description
**Code path:**

1. Any address (relayer) calls `EcrecoverAuthorizer.setIsAuthorized(authorization, signature)`.
2. At line 38–44 of `src/periphery/EcrecoverAuthorizer.sol`, the contract emits:
   ```
   emit SetIsAuthorized(msg.sender, authorizer, authorized, isAuthorized, nonce)
   ```
   Here `msg.sender` = relayer address. [1](#0-0) 
3. At line 46–47, it calls:
   ```
   IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer)
   ``` [2](#0-1) 
4. Inside `Midnight.setIsAuthorized` (line 734), `msg.sender` is now `address(ecrecoverAuthorizer)`:
   ```
   emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
   ``` [3](#0-2) 

**Root cause:** Solidity's `msg.sender` is always the immediate caller. When `EcrecoverAuthorizer` calls `Midnight`, `msg.sender` inside `Midnight` is `address(ecrecoverAuthorizer)`. The `EventsLib.SetIsAuthorized` event's `caller` field is defined as `address indexed caller` and is populated with this value. [4](#0-3) 

**Attacker-controlled inputs:** None required. Any unprivileged relayer submitting a valid signature triggers this divergence deterministically.

**Existing checks:** None prevent or mitigate this. The nonce/deadline/signature checks only gate whether the call succeeds; they do not affect which address is emitted as `caller` in the Midnight event.

### Impact Explanation
Off-chain indexers, analytics tools, or monitoring systems that rely solely on the `Midnight.SetIsAuthorized` event to attribute who submitted a gasless authorization will always see `address(ecrecoverAuthorizer)` as the caller, making it impossible to distinguish individual relayers from the Midnight event alone. The original relayer's identity is only recoverable by cross-referencing the `EcrecoverAuthorizer.SetIsAuthorized` event. No funds are at risk and no invariant is broken; this is a non-critical behavior divergence in the event data.

### Likelihood Explanation
This occurs on every single invocation of `EcrecoverAuthorizer.setIsAuthorized` with a valid signature. It is 100% repeatable, requires no special preconditions beyond a valid signature, and is reachable by any unprivileged user acting as a relayer. The existing test `testEcrecoverAuthorizerPermissionless` already demonstrates that any address can be the relayer. [5](#0-4) 

### Recommendation
Pass the original `msg.sender` (relayer) through to `Midnight` so it can be recorded in the core event. One approach: add an overloaded or extended `setIsAuthorized` variant in `Midnight` that accepts an explicit `caller` address, callable only by whitelisted periphery contracts. Alternatively, emit an additional field in the `Midnight` event (e.g., `relayer`) populated by the periphery contract passing `msg.sender` as a parameter. The simplest fix without changing `Midnight`'s interface is to accept that the Midnight event will always show the periphery contract and document this explicitly, but the cleaner fix is to thread the original caller through.

### Proof of Concept
```solidity
// Foundry unit test
function testCallerDivergenceInEvents() public {
    address relayer = makeAddr("relayer");

    vm.prank(borrower);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, borrower);

    Authorization memory auth = makeAuthorization(borrower, lender, true);
    Signature memory sig = signAuthorization(auth, borrower);

    // Expect EcrecoverAuthorizer event: caller == relayer
    vm.expectEmit(true, true, true, true, address(ecrecoverAuthorizer));
    emit IEcrecoverAuthorizer.SetIsAuthorized(relayer, borrower, lender, true, 0);

    // Expect Midnight event: caller == address(ecrecoverAuthorizer), NOT relayer
    vm.expectEmit(true, true, true, true, address(midnight));
    emit EventsLib.SetIsAuthorized(address(ecrecoverAuthorizer), lender, true, borrower);

    vm.prank(relayer);
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);

    // Assert: Midnight event caller is ecrecoverAuthorizer, not relayer
    // (verified by the expectEmit assertions above passing)
    assertTrue(relayer != address(ecrecoverAuthorizer));
}
```
**Expected assertions:** Both `vm.expectEmit` calls pass, confirming that `Midnight.SetIsAuthorized` has `caller == address(ecrecoverAuthorizer)` while `EcrecoverAuthorizer.SetIsAuthorized` has `caller == relayer`. The divergence is proven.

### Citations

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

**File:** src/periphery/EcrecoverAuthorizer.sol (L46-47)
```text
        IMidnight(MIDNIGHT)
            .setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
```

**File:** src/Midnight.sol (L734-734)
```text
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
```

**File:** src/libraries/EventsLib.sol (L30-30)
```text
    event SetIsAuthorized(address indexed caller, address indexed authorized, bool newIsAuthorized, address indexed onBehalf);
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
