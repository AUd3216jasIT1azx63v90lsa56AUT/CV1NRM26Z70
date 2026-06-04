### Title
Authorization Revocation Front-Running Allows Persistent Unauthorized Access — (File: src/Midnight.sol)

---

### Summary

The `setIsAuthorized` function in `Midnight.sol` allows any currently-authorized account to grant authorization to additional accounts on behalf of the user. This creates a direct analog to the ERC20 approve/double-spend front-running attack: when a user submits a transaction to revoke an operator's authorization, the operator can front-run that transaction by delegating their access to a new accomplice account, preserving full control over the user's positions even after the revocation confirms.

---

### Finding Description

**Root Cause**

`setIsAuthorized` checks only that `msg.sender == onBehalf || isAuthorized[onBehalf][msg.sender]`. Because the authorization check is satisfied for any currently-authorized account, an authorized operator can call `setIsAuthorized(accomplice, true, victim)` at any time — including while a revocation transaction is pending in the mempool. [1](#0-0) 

The protocol's own NatSpec explicitly acknowledges this behavior: [2](#0-1) 

**Attack Flow**

1. Alice calls `setIsAuthorized(Bob, true, Alice)` — Bob is now authorized.
2. Alice decides to revoke Bob and broadcasts `setIsAuthorized(Bob, false, Alice)`.
3. Bob observes Alice's pending transaction in the mempool.
4. Bob front-runs it by calling `setIsAuthorized(Charlie, true, Alice)` (valid because `isAuthorized[Alice][Bob]` is still `true` at this point).
5. Alice's revocation confirms: `isAuthorized[Alice][Bob] = false`.
6. `isAuthorized[Alice][Charlie] = true` — Charlie retains full authorization over Alice's positions.

This is structurally identical to the ERC20 approve race: the entity whose access is being revoked exploits the window between the pending revocation and its confirmation to preserve (or transfer) their access.

**Scope of Authorized Access**

Once Charlie is authorized, they can call every position-mutating function on Alice's behalf:
- `withdraw` — drain Alice's credit/loan tokens
- `withdrawCollateral` — seize Alice's collateral
- `setConsumed` — cancel or manipulate Alice's offers
- `setIsAuthorized` — further delegate to additional accounts, making the attack chain unbounded [3](#0-2) [4](#0-3) [5](#0-4) 

---

### Impact Explanation

**Direct theft of assets.** An attacker who was legitimately authorized (e.g., a smart-contract integration, a third-party operator) can permanently retain control over a user's positions after the user believes they have revoked access. All credit, collateral, and offer state belonging to the victim remain accessible to the attacker's accomplice. The attack chain is unbounded: Charlie can authorize Dave, Dave can authorize Eve, etc., making it impossible for the victim to enumerate and revoke all delegated accounts without a protocol-level mechanism to do so.

---

### Likelihood Explanation

**Realistic.** The preconditions are:
- The attacker was previously authorized by the victim (a normal, intended protocol flow).
- The attacker monitors the mempool for revocation transactions (standard MEV infrastructure).
- The attacker submits a higher-gas-price front-run transaction.

No privileged protocol roles are required. The attack is executable by any EOA or smart contract that was ever authorized by the victim. On chains with a public mempool (Ethereum mainnet, most L2s), this is straightforward to execute.

---

### Recommendation

Apply the same mitigation pattern recommended for ERC20 approve front-running: prevent an authorized account from granting further authorizations on behalf of the user, or require the user themselves (`msg.sender == onBehalf`) to grant new authorizations. Specifically:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    // Only the account owner (not a delegate) may modify authorizations
    require(onBehalf == msg.sender, Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
    emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
}
```

If delegated authorization-granting is a required feature, provide a `revokeAll` mechanism (e.g., an incrementing nonce or generation counter) so a user can atomically invalidate all existing delegations in a single transaction, analogous to OpenZeppelin's `increaseAllowance`/`decreaseAllowance` pattern.

---

### Proof of Concept

**Actors:**
- `Alice` — victim (position owner)
- `Bob` — malicious operator (previously authorized by Alice)
- `Charlie` — Bob's accomplice (initially unauthorized)

**Steps:**

```
// Setup: Alice authorizes Bob
Alice → Midnight.setIsAuthorized(Bob, true, Alice)
// isAuthorized[Alice][Bob] = true

// Alice supplies collateral and has credit in market M
Alice → Midnight.supplyCollateral(M, 0, 1000e18, Alice)

// Alice decides to revoke Bob, broadcasts tx (pending in mempool):
Alice → Midnight.setIsAuthorized(Bob, false, Alice)  // PENDING

// Bob sees Alice's pending tx, front-runs with higher gas:
Bob  → Midnight.setIsAuthorized(Charlie, true, Alice)
// isAuthorized[Alice][Charlie] = true  ← confirmed BEFORE Alice's revocation

// Alice's revocation confirms:
// isAuthorized[Alice][Bob] = false  ✓ (Bob revoked)
// isAuthorized[Alice][Charlie] = true  ✗ (Charlie still authorized)

// Charlie drains Alice's collateral:
Charlie → Midnight.withdrawCollateral(M, 0, 1000e18, Alice, Charlie)
// Alice's 1000e18 collateral transferred to Charlie
```

**Expected outcome:** Alice's revocation of Bob succeeds, but Charlie retains full authorization and drains Alice's collateral. Alice has no on-chain mechanism to discover or enumerate all accounts that Bob may have authorized on her behalf. [6](#0-5) [7](#0-6)

### Citations

**File:** src/Midnight.sol (L107-108)
```text
/// contracts might re-use Midnight's authorization mapping too (e.g ratifiers and authorizers). In particular,
/// authorized accounts can authorize other accounts on behalf of the user.
```

**File:** src/Midnight.sol (L481-482)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L524-545)
```text
    function supplyCollateral(Market memory market, uint256 collateralIndex, uint256 assets, address onBehalf)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        address collateralToken = market.collateralParams[collateralIndex].token;

        Position storage _position = position[id][onBehalf];
        uint256 oldCollateral = _position.collateral[collateralIndex];
        _position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets);

        if (oldCollateral == 0 && assets > 0) {
            uint128 newCollateralBitmap = _position.collateralBitmap.setBit(collateralIndex);
            _position.collateralBitmap = newCollateralBitmap;
            require(
                UtilsLib.countBits(newCollateralBitmap) <= MAX_COLLATERALS_PER_BORROWER, TooManyActivatedCollaterals()
            );
        }

        emit EventsLib.SupplyCollateral(msg.sender, id, collateralToken, assets, onBehalf);

        SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
```

**File:** src/Midnight.sol (L549-556)
```text
    function withdrawCollateral(
        Market memory market,
        uint256 collateralIndex,
        uint256 assets,
        address onBehalf,
        address receiver
    ) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L723-724)
```text
    function setConsumed(bytes32 group, uint256 amount, address onBehalf) external {
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
