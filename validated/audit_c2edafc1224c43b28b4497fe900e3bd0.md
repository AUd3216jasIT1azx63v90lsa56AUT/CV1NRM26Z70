### Title
Authorized Smart Contract Wallets Can Delegate Gate Access to Arbitrary Actors, Bypassing `enterGate` Restrictions — (`src/Midnight.sol`)

### Summary

Midnight's `isAuthorized` system allows any authorized account to further delegate authorization to arbitrary third parties on behalf of the original user. Because `enterGate` checks only the `buyer`/`seller` address (not `msg.sender`), a gate-verified user who authorizes a smart contract wallet enables that wallet — and any address it subsequently authorizes — to interact with gated markets as if they were the verified user, completely bypassing the gate's access controls.

### Finding Description

**Vulnerability class:** Authorization bypass (proxy delegation through transitive authorization)

**Root cause — transitive delegation in `setIsAuthorized`:** [1](#0-0) 

```solidity
function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
    require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
    isAuthorized[onBehalf][authorized] = newIsAuthorized;
```

Any account that holds `isAuthorized[user][account] == true` can call `setIsAuthorized(attacker, true, user)`, granting `attacker` full authorization on behalf of `user`. This is explicitly acknowledged in the contract's own NatSpec: [2](#0-1) 

**Root cause — gate checks the position owner, not `msg.sender`:** [3](#0-2) 

```solidity
require(
    offer.market.enterGate == address(0) || buyerCreditIncrease == 0
        || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
    BuyerGatedFromIncreasingCredit()
);
require(
    offer.market.enterGate == address(0) || sellerDebtIncrease == 0
        || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
    SellerGatedFromIncreasingDebt()
);
```

`buyer`/`seller` is the `taker` or `offer.maker` address — the position owner — not `msg.sender`. The taker authorization check only verifies that `msg.sender` is authorized by `taker`: [4](#0-3) 

```solidity
require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**Attack chain:**

1. Market M has an `enterGate` (e.g., KYC/sanctions gate) where `canIncreaseCredit(alice) == true`.
2. Alice (gate-verified) authorizes `SmartContractWallet` via `setIsAuthorized(SmartContractWallet, true, alice)`.
3. `SmartContractWallet` calls `setIsAuthorized(attacker, true, alice)` — succeeds because `isAuthorized[alice][SmartContractWallet] == true`.
4. Attacker calls `take(offer, ..., taker=alice, ...)`:
   - `isAuthorized[alice][attacker] == true` → `TakerUnauthorized` check passes.
   - Gate check: `canIncreaseCredit(alice)` → returns `true` (Alice is verified).
5. Attacker has executed a trade in the gated market under Alice's identity. Since the attacker is also authorized, they can call `withdraw` and `withdrawCollateral` on behalf of Alice to extract proceeds to any `receiver` address.

The `SmartContractWallet` does not need to be upgradeable — the `setIsAuthorized` function itself is the delegation vector. An upgradeable or `delegatecall`-capable wallet makes the attack even more covert (logic can be added post-authorization), but it is not required.

The same delegation chain applies to `withdraw`, `repay`, `supplyCollateral`, `withdrawCollateral`, and `setConsumed`, all of which share the same `isAuthorized[onBehalf][msg.sender]` check: [5](#0-4) [6](#0-5) 

### Impact Explanation

**High.** An attacker who obtains authorization from a gate-verified user (directly or through a chain) can:
- Lend or borrow in markets restricted by `enterGate`, bypassing KYC/sanctions/compliance controls.
- Withdraw credit or collateral from the verified user's position to an arbitrary `receiver`.
- Ratify offers on behalf of the verified user via `EcrecoverRatifier` and `SetterRatifier`, which also re-use the same `isAuthorized` mapping. [7](#0-6) [8](#0-7) 

The gate's access control is rendered ineffective for any verified user who has authorized a smart contract.

### Likelihood Explanation

**Low/Medium.** The precondition is that a gate-verified user authorizes a smart contract wallet — a normal and expected use case (e.g., authorizing `MidnightBundles` for atomic execution). The malicious step is that the authorized smart contract then delegates to an attacker. This requires either: (a) the smart contract wallet to be malicious/compromised, or (b) the smart contract wallet to be upgradeable and later modified. The `MidnightBundles` periphery contract itself is not upgradeable, but third-party integrators may deploy upgradeable wallets. The delegation path requires no privileged keys and is permissionless once the initial authorization is in place.

### Recommendation

1. **Restrict re-delegation:** Prevent authorized accounts from granting further authorizations on behalf of the original user. Require `onBehalf == msg.sender` in `setIsAuthorized`, or introduce a separate "admin authorization" tier that is the only one permitted to delegate.
2. **Gate on `msg.sender`:** Consider having `enterGate` check `msg.sender` in addition to (or instead of) `buyer`/`seller`, so that the actual transaction originator must also be gate-verified.
3. **Document the risk prominently:** If transitive delegation is intentional, gate deployers must be explicitly warned that authorizing any smart contract effectively extends gate access to all accounts that smart contract may later authorize.

### Proof of Concept

```
Setup:
  - Market M: enterGate = KYCGate (canIncreaseCredit returns true only for alice)
  - alice: KYC-verified EOA
  - SmartContractWallet: any contract (even non-upgradeable)
  - attacker: unverified EOA

Step 1: alice.call → Midnight.setIsAuthorized(SmartContractWallet, true, alice)
  Result: isAuthorized[alice][SmartContractWallet] = true

Step 2: SmartContractWallet.call → Midnight.setIsAuthorized(attacker, true, alice)
  Check: isAuthorized[alice][SmartContractWallet] == true → passes
  Result: isAuthorized[alice][attacker] = true

Step 3: attacker.call → Midnight.take(offer, ratifierData, units, taker=alice, ...)
  Check: isAuthorized[alice][attacker] == true → TakerUnauthorized passes
  Gate check: KYCGate.canIncreaseCredit(alice) == true → BuyerGatedFromIncreasingCredit passes
  Result: attacker executes trade in gated market under alice's identity

Step 4: attacker.call → Midnight.withdraw(market, units, onBehalf=alice, receiver=attacker)
  Check: isAuthorized[alice][attacker] == true → Unauthorized passes
  Result: attacker withdraws alice's credit to attacker's address
``` [1](#0-0) [4](#0-3) [3](#0-2) [9](#0-8)

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

**File:** src/Midnight.sol (L346-346)
```text
        require(taker == msg.sender || isAuthorized[taker][msg.sender], TakerUnauthorized());
```

**File:** src/Midnight.sol (L397-406)
```text
        require(
            offer.market.enterGate == address(0) || buyerCreditIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseCredit(buyer),
            BuyerGatedFromIncreasingCredit()
        );
        require(
            offer.market.enterGate == address(0) || sellerDebtIncrease == 0
                || IEnterGate(offer.market.enterGate).canIncreaseDebt(seller),
            SellerGatedFromIncreasingDebt()
        );
```

**File:** src/Midnight.sol (L481-483)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
```

**File:** src/Midnight.sol (L505-505)
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

**File:** src/ratifiers/EcrecoverRatifier.sol (L44-44)
```text
        require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
```

**File:** src/ratifiers/SetterRatifier.sol (L25-25)
```text
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
```

**File:** src/interfaces/IGate.sol (L5-8)
```text
interface IEnterGate {
    function canIncreaseCredit(address account) external view returns (bool);
    function canIncreaseDebt(address account) external view returns (bool);
}
```
