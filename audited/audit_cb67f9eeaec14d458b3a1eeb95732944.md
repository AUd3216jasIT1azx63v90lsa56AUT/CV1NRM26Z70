Based on my investigation of the codebase, all code references check out and the claim is technically accurate.

**Code verification:**

- Lines 675–676 of `src/Midnight.sol` unconditionally increment `_marketState.withdrawable` and decrement `_position.debt` before the token pull. [1](#0-0) 
- Line 717 of `src/Midnight.sol` is where `safeTransferFrom` actually pulls the loan token — after accounting is already committed. [2](#0-1) 
- `SafeTransferLib.safeTransferFrom` only checks that `transferFrom` returns `true`; it does not measure the contract's balance delta. [3](#0-2) 
- `withdraw` decrements `withdrawable` and transfers tokens 1:1 with no balance check, so an inflated `withdrawable` directly causes insolvency for late withdrawers. [4](#0-3) 
- The identical pattern exists in `repay` (lines 508–509 and 520). [5](#0-4) 
- `touchMarket` imposes no restriction on the loan token type. [6](#0-5) 

**Scope/exclusion check:**

- `SECURITY.md` contains no exclusion for fee-on-transfer tokens. [7](#0-6) 
- `RESEARCHER.md` contains no exclusion for fee-on-transfer tokens. [8](#0-7) 
- `live_context.json` line 233 explicitly states: *"fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded"* — and no such exclusion exists anywhere in the repo. [9](#0-8) 
- The Certora `Solvency.spec` assumption about correct ERC20 transfers is a formal-verification simplification, not an on-chain guard. 

---

Audit Report

## Title
Fee-on-Transfer Loan Token Inflates `withdrawable` in `liquidate` and `repay`, Causing Protocol Insolvency - (File: src/Midnight.sol)

## Summary
In `liquidate` and `repay`, `marketState[id].withdrawable` is incremented by the full `repaidUnits`/`units` amount before `SafeTransferLib.safeTransferFrom` pulls the loan token from the payer. When the loan token charges a transfer fee, the contract receives only `amount * (1 - fee)` tokens but records the full `amount` as withdrawable. This gap accumulates across every liquidation and repayment, causing the contract's actual token balance to fall short of `withdrawable`, making the protocol insolvent for lenders who withdraw last.

## Finding Description
**Root cause:** Accounting state is mutated before the external token pull, and `SafeTransferLib.safeTransferFrom` only verifies that `transferFrom` returns `true` — it does not measure the contract's balance delta.

**Code path in `liquidate`:**
```solidity
// src/Midnight.sol:675-676 — accounting committed first
_marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
_position.debt -= UtilsLib.toUint128(repaidUnits);
// ...event, collateral transfer, optional callback...
// src/Midnight.sol:717 — token pull happens last
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**Code path in `repay`:**
```solidity
// src/Midnight.sol:508-509
position[id][onBehalf].debt -= UtilsLib.toUint128(units);
marketState[id].withdrawable += UtilsLib.toUint128(units);
// src/Midnight.sol:520
SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

For a fee-on-transfer token, `transferFrom(payer, address(this), R)` delivers only `R * (1 - fee)` to the contract while returning `true`. `safeTransferFrom` does not revert. The result per call: `withdrawable` increases by `R`, but the contract balance increases by only `R * (1 - fee)`.

**Why existing checks fail:**
- `SafeTransferLib` checks only the boolean return value, not the balance delta.
- `touchMarket` has no restriction on loan token type; any unprivileged user can create a market with a fee-on-transfer token.
- The Certora `Solvency.spec` assumption that tokens transfer correctly is a formal-verification simplification with no on-chain enforcement.
- `withdraw` transfers `units` tokens and decrements `withdrawable` by `units` with no balance check, so the inflated `withdrawable` directly causes under-collateralization of lender claims.

## Impact Explanation
After each liquidation or repayment with a fee-on-transfer loan token, `marketState[id].withdrawable` exceeds `loanToken.balanceOf(address(this))` by `repaidUnits * fee`. As lenders call `withdraw`, the contract's actual balance is exhausted before `withdrawable` reaches zero. The last lenders to withdraw receive nothing — a direct, permanent loss of funds. This constitutes protocol insolvency, which is an explicitly listed high-value bug class in `live_context.json`.

## Likelihood Explanation
**Preconditions:**
1. A market with a fee-on-transfer loan token must exist. `touchMarket` is permissionless and imposes no token-type restriction — any unprivileged user can create such a market.
2. At least one liquidatable position (unhealthy borrower or post-maturity debt) must exist in that market.

**Feasibility:** Fee-on-transfer tokens (deflationary/tax tokens) are a well-known and deployed ERC20 variant. `live_context.json` explicitly flags this class as requiring testing if not excluded, and no exclusion exists in `SECURITY.md`, `RESEARCHER.md`, or anywhere in the codebase. The exploit is repeatable: every liquidation and repayment in such a market widens the insolvency gap by `repaidUnits * fee`. No privileged access is required.

## Recommendation
1. **Measure balance delta:** Replace the fixed-amount accounting increment with a before/after balance check:
   ```solidity
   uint256 balanceBefore = IERC20(market.loanToken).balanceOf(address(this));
   SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
   uint256 received = IERC20(market.loanToken).balanceOf(address(this)) - balanceBefore;
   _marketState.withdrawable += UtilsLib.toUint128(received);
   _position.debt -= UtilsLib.toUint128(received);
   ```
   Apply the same fix to `repay`.
2. **Alternatively**, document and enforce that fee-on-transfer tokens are not supported as loan tokens, and add an on-chain guard in `touchMarket` (e.g., a whitelist or a transfer-delta probe at market creation).

## Proof of Concept
1. Deploy a mock ERC20 that deducts a 1% fee on every `transferFrom` call.
2. Call `touchMarket` with this token as `market.loanToken` (no privilege required).
3. Have a lender supply credit and a borrower take debt.
4. Make the borrower's position liquidatable (e.g., manipulate the oracle or advance past maturity).
5. Call `liquidate` with `repaidUnits = 1000`.
6. Assert: `marketState[id].withdrawable` increased by `1000`, but `loanToken.balanceOf(address(midnight))` increased by only `990`.
7. Repeat step 5 multiple times; the gap grows linearly.
8. Have all lenders call `withdraw`; the last lender's `withdraw` call reverts or transfers zero, proving insolvency.

### Citations

**File:** src/Midnight.sol (L493-499)
```text
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
```

**File:** src/Midnight.sol (L508-520)
```text
        position[id][onBehalf].debt -= UtilsLib.toUint128(units);
        marketState[id].withdrawable += UtilsLib.toUint128(units);

        address payer = callback != address(0) ? callback : msg.sender;
        emit EventsLib.Repay(msg.sender, id, units, onBehalf, payer);

        if (callback != address(0)) {
            require(
                IRepayCallback(callback).onRepay(id, market, units, onBehalf, data) == CALLBACK_SUCCESS,
                WrongRepayCallbackReturnValue()
            );
        }
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), units);
```

**File:** src/Midnight.sol (L675-676)
```text
            _marketState.withdrawable += UtilsLib.toUint128(repaidUnits);
            _position.debt -= UtilsLib.toUint128(repaidUnits);
```

**File:** src/Midnight.sol (L717-717)
```text
        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/Midnight.sol (L755-791)
```text
    function touchMarket(Market memory market) public returns (bytes32) {
        bytes32 id = toId(market);
        if (marketState[id].tickSpacing == 0) {
            require(market.maturity <= block.timestamp + 100 * 365 days, MaturityTooFar());
            require(market.collateralParams.length > 0, NoCollateralParams());
            require(market.collateralParams.length <= MAX_COLLATERALS, TooManyCollateralParams());
            address previousCollateralToken;
            for (uint256 i = 0; i < market.collateralParams.length; i++) {
                address collateralToken = market.collateralParams[i].token;
                require(collateralToken > previousCollateralToken, CollateralParamsNotSorted());
                uint256 lltv = market.collateralParams[i].lltv;
                require(isLltvAllowed(lltv), LltvNotAllowed());
                require(
                    market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_LOW)
                        || market.collateralParams[i].maxLif == maxLif(lltv, LIQUIDATION_CURSOR_HIGH),
                    InvalidMaxLif()
                );
                previousCollateralToken = collateralToken;
            }

            MarketState storage _marketState = marketState[id];
            _marketState.tickSpacing = DEFAULT_TICK_SPACING;
            uint16[7] memory _defaultSettlementFeeCbp = defaultSettlementFeeCbp[market.loanToken];
            _marketState.settlementFeeCbp0 = _defaultSettlementFeeCbp[0];
            _marketState.settlementFeeCbp1 = _defaultSettlementFeeCbp[1];
            _marketState.settlementFeeCbp2 = _defaultSettlementFeeCbp[2];
            _marketState.settlementFeeCbp3 = _defaultSettlementFeeCbp[3];
            _marketState.settlementFeeCbp4 = _defaultSettlementFeeCbp[4];
            _marketState.settlementFeeCbp5 = _defaultSettlementFeeCbp[5];
            _marketState.settlementFeeCbp6 = _defaultSettlementFeeCbp[6];
            _marketState.continuousFee = defaultContinuousFee[market.loanToken];
            IdLib.storeInCode(market, INITIAL_CHAIN_ID);

            emit EventsLib.MarketCreated(market, id);
        }
        return id;
    }
```

**File:** src/libraries/SafeTransferLib.sol (L24-34)
```text
    function safeTransferFrom(address token, address from, address to, uint256 value) internal {
        require(token.code.length > 0, NoCode());

        (bool success, bytes memory returndata) = token.call(abi.encodeCall(IERC20.transferFrom, (from, to, value)));
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returndata, 0x20), mload(returndata))
            }
        }
        require(returndata.length == 0 || abi.decode(returndata, (bool)), TransferFromReturnedFalse());
    }
```

**File:** SECURITY.md (L1-65)
```markdown
# Common Vulnerability Exclusion List

## Out of Scope & Rules

These are the default impacts recommended to projects to mark as out of scope for their bug bounty program. The actual list of out-of-scope impacts differs from program to program.

### General

- Impacts requiring attacks that the reporter has already exploited themselves, leading to damage.
- Impacts caused by attacks requiring access to leaked keys/credentials.
- Impacts caused by attacks requiring access to privileged addresses (governance, strategist), except in cases where the contracts are intended to have no privileged access to functions that make the attack possible.
- Impacts relying on attacks involving the depegging of an external stablecoin where the attacker does not directly cause the depegging due to a bug in code.
- Mentions of secrets, access tokens, API keys, private keys, etc. in GitHub will be considered out of scope without proof that they are in use in production.
- Best practice recommendations.
- Feature requests.
- Impacts on test files and configuration files, unless stated otherwise in the bug bounty program.

### Smart Contracts / Blockchain DLT

- Incorrect data supplied by third-party oracles.
- Impacts requiring basic economic and governance attacks (e.g. 51% attack).
- Lack of liquidity impacts.
- Impacts from Sybil attacks.
- Impacts involving centralization risks.

Note: This does not exclude oracle manipulation/flash-loan attacks.

### Websites and Apps

- Theoretical impacts without any proof or demonstration.
- Impacts involving attacks requiring physical access to the victim device.
- Impacts involving attacks requiring access to the local network of the victim.
- Reflected plain text injection (e.g. URL parameters, path, etc.).
- This does not exclude reflected HTML injection with or without JavaScript.
- This does not exclude persistent plain text injection.
- Any impacts involving self-XSS.
- Captcha bypass using OCR without impact demonstration.
- CSRF with no state-modifying security impact (e.g. logout CSRF).
- Impacts related to missing HTTP security headers (such as `X-FRAME-OPTIONS`) or cookie security flags (such as `httponly`) without demonstration of impact.
- Server-side non-confidential information disclosure, such as IPs, server names, and most stack traces.
- Impacts causing only the enumeration or confirmation of the existence of users or tenants.
- Impacts caused by vulnerabilities requiring unprompted, in-app user actions that are not part of the normal app workflows.
- Lack of SSL/TLS best practices.
- Impacts that only require DDoS.
- UX and UI impacts that do not materially disrupt use of the platform.
- Impacts primarily caused by browser/plugin defects.
- Leakage of non-sensitive API keys (e.g. Etherscan, Infura, Alchemy, etc.).
- Any vulnerability exploit requiring browser bugs for exploitation (e.g. CSP bypass).
- SPF/DMARC misconfigured records.
- Missing HTTP headers without demonstrated impact.
- Automated scanner reports without demonstrated impact.
- UI/UX best practice recommendations.
- Non-future-proof NFT rendering.

## Prohibited Activities

The following activities are prohibited by default on bug bounty programs on Immunefi. Projects may add further restrictions to their own program.

- Any testing on mainnet or public testnet deployed code; all testing should be done on local forks of either public testnet or mainnet.
- Any testing with pricing oracles or third-party smart contracts.
- Attempting phishing or other social engineering attacks against employees and/or customers.
- Any testing with third-party systems and applications (e.g. browser extensions), as well as websites (e.g. SSO providers, advertising networks).
- Any denial-of-service attacks that are executed against project assets.
- Automated testing of services that generates significant amounts of traffic.
- Public disclosure of an unpatched vulnerability in an embargoed bounty.
```

**File:** RESEARCHER.md (L1-130)
```markdown
# RESEARCHER Playbook (Attacker-First, No-Privilege Baseline)

Last updated: April 27, 2026

## Role

You are a senior adversarial security researcher for the target project under
review.

Your goal is to find real, exploitable vulnerabilities that can cause:

- Direct theft or unauthorized movement of assets/value.
- Unauthorized state changes or privilege escalation.
- Permanent lock, freeze, or unrecoverable corruption of user/project state.
- Service unavailability or severe degradation under realistic attacker input.
- Critical integrity failures in consensus, state transition, or trust model.

Read and apply `SECURITY.md` first. Do not report findings that are explicitly
out of scope.

## Non-Negotiable Rules

- Think like a real attacker, not a style reviewer.
- Baseline attacker has **no privileged access**:
    - no admin/owner/governance/operator keys
    - no leaked secrets/credentials
    - no internal or physical network access
- Treat privileged-path findings as valid only if the program explicitly marks
  those assumptions as in scope.
- Every claim must include attacker preconditions, trigger path, and concrete
  impact.
- Prefer one proven exploit over many speculative issues.
- No "best practice only" findings without exploitability.
- No vague language ("could", "might", "potentially") without evidence.

## Attacker Profiles You Must Emulate

- External attacker with no privileged keys (default).
- Malicious normal user abusing valid product/protocol flows.
- Malicious API/RPC/web client submitting crafted inputs at scale.
- Malicious peer/integrator/oracle only where that role is reachable without
  privileged assumptions.

## Priority Attack Surfaces (Any Project)

- Authentication and authorization boundaries.
- Input parsing, deserialization, and schema validation.
- State transition logic and invariant enforcement.
- Financial/accounting/token math and rounding behavior.
- Concurrency boundaries (race conditions, TOCTOU, replay).
- Storage/proof/merkle/state-root trust assumptions.
- API/RPC/websocket/message handlers and rate-limit boundaries.
- Resource exhaustion paths (CPU, memory, disk, connection slots).
- Feature flags, upgrade/migration, and version-compatibility edges.
- Cryptographic verification and domain separation assumptions.

## High-Value Scenarios To Always Test

- Authorization bypass leading to privileged action as unprivileged user.
- Replay/nonce/sequence misuse enabling duplicate or unauthorized effects.
- Signature/proof verification bypass with malformed but accepted input.
- Accounting drift from precision/rounding/unit conversion errors.
- Inconsistent state acceptance across nodes/services/components.
- Permanent lock/freeze states created through reachable user actions.
- Cross-tenant or cross-user data exposure and integrity breaks.
- Request/message patterns causing sustained crash or unbounded resource usage.
- Upgrade or activation edge cases violating invariants.

## Audit Method (Execution Order)

1. Define invariants before implementation review.
2. Enumerate attacker-controlled entry points.
3. Trace end-to-end: input -> validation -> authorization -> state mutation ->
   persistence -> propagation.
4. Attack trust boundaries:
    - external input -> parser/validator
    - user -> authz checks -> privileged action
    - API/RPC/peer message -> handler -> business logic
    - business logic -> storage/crypto/proof verification
5. Force edge cases:
    - max/min values, empty/zero, malformed encodings
    - duplicate/reordered/replayed requests
    - stale/future context and timing boundaries
    - feature enabled/disabled mismatches
6. Confirm exploitability with realistic, no-privilege capabilities.
7. Quantify impact using `SECURITY.md` rules.

## Evidence Standard (Required For Any Valid Finding)

- Exact file(s), function(s), and line range(s).
- Root cause and violated assumption.
- Realistic attacker preconditions (no-privilege by default).
- End-to-end exploit path.
- Existing checks and why they fail.
- Concrete impact category and severity rationale.
- Reproducible PoC or deterministic equivalent reasoning.

## Immediate Rejection Filters

- No concrete exploit path.
- No measurable impact.
- Impossible or out-of-scope preconditions.
- Requires direct break of standard cryptographic primitives.
- Pure phishing/social engineering/user self-harm.
- Pure documentation/style/performance feedback with no security break.

## Reporting Format (Use Exactly)

### Title
[Clear vulnerability statement]

### Summary
[2-3 sentence overview]

### Finding Description
[Root cause, code path, exploit flow]

### Impact Explanation
[Concrete impact and severity]

### Likelihood Explanation
[Realistic feasibility and attacker requirements]

### Recommendation
[Specific fix with rationale]

### Proof of Concept
[Reproduction steps, inputs, and expected outcome]

If not valid, output exactly:
```

**File:** live_context.json (L230-235)
```json
    "external_calls": [
      "callbacks must not corrupt partial state through reentrancy",
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
    ]
```
