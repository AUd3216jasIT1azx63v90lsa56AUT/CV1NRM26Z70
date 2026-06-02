Audit Report

## Title
Fee-on-Transfer Collateral Token Enables Phantom Collateral Accounting and Undercollateralized Borrowing - ([File: src/Midnight.sol])

## Summary
`supplyCollateral` in `src/Midnight.sol` commits `_position.collateral[collateralIndex]` using the caller-supplied `assets` value at line 533 before executing the ERC-20 transfer at line 545. `SafeTransferLib.safeTransferFrom` validates only that `transferFrom` does not revert and returns `true`, with no balance-before/after guard. A fee-on-transfer collateral token satisfies both checks while delivering fewer (or zero) tokens, leaving the protocol with phantom accounting that `isHealthy` treats as real collateral, enabling undercollateralized borrowing and direct theft of lender funds.

## Finding Description
**Root cause — checks-effects-interactions ordering with no received-amount guard:**

`supplyCollateral` (`src/Midnight.sol` lines 524–546):
- Line 533 writes `_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + assets)` using the caller-supplied `assets` before any transfer occurs.
- Lines 535–541 set the bitmap bit and enforce `MAX_COLLATERALS_PER_BORROWER`, also before the transfer.
- Line 545 is the actual transfer: `SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets)`.

`SafeTransferLib.safeTransferFrom` (`src/libraries/SafeTransferLib.sol` lines 24–34):
- Calls `token.transferFrom(from, to, value)`, checks `success == true`, and decodes the return value as `bool`.
- No balance snapshot is taken before or after. A fee-on-transfer token with a 100% fee satisfies both checks while delivering 0 tokens.

`isHealthy` (`src/Midnight.sol` lines 944–960):
- Iterates the bitmap, reads `_position.collateral[i]` (the phantom value), and computes `maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE).mulDivDown(collateralParam.lltv, WAD)`.
- This produces a positive `maxDebt` from collateral that was never received, allowing the borrower to pass health checks and take debt.

**Exploit flow:**
1. Attacker deploys a fee-on-transfer ERC-20 token (fee = 100%) and creates a permissionless market (`touchMarket` is `public`) with it as the collateral token.
2. Lenders supply real loan tokens to the market (permissionless market, no token behavior validation in `touchMarket`).
3. Attacker calls `supplyCollateral(market, collateralIndex, assets, attacker)` with `assets > 0`.
4. Accounting and bitmap are committed; `transferFrom` succeeds (returns `true`) but delivers 0 tokens.
5. `_position.collateral[collateralIndex] == assets`, `token.balanceOf(address(this))` unchanged.
6. Attacker calls a borrow path; `isHealthy` computes positive `maxDebt` from the phantom value and approves the borrow.
7. Attacker receives real loan tokens backed by zero real collateral.

**Why existing checks fail:**
- `SafeTransferLib` has no received-amount guard (lines 24–34).
- Accounting is unconditionally committed before the external call (line 533 before line 545).
- `touchMarket` validates LLTV tiers, sorted collateral addresses, and maxLif, but imposes no constraint on token transfer behavior (lines 755–791).
- Certora formal verification explicitly assumes well-behaved ERC-20 tokens (`certora/README.md` line 112), so `Solvency.spec` invariants are not proved against fee-on-transfer tokens.

## Impact Explanation
Direct theft of lender funds and protocol insolvency. `_position.collateral[collateralIndex]` records `assets` while the contract holds 0 tokens. `isHealthy` computes a positive `maxDebt` from this phantom value, allowing the borrower to take real loan-token debt with no real backing. When the phantom debt cannot be repaid, lenders bear the loss. This directly violates the core invariants stated in `live_context.json`: "ERC20 transfer deltas must match accounting deltas" and "collateral bitmap/list state must match actual deposited collateral." The impact class matches "protocol insolvency," "bad debt creation," and "direct loss of user funds" — all listed as highest-priority bug classes.

## Likelihood Explanation
Market creation is permissionless (`touchMarket` is `public`). An attacker can deploy a fee-on-transfer token and create a market with no admin involvement. `supplyCollateral` requires only `onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender]`, which the attacker satisfies trivially. Fee-on-transfer tokens with configurable tax rates exist on mainnet; a 100% fee is an extreme but deployable configuration. Any fee > 0 causes partial accounting inflation. The attack is repeatable across collateral indices and markets. `live_context.json` explicitly lists "token charges fee" as a recommended fuzz axis and "fee-on-transfer" tokens as something that "should be tested if not explicitly excluded," confirming this is an in-scope attack surface. The token type is not excluded anywhere in `SECURITY.md` or the codebase.

## Recommendation
Add a received-amount guard in `supplyCollateral` by snapshotting the contract's collateral token balance before and after the transfer, and using the actual delta (not the caller-supplied `assets`) for accounting:

```solidity
uint256 balanceBefore = IERC20(collateralToken).balanceOf(address(this));
SafeTransferLib.safeTransferFrom(collateralToken, msg.sender, address(this), assets);
uint256 received = IERC20(collateralToken).balanceOf(address(this)) - balanceBefore;
_position.collateral[collateralIndex] = UtilsLib.toUint128(oldCollateral + received);
```

Move the accounting write and bitmap update to after the transfer (after computing `received`), restoring correct checks-effects-interactions ordering. Alternatively, explicitly document and enforce that fee-on-transfer tokens are not supported as collateral (e.g., via a token allowlist or a revert if `received != assets`).

## Proof of Concept
**Minimal Foundry test:**

```solidity
// FeeOnTransferToken: transferFrom takes 100% fee, returns true
// 1. Deploy FeeOnTransferToken as collateralToken
// 2. Create market with FeeOnTransferToken as collateral, loanToken as loan
// 3. Lender supplies N loanTokens to the market
// 4. Attacker calls supplyCollateral(market, 0, 1e18, attacker)
//    - _position.collateral[0] == 1e18
//    - FeeOnTransferToken.balanceOf(midnight) == 0
// 5. Attacker borrows via take(); isHealthy returns true (phantom maxDebt > 0)
// 6. Assert: attacker received real loanTokens; midnight holds 0 collateral
// 7. Assert: core invariant violated: accounting delta (1e18) != transfer delta (0)
```

The test proves the invariant "ERC20 transfer deltas must match accounting deltas" is broken with a concrete, no-privilege exploit path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** src/Midnight.sol (L524-546)
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
    }
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

**File:** src/Midnight.sol (L944-960)
```text
    function isHealthy(Market memory market, bytes32 id, address borrower) public view returns (bool) {
        Position storage _position = position[id][borrower];
        uint256 debt = _position.debt;
        uint256 maxDebt;
        if (debt > 0) {
            uint128 _collateralBitmap = _position.collateralBitmap;
            while (_collateralBitmap != 0) {
                uint256 i = UtilsLib.msb(_collateralBitmap);
                CollateralParams memory collateralParam = market.collateralParams[i];
                uint256 price = IOracle(collateralParam.oracle).price();
                maxDebt += _position.collateral[i].mulDivDown(price, ORACLE_PRICE_SCALE)
                    .mulDivDown(collateralParam.lltv, WAD);
                _collateralBitmap = _collateralBitmap.clearBit(i);
            }
        }
        return maxDebt >= debt;
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

**File:** certora/README.md (L112-112)
```markdown
- ERC20 tokens are assumed well-behaved, see the comments in the respective files for more detail.
```

**File:** live_context.json (L232-234)
```json
      "ERC20 transfer deltas must match accounting deltas",
      "fee-on-transfer, rebasing, false-return, ERC777-like hooks, and non-standard decimals should be tested if not explicitly excluded",
      "multicall must not bypass per-action invariants"
```
