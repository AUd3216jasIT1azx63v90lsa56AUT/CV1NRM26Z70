import json
import os

# One indexed repository: the Confidence Pools contest repository.
MAX_REPO = 10
SOURCE_REPO = "incjanta/confidence-pool"
REPO_NAME = "confidence-pool"
run_number = os.environ.get("GITHUB_RUN_NUMBER", "0")


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index."""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


# repositories.json can contain an earlier project's worker mirrors. This audit has one
# canonical DeepWiki target, so do not rotate into stale repositories on CI runs.
BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"


# CodeHawks contest scope. BattleChain interfaces and every other file are context only.
scope_files = [
    "src/ConfidencePool.sol",
    "src/ConfidencePoolFactory.sol",
]


target_scopes = [
    # Critical / High: loss, insolvency, theft, or permanent lock
    "Critical theft or unrecoverable loss of staker principal, bonus funds, or attacker bounty through broken pool accounting or settlement",
    "Critical protocol insolvency where the pool's ERC20 balance cannot cover remaining stake, bonus, bounty, or corrupted-path liabilities",
    "Critical unauthorized claim, withdrawal, sweep, or bounty payment to an unentitled address",
    "Critical clone initialization or factory upgrade flaw allowing ownership takeover, malicious implementation use, or theft from newly created pools",
    "Critical outcome-state confusion allowing the same value to be paid twice or principal to be routed to the wrong resolution beneficiary",
    "High permanent freezing of staker principal, earned bonus, recovery funds, or a valid whitehat bounty",
    "High reentrancy or hostile ERC20 interaction that corrupts stake, bonus, snapshot, reserve, or claim accounting",
    "High factory deterministic-clone or per-agreement indexing flaw causing address collision, incorrect initialization, or pool substitution",
    "High scope validation or scope-lock bypass that changes a pool's committed covered accounts after risk begins",
    "High expiry mutation, timestamp boundary, or truncation flaw that changes promised withdrawal, staking, resolution, or bounty windows",
    "High registry-state observation flaw that incorrectly opens withdrawals, closes deposits, seals risk timestamps, or selects a resolution branch",
    "High moderator re-flag or claimsStarted finality flaw that permits conflicting outcomes after economically meaningful value movement",
    "High SURVIVED or EXPIRED claim accounting flaw that overpays one staker, under-reserves others, or makes claims order-dependent",
    "High k=2 time-weighted bonus math flaw that lets an attacker extract materially more bonus by splitting, topping up, timing, or manipulating observation",
    "High CORRUPTED good-faith bounty flaw allowing the wrong attacker, repeated claims, renewed deadlines, or excess entitlement",
    "High CORRUPTED bad-faith or post-window sweep flaw that strands funds or sends more than the intended available balance",
    "High sweepUnclaimedBonus reserve flaw allowing principal or another staker's earned bonus to be swept",
    "High withdrawal accounting flaw allowing risk escape after active risk or leaving stale score weight that steals later bonus",
    "High donation, balance-difference, fee-on-transfer, rebasing, or token-upgrade interaction that creates exploitable liability mismatch despite the allowlist model",
    "High pause or access-control bypass affecting pool creation, staking, bonus contribution, settlement, withdrawal, claims, sweeps, or UUPS upgrades",
    "High denial of service against a core withdrawal, resolution, claim, bounty, or sweep path at low attacker cost",

    # Medium: bounded loss, griefing, and meaningful correctness failures
    "Medium rounding or precision error in quadratic scoring, snapshot math, reserve math, or proportional payout causing repeatable value leakage",
    "Medium multi-staker or multi-deposit ordering flaw where equivalent economic positions receive inconsistent payouts",
    "Medium edge-state transition bug across NOT_DEPLOYED, NEW_DEPLOYMENT, ATTACK_REQUESTED, UNDER_ATTACK, PROMOTION_REQUESTED, PRODUCTION, and CORRUPTED",
    "Medium incorrect no-observed-risk or zero-global-score branch behavior outside the explicitly accepted design",
    "Medium post-resolution donation or repeated-sweep behavior causing bounded fund lock, griefing, or incorrect recipient accounting",
    "Medium factory configuration or allowlist lifecycle flaw that creates realistically unusable or misconfigured pools without requiring owner compromise",
    "Medium gas or array-size denial of service in pool creation, scope replacement, scope validation, or pool enumeration",
    "Medium event, getter, snapshot, or bookkeeping inconsistency that breaks a security-critical integration or prevents correct settlement",

    # Low / QA fallback
    "Low non-critical invariant mismatch or missing edge-case coverage in the two in-scope contracts",
]

scope_scan = []


PROJECT_CONTEXT = """
Confidence Pools is an ERC20 staking and bonus-settlement protocol for BattleChain Safe Harbor
agreements. ConfidencePoolFactory is UUPS-upgradeable and deploys non-upgradeable deterministic
ConfidencePool clones. Each clone holds all stake and bonus assets, commits to a BattleChain-only
account scope, observes registry state live, and resolves through SURVIVED, EXPIRED, or CORRUPTED.

Contest scope is exactly src/ConfidencePool.sol and src/ConfidencePoolFactory.sol. BattleChain
IAgreement, IAttackRegistry, IBattleChainSafeHarborRegistry, OpenZeppelin, tests, mocks, and scripts
are dependencies/context, not reportable targets. Solidity 0.8.26, via-IR, optimizer 200. Standard
ERC20 tokens only; fee-on-transfer and rebasing tokens are unsupported, though the scoped code's
defenses and allowlist assumptions must still be evaluated for an in-scope exploit.

Actors: unprivileged staker, bonus contributor, pool sponsor/agreement owner, named whitehat
attacker, permissionless keeper/caller, trusted moderator/DAO, and trusted factory owner. Do not
report trusted owner/moderator/registry malice by itself. Privileged behavior matters only when an
unprivileged caller can exploit a scoped implementation defect under honest configuration.

Useful live dependency snapshot from fork tests: BattleChain testnet chainId 627;
SafeHarborRegistry 0x0a652e265336a0296816aC4D8400880e3E537C24; AttackRegistry
0xdD029a6374095EEb4c47a2364Ce1D0f47f007350; AgreementFactory
0x2Bee2970f10FDc2aeA28662BB6F6A501278Ebd46; demo Agreement
0xE550894617Ac4C1bbc019C2AA5D47495a0F07716. Use these only to verify interface/state assumptions;
the dependency contracts remain out of scope and no deployed ConfidencePool address is supplied.
"""


ACCEPTED_BEHAVIORS = """
Before accepting a finding, check docs/DESIGN.md and reject findings that merely restate these
documented decisions: UNDER_ATTACK and PROMOTION_REQUESTED mean legally attackable, not breached;
EXPIRED resolution during an active-risk state is intended; stake during UNDER_ATTACK is intended;
re-flagging ends on first value-moving claim; no observed risk pays zero bonus; the documented
no-risk-window CORRUPTED race is accepted; the 180-day scope-blind auto-CORRUPTED backstop is an
accepted trust trade-off; first-observed risk timestamps and the globalScore==0 amount-weighted
fallback are intended; pool-local scope does not gate moderator judgement; pre-risk withdrawal and
its one-way closure are intended; the registry singleton and moderator are trusted; good-faith
CORRUPTED reserves the whole snapshot for the named attacker; auto-resolution is registry-selected;
repeatable sweeps intentionally recover later donations. A report is valid only if it finds a
distinct implementation defect or proves the documented behavior violates the stated guarantees
in a materially different, unacknowledged way.
"""


PROTOCOL_DOCUMENT_RULES = """
Mandatory protocol context: read README.md, protocol-readme.md, and docs/DESIGN.md before doing
this task. Use README.md for the official contest scope, actors, compatibility, and stated protocol
behavior. Use protocol-readme.md for architecture, lifecycle, registry-state semantics, resolution
paths, economic formulas, trust assumptions, and operational parameters. Treat docs/DESIGN.md as
the authoritative source for intentional behavior, accepted trade-offs, rejected alternatives, and
known false positives. Reconcile every question or claim against all three documents and the live
Solidity implementation. The code determines actual behavior, but documented intentional behavior
must not be reported as a vulnerability unless a distinct implementation defect violates the
documented guarantee. Do not proceed from generic DeFi assumptions or enum names alone.
"""


def question_generator(target_file: str) -> str:
    """Generate exploit-focused questions for one Confidence Pools target and impact."""
    prompt = f"""
Generate exploit-focused security audit and Foundry fuzz/invariant questions for this exact
BattleChain Confidence Pools target:

{target_file}

{PROJECT_CONTEXT}

{PROTOCOL_DOCUMENT_RULES}

{ACCEPTED_BEHAVIORS}

Treat `File Name:` as the exact scoped file and `Scope:` as the only impact to target. Assume the
full repository, interfaces, and tests are available.
Use live_context.json if present, but never invent deployments or balances. Do not ask for code.

Core invariants:
- pool token balance covers every remaining principal, bonus, bounty, and corrupted reserve;
- no principal or bonus is paid, swept, or claimed twice;
- totalEligibleStake and score accumulators exactly track eligible users through stake/withdraw;
- snapshots freeze the correct liabilities and claim order cannot change aggregate entitlement;
- k=2 shares are bounded by snapshotTotalBonus and economically equivalent deposits behave consistently;
- only valid registry states and timing permit each deposit, withdrawal, outcome, claim, and sweep;
- scope and expiry become immutable at their documented one-way boundaries;
- claimsStarted and corrupted deadlines cannot be reset or bypassed through re-flagging/donations;
- factory clones initialize once with the intended agreement, token, registry, moderator, owner, and scope;
- factory upgrades/configuration cannot be reached by unprivileged users.

Prioritize cross-function sequences involving stake, contributeBonus, withdraw, pokeRiskWindow,
setScope, setExpiry, flagOutcome, claimSurvived, claimExpired, claimAttackerBounty, claimCorrupted,
sweepUnclaimedBonus, sweepUnclaimedCorrupted, createPool, allowlisting, pausing, and upgrades. Test
timestamp boundaries, registry transitions, multiple users/deposits, re-flags, donations, partial or
abnormal ERC20 transfers, rounding, zero/dust values, and call-order permutations.

Rules:
- attacker is unprivileged unless the scoped impact specifically concerns an access-control bypass;
- do not rely on malicious trusted roles, leaked keys, unsupported tokens alone, dependency bugs,
  impossible registry states, user mistakes, or an explicitly accepted design trade-off;
- generate 35 to 60 non-duplicate, high-signal questions;
- at least 75% must be multi-step state-transition, accounting, invariant, fuzz, or cross-contract questions;
- every question must identify the exact symbol, attacker action, preconditions, call sequence,
  violated invariant, scoped impact, existing guard to challenge, and a concrete Foundry proof idea;
- output only valid Python, with no markdown or explanations.

questions = [
    "[File: file_path] [Function: exact_symbol] Can an unprivileged ATTACKER under PRECONDITIONS execute CALL_SEQUENCE, bypassing EXISTING_GUARD and violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: Foundry unit/fuzz/invariant test with PARAMETERS and EXPECTED_ASSERTIONS.",
]
"""
    return prompt


def audit_format(security_question: str) -> str:
    """Generate a focused Confidence Pools exploit-validation prompt."""
    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Project Context
{PROJECT_CONTEXT}

## Mandatory Protocol Documents
{PROTOCOL_DOCUMENT_RULES}

## Accepted-Behavior Filter
{ACCEPTED_BEHAVIORS}

## Mission
Prove or disprove only this question against the live scoped implementation. Trace exact reachable
Solidity paths, attacker-controlled inputs, registry/timestamp prerequisites, state and token-balance
changes, external-call ordering, and every existing guard. Use tests and docs as context, not as proof
that the implementation is correct. Reject dependency-only and out-of-scope root causes.

## Valid Only If
1. Root cause is in one of the two scoped contracts and exact functions/lines exist.
2. A realistic actor can reach preconditions without trusted-role compromise or victim mistakes.
3. The sequence produces a concrete scoped loss, insolvency, permanent/meaningful freeze, theft,
   access-control failure, or other stated impact.
4. docs/DESIGN.md does not explicitly accept the behavior, or the proof demonstrates a distinct bug.
5. Existing checks are shown insufficient and a Foundry unit/fuzz/invariant/fork PoC has decisive assertions.

## Output
If valid, output exactly:

### Title
[Bug and impact] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Exact root cause, reachable sequence, state/accounting changes, and failed guards]

### Impact Explanation
[Concrete scoped impact]

### Likelihood Explanation
[Actor, prerequisites, feasibility, repeatability]

### Recommendation
[Specific scoped fix]

### Proof of Concept
[Runnable Foundry test plan/code and exact assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """Generate strict contest-style validation for Confidence Pools claims."""
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Project Context
{PROJECT_CONTEXT}

## Mandatory Protocol Documents
{PROTOCOL_DOCUMENT_RULES}

## Accepted-Behavior Filter
{ACCEPTED_BEHAVIORS}

Validate only the submitted claim. Do not create a replacement finding. Require an exact root cause
inside src/ConfidencePool.sol or src/ConfidencePoolFactory.sol, a reachable attacker sequence,
review of all guards and docs/DESIGN.md, concrete impact, realistic likelihood, and a reproducible
Foundry proof. Reject trusted-role malice, dependency-only faults, unsupported-token behavior by
itself, stale-interface speculation without live evidence, gas/style/docs issues, and theoretical
accounting differences without loss or meaningful denial of service. Do not upgrade severity beyond
the evidence. Prefer #NoVulnerability over speculation.

If valid, output exactly:

Audit Report

## Title
[Clear vulnerability and impact] - ([File: file_path])

## Summary
[2-3 sentences]

## Finding Description
[Exact code path, root cause, exploit flow, balance/state deltas, and failed guards]

## Impact Explanation
[Concrete contest-scoped impact and severity]

## Likelihood Explanation
[Attacker capability, prerequisites, feasibility, repeatability]

## Recommendation
[Specific fix]

## Proof of Concept
[Runnable Foundry unit/fuzz/invariant/fork test and expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one outcome. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """Generate a cross-project analog scan prompt for Confidence Pools."""
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Current Project
{PROJECT_CONTEXT}

## Mandatory Protocol Documents
{PROTOCOL_DOCUMENT_RULES}

## Accepted-Behavior Filter
{ACCEPTED_BEHAVIORS}

Use the external report only as a vulnerability-class hint. Map its root cause to exact reachable
code in the two scoped contracts, then independently prove attacker control, failed guards, and
concrete impact. Reject loose thematic similarity, trusted-role/dependency-only issues, and accepted
design behavior. Do not claim files are inaccessible and do not ask for code.

If a valid analog exists, output exactly:

### Title
[Clear vulnerability and impact] - ([File: file_path])

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
