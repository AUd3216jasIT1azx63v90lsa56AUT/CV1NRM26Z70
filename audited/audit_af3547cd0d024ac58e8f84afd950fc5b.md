### Title
Midnight-authorized operator can permanently cancel a maker's offer roots via EcrecoverRatifier/SetterRatifier - (File: src/ratifiers/EcrecoverRatifier.sol)

### Summary
`EcrecoverRatifier.cancelRoot` and `SetterRatifier.setIsRootRatified` use Midnight's global `isAuthorized` mapping as their sole authorization check, with no scope restriction. Any operator authorized by a maker in Midnight for any purpose — including an unrelated one such as acting as a taker — can call `cancelRoot(maker, root)` to permanently block all of the maker's live offers in that root, or `setIsRootRatified(maker, root, false)` to deactivate them, without the maker's knowledge or consent.

### Finding Description

**Exact code path:**

`EcrecoverRatifier.cancelRoot` (line 28) performs: [1](#0-0) 

`SetterRatifier.setIsRootRatified` (line 25) performs: [2](#0-1) 

Both checks delegate entirely to `IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender)`, which reads from Midnight's flat, unscoped mapping: [3](#0-2) 

`Midnight.setIsAuthorized` sets this flag with no scope, purpose, or target restriction: [4](#0-3) 

**Exploit flow:**

1. Maker calls `Midnight.setIsAuthorized(operator, true, maker)` to authorize `operator` for any legitimate purpose (e.g., taking on the maker's behalf).
2. `isAuthorized[maker][operator]` is now `true`.
3. Operator calls `EcrecoverRatifier.cancelRoot(maker, root)` — the check `IMidnight(MIDNIGHT).isAuthorized(maker, operator)` passes unconditionally.
4. `isRootCanceled[maker][root] = true` is written.
5. Any subsequent `take` call for an offer in that root hits: [5](#0-4) 
and reverts with `RootCanceled`.

For `SetterRatifier`, the same operator calls `setIsRootRatified(maker, root, false)`, setting `isRootRatified[maker][root] = false`, causing `isRatified` to revert at: [6](#0-5) 

**Why existing checks fail:**

The only guard in both functions is the `isAuthorized` check. There is no secondary check restricting the action to root-management purposes, no per-function authorization scope, and no way for the maker to distinguish "authorized for taking" from "authorized for root cancellation." The check passes for any operator the maker has ever authorized for any reason.

The `Midnight.sol` NatSpec (lines 105–108) does note that "other contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers)," but this is a general advisory comment about smart-contract authorization design — it does not constitute a code-level guard, does not appear in the ratifier contracts themselves, and does not prevent the exploit. [7](#0-6) 

### Impact Explanation

For `EcrecoverRatifier`: cancellation is **permanent** — `isRootCanceled[maker][root]` can only be set to `true`; there is no reset function. All offers signed under that root become permanently unfillable. The maker loses all expected lending/borrowing income from those offers with no recourse.

For `SetterRatifier`: the maker can call `setIsRootRatified(maker, root, true)` to re-enable, but the authorized operator can immediately call `setIsRootRatified(maker, root, false)` again, creating a griefing loop as long as the authorization persists.

### Likelihood Explanation

**Preconditions:** `isAuthorized[maker][operator] == true` — a single prior call to `Midnight.setIsAuthorized`. This is a standard, expected protocol interaction (e.g., a lender authorizing a borrower to take on their behalf, or a maker authorizing a keeper).

**Feasibility:** Trivially reachable in one transaction by any authorized operator. No special privileges, no oracle manipulation, no flash loans required.

**Repeatability:** For `EcrecoverRatifier`, one call is sufficient for permanent damage. For `SetterRatifier`, repeatable indefinitely.

### Recommendation

Restrict root cancellation to the maker only — remove the `isAuthorized` delegation from both functions:

```solidity
// EcrecoverRatifier.cancelRoot
require(maker == msg.sender, Unauthorized());

// SetterRatifier.setIsRootRatified
require(maker == msg.sender, Unauthorized());
```

If delegation is desired, introduce a separate, scoped authorization mapping in each ratifier (e.g., `isRootManager[maker][operator]`) that the maker must explicitly set, independent of Midnight's general `isAuthorized` flag.

### Proof of Concept

```solidity
// Foundry unit test
function test_authorizedOperatorCanCancelMakerRoot() public {
    // Setup: lender creates and signs an offer tree rooted at `root`
    bytes32 root = ...; // merkle root of lender's offers
    
    // Lender authorizes borrower for taking (unrelated purpose)
    vm.prank(lender);
    midnight.setIsAuthorized(borrower, true, lender);
    
    // Precondition: lender's root is not canceled
    assertFalse(ecrecoverRatifier.isRootCanceled(lender, root));
    
    // Borrower exploits: cancels lender's root
    vm.prank(borrower);
    ecrecoverRatifier.cancelRoot(lender, root);
    
    // Root is now permanently canceled
    assertTrue(ecrecoverRatifier.isRootCanceled(lender, root));
    
    // Subsequent take on lender's offer reverts with RootCanceled
    vm.expectRevert(IEcrecoverRatifier.RootCanceled.selector);
    midnight.take(lenderOffer, ratifierData, units, taker, receiver, address(0), "");
}
```

**Expected assertions:**
- `isRootCanceled(lender, root)` transitions from `false` to `true` after borrower's call.
- `midnight.take(...)` reverts with `RootCanceled` for any offer under that root.
- Lender has no on-chain mechanism to undo the cancellation in `EcrecoverRatifier`.

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L27-31)
```text
    function cancelRoot(address maker, bytes32 root) external {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootCanceled[maker][root] = true;
        emit CancelRoot(msg.sender, maker, root);
    }
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L38-38)
```text
        require(!isRootCanceled[offer.maker][root], RootCanceled());
```

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L35-35)
```text
        require(isRootRatified[offer.maker][root], NotRatified());
```

**File:** src/Midnight.sol (L105-108)
```text
/// @dev When authorizing a smart-contract, one should consider:
/// - The targets/functions that the account can call. At least Midnight's functions should be considered, but other
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
```

**File:** src/Midnight.sol (L192-192)
```text
    mapping(address authorizer => mapping(address authorized => bool)) public isAuthorized;
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
