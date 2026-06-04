### Title
Authorized Account Can Front-Run Authorization Revocation to Steal Collateral and Establish a Persistent Backdoor — (File: src/Midnight.sol)

---

### Summary

The `setIsAuthorized` function in `Midnight.sol` is a single-step, immediately-effective operation with no time-lock or two-step confirmation. A malicious authorized account (Bob) can monitor the mempool for the authorizer's (Alice's) revocation transaction and front-run it: draining Alice's collateral and re-authorizing a new malicious account on Alice's behalf before the revocation lands. This is the direct analog of the Convexity `transferRepoOwnership` race condition — the party losing control can worsen the state before the handover completes.

---

### Finding Description

**Root cause:** `setIsAuthorized` is a single atomic write with no pending/acceptance phase. The protocol's own inline documentation explicitly states:

> "authorized accounts can authorize other accounts on behalf of the user."

This means an authorized account holds two dangerous capabilities simultaneously:
1. It can drain the authorizer's collateral via `withdrawCollateral`.
2. It can grant a new authorization on the authorizer's behalf via `setIsAuthorized`.

**Code path:**

`setIsAuthorized` at `src/Midnight.sol` line 731: [1](#0-0) 

`withdrawCollateral` authorization check at `src/Midnight.sol` line 556: [2](#0-1) 

The protocol comment confirming authorized accounts can re-authorize others at `src/Midnight.sol` lines 101–110: [3](#0-2) 

**Attack flow:**

1. Alice calls `setIsAuthorized(Bob, true, Alice)` — Bob is now authorized.
2. Alice later submits `setIsAuthorized(Bob, false, Alice)` to revoke Bob.
3. Bob (or a MEV bot acting for Bob) sees Alice's revocation in the mempool.
4. Bob front-runs using `multicall` (atomically):
   - `withdrawCollateral(market, i, fullCollateralAmount, Alice, Bob)` — transfers Alice's collateral to Bob. This passes because `isAuthorized[Alice][Bob] == true` at the time of execution, and the health check passes if Alice has no debt (or only up to the healthy threshold if she does).
   - `setIsAuthorized(Mallory, true, Alice)` — authorizes a new malicious account on Alice's behalf. This passes because `isAuthorized[Alice][Bob] == true` at the time of execution.
5. Alice's revocation mines, revoking Bob.
6. **Result:** Bob has stolen Alice's collateral, and Mallory — unknown to Alice — now has full authorization over Alice's position.

The `multicall` function at `src/Midnight.sol` lines 211–220 makes this atomic and reliable: [4](#0-3) 

---

### Impact Explanation

- **Direct asset theft:** Alice's collateral (any amount not required to maintain health) is transferred to Bob's address with no recourse.
- **Persistent backdoor:** Even after Alice successfully revokes Bob, Mallory retains full authorization. Mallory can continue to withdraw remaining assets, take bad trades on Alice's behalf, or authorize further accounts. Alice may not discover Mallory's authorization without actively auditing the `isAuthorized` mapping.
- **Severity:** High — direct theft of user funds and durable unauthorized access that survives the intended revocation.

---

### Likelihood Explanation

- **No privileged access required.** Bob is a normal protocol user who was legitimately authorized by Alice. No admin keys, governance access, or leaked credentials are needed.
- **Mempool visibility is standard.** Any Ethereum full node, MEV searcher, or public mempool service can observe Alice's pending revocation transaction.
- **Strong financial incentive.** Bob profits directly from the stolen collateral.
- **Atomic execution available.** `multicall` allows Bob to bundle the theft and backdoor creation into a single transaction, eliminating partial-execution risk.
- **Realistic trigger.** Users routinely revoke authorizations when they stop using a third-party manager, vault, or integration — exactly the moment this attack fires.

---

### Recommendation

1. **Two-step revocation with time-lock:** Introduce a `pendingRevocation` mapping. A revocation request starts a delay (e.g., 1 block or a configurable period) during which the authorized account cannot perform sensitive actions (`withdrawCollateral`, `setIsAuthorized`) on the authorizer's behalf.
2. **Restrict re-authorization by delegates:** Remove or gate the ability for authorized accounts to call `setIsAuthorized` on behalf of the authorizer. This eliminates the persistent-backdoor vector while preserving the rest of the authorization model.
3. **Documentation warning (minimum):** If neither fix is implemented, prominently document that revoking an authorization is subject to front-running and that users should assume the authorized account may act up to the moment of revocation.

---

### Proof of Concept

```
Preconditions:
  - Alice has 1000 USDC of collateral in market M, no debt.
  - Alice previously called setIsAuthorized(Bob, true, Alice).

Step 1: Alice submits tx: setIsAuthorized(Bob, false, Alice)  [pending in mempool]

Step 2: Bob (or MEV bot) detects Alice's pending tx and submits a higher-gas tx:
  midnight.multicall([
    abi.encodeCall(midnight.withdrawCollateral, (M, 0, 1000e6, Alice, Bob)),
    abi.encodeCall(midnight.setIsAuthorized,    (Mallory, true, Alice))
  ])

Step 3: Bob's multicall mines first (higher gas).
  - Alice's 1000 USDC collateral is now in Bob's wallet.
  - isAuthorized[Alice][Mallory] == true.

Step 4: Alice's revocation mines.
  - isAuthorized[Alice][Bob] == false.  ✓ (Bob revoked)
  - isAuthorized[Alice][Mallory] == true.  ✗ (backdoor persists)

Outcome:
  - Alice has lost her collateral.
  - Mallory retains full control of Alice's position indefinitely.
```

### Citations

**File:** src/Midnight.sol (L101-110)
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
```

**File:** src/Midnight.sol (L211-220)
```text
    function multicall(bytes[] calldata calls) external {
        for (uint256 i = 0; i < calls.length; i++) {
            (bool success, bytes memory returnData) = address(this).delegatecall(calls[i]);
            if (!success) {
                assembly ("memory-safe") {
                    revert(add(returnData, 0x20), mload(returnData))
                }
            }
        }
    }
```

**File:** src/Midnight.sol (L556-556)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```
