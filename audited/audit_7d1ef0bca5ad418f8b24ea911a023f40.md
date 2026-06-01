Based on my analysis of the code, here is my determination:

### Title
Stale Root Ratification in SetterRatifier Survives Operator Authorization Revocation - (File: src/ratifiers/SetterRatifier.sol)

### Summary
`SetterRatifier.setIsRootRatified` stores ratification as persistent on-chain state (`isRootRatified[maker][root] = true`) at the time an authorized operator calls it. Unlike `EcrecoverRatifier`, which re-checks `IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer)` at every `isRatified` call, `SetterRatifier.isRatified` only checks the stored boolean flag and never re-validates whether the entity that set it is still authorized. Consequently, revoking an operator's authorization via `Midnight.setIsAuthorized(operator, false)` does not invalidate roots that operator previously ratified, leaving those roots permanently active until the maker explicitly calls `setIsRootRatified(maker, root, false)`.

### Finding Description

**Root cause — `SetterRatifier.isRatified` (lines 30–36):**

```solidity
function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    require(msg.sender == MIDNIGHT, NotMidnight());
    (bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
        abi.decode(ratifierData, (bytes32, uint256, bytes32[]));
    require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
    require(isRootRatified[offer.maker][root], NotRatified());   // ← only checks stored flag
    return CALLBACK_SUCCESS;
}
```

There is no call to `IMidnight(MIDNIGHT).isAuthorized(offer.maker, setter)` here. Compare with `EcrecoverRatifier.isRatified` (line 44), which does:

```solidity
require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
```

That live re-check is what makes `EcrecoverRatifier` correctly invalidate when authorization is revoked (confirmed by `testIsRatifiedRevokeAuthorizationInvalidates` in `test/EcrecoverRatifierTest.sol`). `SetterRatifier` has no equivalent test or check.

**Exploit path:**

1. `maker.setIsAuthorized(operator, true, maker)` — operator is authorized.
2. `operator.setIsRootRatified(maker, root, true)` — passes the check at line 25 (`IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender)`); `isRootRatified[maker][root]` is set to `true`.
3. `maker.setIsAuthorized(operator, false, maker)` — `isAuthorized[maker][operator]` is now `false`. The operator can no longer call any Midnight function on behalf of the maker.
4. `taker.take(offer, abi.encode(root, leafIndex, proof), units, taker, ...)` — `Midnight.take` calls `SetterRatifier.isRatified`; the only check is `isRootRatified[maker][root]` which is still `true`. The take succeeds.

**Why existing checks fail:**

- `Midnight.take` (line 346) checks `isAuthorized[taker][msg.sender]` — this is the *taker's* authorization, not the maker's.
- The ratifier authorization check in `take` is delegated entirely to `offer.ratifier.isRatified(...)`. For `SetterRatifier`, that check is only the stored flag.
- The maker has no automatic cleanup: `setIsAuthorized` only writes to `isAuthorized[onBehalf][authorized]` (line 733); it does not iterate or clear any ratifier state.
- The offer's `expiry` field is a separate check; if the operator set `offer.expiry = type(uint256).max` (as in the test at line 35 of `SetterRatifierTest.sol`), the offer never expires.

### Impact Explanation

Any taker can fill offers from a root ratified by a now-deauthorized operator, for as long as the offer's `expiry` has not passed and the market exists. Since `take` does not block on market maturity (the maturity check governs liquidations, not takes), if the offer's `expiry` extends past `market.maturity`, takes can occur post-maturity, creating new credit/debt positions in a matured market. Every such take is economically valid (credit matches debt), but it is unauthorized: the maker revoked the operator precisely to stop further offer fills, and the protocol does not honor that intent for `SetterRatifier`-backed offers.

### Likelihood Explanation

**Preconditions:** maker authorizes an operator (common pattern), operator ratifies at least one root (the operator's primary purpose), maker later revokes the operator (e.g., key rotation, end of service agreement). All three steps are normal, unprivileged user actions. No oracle manipulation, admin access, or impossible values are required. The scenario is repeatable for every root the operator ratified before revocation.

### Recommendation

In `SetterRatifier.isRatified`, record *who* ratified each root and re-validate their authorization at fill time:

```solidity
mapping(address maker => mapping(bytes32 root => address setter)) public rootRatifier;

function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
    require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
    isRootRatified[maker][root] = newIsRootRatified;
    rootRatifier[maker][root] = msg.sender;   // record setter
    emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
}

function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
    require(msg.sender == MIDNIGHT, NotMidnight());
    (bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
        abi.decode(ratifierData, (bytes32, uint256, bytes32[]));
    require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
    require(isRootRatified[offer.maker][root], NotRatified());
    address setter = rootRatifier[offer.maker][root];
    require(setter == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, setter), Unauthorized());
    return CALLBACK_SUCCESS;
}
```

Alternatively, document clearly that revoking an operator does **not** un-ratify roots and that makers must call `setIsRootRatified(maker, root, false)` for each root before or alongside revocation — but this is a weaker mitigation given the asymmetry with `EcrecoverRatifier`.

### Proof of Concept

```solidity
// Foundry unit test
function testStaleRatificationSurvivesRevocation() public {
    // Setup: lender is maker, borrower is operator
    Offer memory offer = makeOffer(lender);   // expiry = block.timestamp + 200, ratifier = setterRatifier
    bytes32 root = HashLib.hashOffer(offer);

    // Step 1: maker authorizes operator
    vm.prank(lender);
    midnight.setIsAuthorized(borrower, true, lender);

    // Step 2: operator ratifies root on behalf of maker
    vm.prank(borrower);
    setterRatifier.setIsRootRatified(lender, root, true);
    assertTrue(setterRatifier.isRootRatified(lender, root));

    // Step 3: maker revokes operator
    vm.prank(lender);
    midnight.setIsAuthorized(borrower, false, lender);
    assertFalse(midnight.isAuthorized(lender, borrower));

    // Step 4: taker fills offer — MUST REVERT but does not
    // Expected: revert with NotRatified or Unauthorized
    // Actual: succeeds because isRootRatified[lender][root] == true
    vm.prank(borrower);  // borrower as taker
    // assert this does NOT revert:
    midnight.take(offer, abi.encode(root, 0, new bytes32[](0)), 0, borrower, borrower, address(0), hex"");

    // Assertion: after revocation, no take using operator-ratified root should succeed
    // This assertion FAILS, proving the bug:
    // vm.expectRevert(); // would fail — take succeeds
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/ratifiers/SetterRatifier.sol (L24-28)
```text
    function setIsRootRatified(address maker, bytes32 root, bool newIsRootRatified) public {
        require(maker == msg.sender || IMidnight(MIDNIGHT).isAuthorized(maker, msg.sender), Unauthorized());
        isRootRatified[maker][root] = newIsRootRatified;
        emit SetIsRootRatified(msg.sender, maker, root, newIsRootRatified);
    }
```

**File:** src/ratifiers/SetterRatifier.sol (L30-37)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(isRootRatified[offer.maker][root], NotRatified());
        return CALLBACK_SUCCESS;
    }
```

**File:** src/ratifiers/EcrecoverRatifier.sol (L42-45)
```text
        address _signer = ecrecover(digest, sig.v, sig.r, sig.s);
        require(_signer != address(0), InvalidSignature());
        require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
        return CALLBACK_SUCCESS;
```

**File:** src/Midnight.sol (L731-735)
```text
    function setIsAuthorized(address authorized, bool newIsAuthorized, address onBehalf) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        isAuthorized[onBehalf][authorized] = newIsAuthorized;
        emit EventsLib.SetIsAuthorized(msg.sender, authorized, newIsAuthorized, onBehalf);
    }
```

**File:** test/EcrecoverRatifierTest.sol (L185-205)
```text
    function testIsRatifiedRevokeAuthorizationInvalidates() public {
        Offer memory offer = makeOffer(lender);
        bytes32 _root = HashLib.hashOffer(offer);

        vm.prank(lender);

        midnight.setIsAuthorized(borrower, true, lender);
        bytes memory ratifierData = buildRatifierData(_root, borrower);

        // Works while authorized.
        vm.prank(address(midnight));
        ecrecoverRatifier.isRatified(offer, ratifierData);

        // Revoke.
        vm.prank(lender);
        midnight.setIsAuthorized(borrower, false, lender);

        vm.prank(address(midnight));
        vm.expectRevert(IEcrecoverRatifier.Unauthorized.selector);
        ecrecoverRatifier.isRatified(offer, ratifierData);
    }
```

**File:** test/SetterRatifierTest.sol (L48-61)
```text
    function testIsRatifiedAuthorizedSetterCanRatifyOnBehalf() public {
        Offer memory offer = makeOffer(lender);
        bytes32 _root = HashLib.hashOffer(offer);

        vm.prank(lender);
        midnight.setIsAuthorized(borrower, true, lender);

        vm.prank(borrower);
        setterRatifier.setIsRootRatified(lender, _root, true);

        vm.prank(address(midnight));
        bytes32 result = setterRatifier.isRatified(offer, abi.encode(_root, 0, new bytes32[](0)));
        assertEq(result, CALLBACK_SUCCESS);
    }
```
