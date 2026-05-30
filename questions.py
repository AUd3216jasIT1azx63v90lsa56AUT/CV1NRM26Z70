import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the path from https:///github.com/dfinity/ICRC-1
SOURCE_REPO = "codertjay/midnight"
# todo: the name of the repository
REPO_NAME = "midnight"
run_number = os.environ.get('GITHUB_RUN_NUMBER', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    "src/Midnight.sol",
    "src/interfaces/ICallbacks.sol",
    "src/interfaces/IERC20.sol",
    "src/interfaces/IGate.sol",
    "src/interfaces/IMidnight.sol",
    "src/interfaces/IOracle.sol",
    "src/interfaces/IRatifier.sol",
    "src/libraries/ConstantsLib.sol",
    "src/libraries/EventsLib.sol",
    "src/libraries/IdLib.sol",
    "src/libraries/SafeTransferLib.sol",
    "src/libraries/TickLib.sol",
    "src/libraries/UtilsLib.sol",
    "src/periphery/ConsumableUnitsLib.sol",
    "src/periphery/EcrecoverAuthorizer.sol",
    "src/periphery/MidnightBundles.sol",
    "src/periphery/TakeAmountsLib.sol",
    "src/periphery/interfaces/IERC20Permit.sol",
    "src/periphery/interfaces/IEcrecoverAuthorizer.sol",
    "src/periphery/interfaces/IMidnightBundles.sol",
    "src/periphery/interfaces/IPermit2.sol",
    "src/ratifiers/EcrecoverRatifier.sol",
    "src/ratifiers/SetterRatifier.sol",
    "src/ratifiers/interfaces/IEcrecoverRatifier.sol",
    "src/ratifiers/interfaces/ISetterRatifier.sol",
    "src/ratifiers/libraries/HashLib.sol",
]

target_scopes = [
    # Critical / best targets
    "Critical Direct loss of user funds through incorrect credit, debt, collateral, withdrawable, or fee accounting",
    "Critical Protocol insolvency where Midnight contract token balances cannot cover collateral, withdrawable assets, or claimable fees",
    "Critical Unauthorized borrowing, withdrawal, collateral seizure, or debt reduction caused by broken authorization, signature, ratifier, gate, or callback logic",
    "Critical Bad debt creation or undercollateralized position creation through incorrect health checks, oracle handling, LLTV/LIF math, maturity logic, or collateral bitmap accounting",
    "Critical Liquidation bypass or incorrect liquidation that lets unhealthy borrowers avoid liquidation or lets liquidators seize excess collateral",
    "Critical Market settlement or maturity logic flaw that allows credit/debt units to redeem for more assets than economically owed",
    "Critical Offer/take logic flaw allowing a taker or maker to receive assets without paying the correct countervalue",
    "Critical Reentrancy or callback-driven state corruption across take, repay, withdraw, liquidation, fee claim, or collateral flows",
    "Critical Multicall or sequencing bug that breaks invariants only when multiple protocol actions are composed in one transaction",

    # High / strong bounty style
    "High Permanent or long-term freezing of user funds caused by broken withdraw, repay, liquidate, claim fee, or settlement paths",
    "High Incorrect consumed-offer accounting allowing offers to be overfilled, reused, undercharged, or griefed after partial fill",
    "High Incorrect credit/debt netting where a user can hold incompatible position states or bypass required position updates",
    "High Incorrect loss-factor accounting causing unfair debt reduction, credit destruction, bad debt misallocation, or position desynchronization",
    "High Oracle edge-case bug causing valid borrowers to become liquidatable, invalid borrowers to stay healthy, or withdrawals/liquidations to revert unexpectedly",
    "High Rounding, fixed-point math, mulDiv, fee, spread, or time-to-maturity error that creates extractable value or systematic value leakage",
    "High Access-control failure in fee setter, role setter, tick-spacing setter, fee claimer, market parameter setter, or authorization setter",
    "High Gate or ratifier bypass allowing restricted markets, restricted liquidation, or restricted credit/debt increase to be accessed by unauthorized users",
    "High Callback payer confusion where tokens are pulled from the wrong party or an explicit payer can be bypassed",
    "High Non-standard ERC20 behavior, fee-on-transfer behavior, callback revert behavior, or false-return handling that breaks accounting or freezes funds",
    "High Collateral bitmap corruption causing health checks or liquidation loops to skip active collateral or include inactive collateral",
    "High Market creation parameter bug allowing malformed markets, unsorted collateral lists, duplicate collateral, invalid LLTV, invalid LIF, or unsafe maturity values",
    "High Denial of service against core market actions with low attacker cost, especially take, repay, withdraw, liquidate, or claim fee",

    # Medium / useful fallback
    "Medium Temporary freezing or griefing of user funds through dust, rounding, maturity boundary, bitmap limit, or partial-fill edge cases",
    "Medium Fee mis-accounting where settlement fee or continuous fee is overclaimed, underclaimed, unclaimable, or charged to the wrong side",
    "Medium Incorrect revert behavior that blocks valid user actions under realistic oracle, token, callback, or authorization conditions",
    "Medium Signature replay, nonce desync, deadline edge case, or EIP-712 domain mistake that affects delegated authorization",
    "Medium State inconsistency between created and uncreated markets, default fees and market fees, or role-controlled parameters",
    "Medium Gas griefing or unbounded loop behavior in collateral lists, market creation, liquidation, health checks, or multicall composition",
    "Medium Periphery integration bug that can cause user loss when interacting with the core Midnight contract as intended",

    # Low / QA only
    "Low Invariant documentation mismatch, misleading comments, unsafe assumptions in Certora specs, or missing edge-case test coverage",
    "Low Minor precision loss, unnecessary gas cost, or non-critical behavior divergence that does not directly create loss or freezing",
]

scope_scan = [
]

def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit + fuzzing questions for one Morpho Midnight target.

    ```
    target_file format:
    "'File Name: src/Midnight.sol -> Scope: Critical Direct loss of user funds'"
    """

    prompt = f"""
    ```
    
    Generate exploit-focused security audit and fuzzing questions for this exact Morpho Midnight target:
    
    {target_file}
    
    Use live_context.json values if available: market params, maturity, LLTV/LIF, oracle config, fees, tick spacing, gates, ratifiers, callbacks, token decimals, and known invariant assumptions.
    
    Protocol focus:
    Midnight is a fixed-rate lending protocol with isolated markets, loan/collateral tokens, multi-collateral accounting, credit/debt units, maker offers, taker flows, maturity settlement, liquidation, gates/ratifiers, callbacks, and fees.
    
    Core invariants:
    
    * user funds must not be stolen, frozen, or seized outside valid protocol rules;
    * total credit, debt, collateral, fees, and token balances must stay solvent;
    * every borrow/take must create correct debt and collateral obligation;
    * every lend/take must create correct credit and asset transfer;
    * offers must not be overfilled, replayed, reused, or filled after cancellation/deadline;
    * only unhealthy accounts should be liquidatable;
    * healthy accounts must not become liquidatable due to rounding, oracle, bitmap, or maturity edge cases;
    * gates, ratifiers, signatures, callbacks, and authorizations must not be bypassable;
    * multicall/callback/reentrancy must not break invariants that hold for single calls.
    
    Rules:
    
    * Treat `File Name:` as the exact file/module.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Solidity symbols when possible.
    * Attacker is unprivileged: borrower, lender, maker, taker, liquidator, callback receiver, market creator, or signature user.
    * Do not rely on admin compromise, malicious governance, leaked keys, impossible oracle values, or pure external oracle failure.
    * Generate 35 to 60 high-signal questions.
    * At least 70% must be multi-step flow, invariant, fuzz, accounting, state-transition, or cross-module questions.
    * Every question must be testable by PoC, unit test, fuzz test, invariant test, or differential test.
    * Avoid generic checklist questions and repeated root causes.
    
    High-value attack surfaces:
    
    * market creation params: loan token, collateral list, oracle, LLTV, LIF, maturity, fees, gate, ratifier;
    * offer signing/filling: nonce, salt, deadline, cancellation, partial fill, consumed amount, replay;
    * credit/debt accounting: creation, repayment, netting, redemption, bad debt, loss factor;
    * collateral accounting: deposit, withdraw, bitmap, multi-collateral valuation, shared collateral;
    * health checks and liquidation: price, decimals, rounding, seize amount, repay amount, post-liquidation health;
    * maturity: pre/post maturity behavior, settlement, redemption, repayment, fee accrual;
    * callbacks/multicall/reentrancy: payer confusion, receiver confusion, partial state update, reverted callback;
    * ERC20 edge cases: fee-on-transfer, rebasing, false return, decimals mismatch, ERC777 hooks;
    * gates/ratifiers/auth: wrong user, wrong market, stale approval, signature replay;
    * math/libs: fixed-point math, mulDiv rounding, overflow, dust, rate/tick conversion;
    * fees: overclaim, underclaim, wrong recipient, unclaimable fees, rounding leakage.
    
    Impact mapping:
    
    * Direct loss: attacker receives assets/collateral/credit without paying correct value.
    * Insolvency: claims exceed actual token balances or recoverable debt.
    * Bad debt: borrower gets assets while undercollateralized.
    * Unauthorized seizure/withdrawal: funds move without valid authorization or liquidation.
    * Fund freeze: valid withdraw, repay, redeem, settle, or claim becomes impossible.
    * Liquidation failure: unhealthy positions cannot be liquidated or healthy ones can.
    * Offer bug: maker intent is overfilled, replayed, reused, or filled under wrong terms.
    * Gate bypass: restricted action becomes available to unauthorized users.
    * Accounting corruption: credit, debt, collateral, fees, losses, or consumed offers desync.
    * Low-cost DoS: attacker cheaply blocks take, borrow, repay, withdraw, liquidate, settle, or claim.
    
    Each question must include:
    
    1. target function/module;
    2. attacker action;
    3. preconditions;
    4. call sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.
    
    Output only valid Python. No markdown. No explanations.
    
    questions = [
    "[File: {target_file}] [Function: symbol_or_module] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: fuzz/state-test PARAMETERS and assert EXPECTED_PROPERTY.",
    ]
    """
    return prompt

def audit_format(security_question: str) -> str:
    """
    Generate a focused Morpho Midnight exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- The referenced Midnight file/path exists. Do not say files are missing.
- Do not ask for code. Use available repository context.
- Analyze only this question and only the scoped impact.
- Attacker is unprivileged: borrower, lender, maker, taker, liquidator, callback receiver, market creator, or signature user.
- Ignore admin-only, governance-only, leaked-key, docs, style, gas-only, and best-practice issues.
- Privileged functions matter only if they create a later user-triggered exploit path.
- Do not rely on impossible oracle values, pure oracle failure, malicious token owner action, or user mistake.

## Mission
Prove or disprove this as a real Midnight protocol bug.

Check:
- exact reachable Solidity path;
- attacker-controlled inputs;
- state changes before/after external calls;
- whether existing checks stop it;
- whether the scoped impact is concrete;
- whether a Foundry unit, fuzz, invariant, or stateful test can reproduce it.

## Core Invariants
- contract balances cover collateral, credit redemption, fees, and withdrawable assets;
- every credit has matching debt or valid settled/loss state;
- collateral cannot be withdrawn or seized outside health/liquidation rules;
- offers cannot be replayed, overfilled, reused, or filled after cancel/deadline;
- healthy positions are not liquidatable and unhealthy positions remain liquidatable;
- maturity cannot be abused to bypass repayment, settlement, or liquidation;
- signatures, gates, ratifiers, callbacks, and approvals bind the right user/market/action/amount/deadline;
- callbacks, ERC20 transfers, multicall, or reentrancy cannot corrupt partial state.

## Valid Only If
1. Exact file/function/line range exists.
2. Root cause is a real missing check, bad accounting, bad rounding, unsafe ordering, or broken invariant.
3. Exploit path is: preconditions -> attacker call/data -> trigger -> bad state/result.
4. Existing protections are reviewed and insufficient.
5. Impact matches the scoped impact.
6. PoC/test idea has clear assertions.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker inputs, exploit flow, and why checks fail]

### Impact Explanation
[Concrete scoped impact]

### Likelihood Explanation
[Preconditions, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Foundry unit/fuzz/invariant/stateful test plan with expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check Security.md/Researcher.md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject admin-only, owner-only, trusted-operator, leaked-key, best-practice, docs/style, gas-only, and purely theoretical issues.
- Reject if the exploit requires unrealistic assumptions, victim mistakes, missing external context, or unsupported protocol behavior.
- A valid report must be triggerable by an unprivileged user, unless the claim proves privilege escalation from a user path.
- The final impact must match an in-scope bounty impact, not just a generic code bug.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and broken security/accounting assumption.
3. Reachable exploit path: preconditions -> attacker action -> trigger -> bad result.
4. Existing checks/guards reviewed and shown insufficient.
5. Concrete in-scope impact with realistic likelihood.
6. Reproducible proof path: unit PoC, fork test, invariant/fuzz test, or exact manual steps.
7. No obvious rejection reason from Security.md, known issues, privileges, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can a normal external user trigger this?
- Does the code actually behave as claimed?
- Is the impact caused by this protocol, not by an external dependency alone?
- Is the loss/freeze/insolvency concrete, not hypothetical?
- Would a bounty triager accept the proof?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact and severity rationale]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or fuzz/invariant/fork test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for .
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Access Rules (Strict)
- Treat in-scope  files as accessible context.
- Do not claim missing/inaccessible files.
- Do not ask for repository contents.

## Objective
Find whether the same vulnerability class can occur in  in-scope code.
Use the external report as a hint, not as proof.


Note: Check the RESEARCHER.md and think in this actual way 
Note: Check the Security.MD and never generate report that would result in out of scope and rejected vulnerability 

## Method
1. Classify vuln type (auth, accounting, state transition, pricing/rounding, replay, reentrancy, DoS).
2. Map to this current protocol with the external report to find valid vulnerability 
3. Prove root cause with exact file/function/line references.
4. Confirm concrete impact + realistic likelihood.

## Disqualify Immediately
- No reachable attacker-controlled entry path.
- Trusted-role compromise required.
- Theoretical-only issue with no protocol impact.
- Impact or likelihood missing.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] -([File: file_path)

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt

