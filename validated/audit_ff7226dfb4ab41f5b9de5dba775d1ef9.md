Audit Report

## Title
Attacker-controlled `offer.callback` used as unconsented payer drains victim callback contract balance - (File: `src/Midnight.sol`)

## Summary
In `Midnight.take`, when `offer.buy = true` and `offer.callback` is non-zero, the protocol unconditionally assigns `payer = offer.callback` and executes `safeTransferFrom(loanToken, offer.callback, ...)` after invoking `onBuy` on that address. Because `offer.callback` is a field set unilaterally by `offer.maker` with no protocol-level check that the callback contract has consented to acting as payer for this offer, any attacker controlling `offer.maker` can point `offer.callback` at any `IBuyCallback` implementor that approves Midnight inside `onBuy` without validating the `buyer` argument, causing Midnight to drain that contract's token balance without its consent.

## Finding Description

**Exact code path (`src/Midnight.sol`):**

```
line 420: address buyerCallback = offer.buy ? offer.callback : takerCallback;
          → buyerCallback = offer.callback  (when offer.buy = true)

line 422: address payer = buyerCallback != address(0) ? buyerCallback : (offer.buy ? buyer : msg.sender);
          → payer = offer.callback  (when offer.callback != address(0))

lines 445–453: IBuyCallback(buyerCallback).onBuy(..., buyer, ...) must return CALLBACK_SUCCESS

line 455: safeTransferFrom(loanToken, payer, address(this), buyerAssets - sellerAssets)
line 456: safeTransferFrom(loanToken, payer, receiver, sellerAssets)
```

**Root cause:** `offer.callback` is embedded in the offer struct by `offer.maker`. The ratifier check at line 355 only verifies `isAuthorized[offer.maker][offer.ratifier]` — the maker self-authorizes their own ratifier. The ratifier check at line 356 verifies `IRatifier.isRatified(offer, ...)` — an attacker-deployed dummy ratifier always returns `CALLBACK_SUCCESS`. There is no check of the form `isAuthorized[offer.callback][offer.maker]` or any equivalent mechanism requiring `offer.callback` to consent to being the payer for this specific offer.

**Structural asymmetry:** When `offer.buy = false`, the taker provides `takerCallback` and the taker is `msg.sender` (or explicitly authorized by `msg.sender`) — consent is implicit. When `offer.buy = true`, the maker provides `offer.callback` but the maker is not `msg.sender`; their consent is proxied through a ratifier they self-authorize, which does not bind `offer.callback`.

**Attacker inputs:**
- `offer.buy = true`
- `offer.maker = attackerMaker`
- `offer.callback = victimContract` (any `IBuyCallback` implementor that approves Midnight and returns `CALLBACK_SUCCESS` without checking `buyer`)
- `offer.ratifier = dummyRatifier` (attacker-deployed, always returns `CALLBACK_SUCCESS`)
- `taker = attackerTaker` (`!= attackerMaker`)

**Exploit flow:**
1. Attacker deploys `DummyRatifier` returning `CALLBACK_SUCCESS` unconditionally.
2. `attackerMaker` calls `setIsAuthorized(dummyRatifier, true, attackerMaker)` — satisfies line 355.
3. Attacker crafts offer with above fields; ratifier check at line 356 passes.
4. `attackerTaker` calls `take(offer, ratifierData, units, attackerTaker, receiver, address(0), "")`.
5. `onBuy` is called on `victimContract`. If `victimContract` does not validate `buyer`, it approves Midnight for `buyerAssets` and returns `CALLBACK_SUCCESS`.
6. Midnight executes `safeTransferFrom(loanToken, victimContract, address(this), buyerAssets - sellerAssets)` and `safeTransferFrom(loanToken, victimContract, receiver, sellerAssets)`, draining `victimContract`'s balance.

**Why existing checks fail:**
- `isAuthorized[offer.maker][offer.ratifier]` — attacker self-authorizes their dummy ratifier; passes.
- `IRatifier.isRatified(offer, ...)` — dummy ratifier always returns `CALLBACK_SUCCESS`; passes.
- `IBuyCallback(buyerCallback).onBuy(...)` — victim contract returns `CALLBACK_SUCCESS` without checking `buyer`; passes.
- No check exists for `isAuthorized[offer.callback][offer.maker]` or any consent from `offer.callback`.

## Impact Explanation
Any buyer callback contract that (a) implements `IBuyCallback.onBuy` and returns `CALLBACK_SUCCESS` without validating the `buyer` argument, (b) holds or can acquire a token balance, and (c) approves Midnight for tokens inside `onBuy` can have its entire approved balance drained by an unprivileged attacker. The attacker receives credit in the protocol (as `buyer = offer.maker`) funded entirely by the victim contract, constituting direct unauthorized theft. This is a Critical severity finding: direct theft of user/protocol funds with no privilege requirement.

## Likelihood Explanation
The preconditions are realistic. The canonical `LendCallback` test contract (`test/TakeTest.sol`, lines 1452–1471) is exactly the vulnerable pattern: it validates only the market ID, approves Midnight for `buyerAssets` inside `onBuy`, and returns `CALLBACK_SUCCESS` without checking `buyer`. Real-world callback contracts following this flash-loan-style pattern — the intended integrators of this protocol — are vulnerable. The attack is permissionless, requires no privileged access, and is repeatable against any qualifying victim contract. The attacker needs only two EOA addresses and a one-line dummy ratifier contract.

## Recommendation
Add a consent check requiring that `offer.callback` has explicitly authorized `offer.maker` to use it as a payer. For example, before using `offer.callback` as `payer`, require:

```solidity
require(
    offer.callback == address(0) || isAuthorized[offer.callback][offer.maker],
    CallbackUnauthorized()
);
```

This mirrors the existing `isAuthorized[taker][msg.sender]` pattern used for taker authorization (line 346) and the `isAuthorized[offer.maker][offer.ratifier]` pattern used for ratifier authorization (line 355), creating a consistent consent model across all three roles.

## Proof of Concept

**Minimal Foundry test plan:**

1. Deploy `DummyRatifier` that always returns `CALLBACK_SUCCESS`.
2. Deploy `VictimCallback` implementing `IBuyCallback.onBuy` that: validates market ID only, calls `ERC20(market.loanToken).approve(msg.sender, buyerAssets)`, and returns `CALLBACK_SUCCESS` — identical to `LendCallback` in `test/TakeTest.sol:1452–1471`.
3. Fund `VictimCallback` with loan tokens.
4. `attackerMaker` calls `setIsAuthorized(dummyRatifier, true, attackerMaker)`.
5. Craft offer: `buy=true`, `maker=attackerMaker`, `callback=victimCallback`, `ratifier=dummyRatifier`.
6. `attackerTaker` calls `take(offer, ...)`.
7. Assert: `VictimCallback` token balance is zero; `attackerTaker`'s designated receiver holds `sellerAssets`; Midnight holds `buyerAssets - sellerAssets`; `position[id][attackerMaker].credit > 0`.