#!/usr/bin/env python3
"""
Live protocol context scanner.

Usage examples:
  python live_context_scanner.py --from-questions --protocol OSTIUM --out live_context.json
  python live_context_scanner.py --url https://arbiscan.io/address/0x... --url https://etherscan.io/address/0x...
  python live_context_scanner.py --scope-file scope_urls.json

Expected scope-file formats:
  1) JSON list of explorer URLs or addresses
  2) plain text file (one URL/address per line)
"""

from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests


# EIP-1967 slots
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
EIP1967_BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"

# Common event/topic names
TRANSFER_EVENT_SIG = "Transfer(address,address,uint256)"
TRANSFER_SINGLE_EVENT_SIG = "TransferSingle(address,address,address,uint256,uint256)"
TRANSFER_BATCH_EVENT_SIG = "TransferBatch(address,address,address,uint256[],uint256[])"


@dataclass(frozen=True)
class ChainConfig:
    chain: str
    chain_id: int
    native_symbol: str
    rpc_endpoints: Tuple[str, ...]


CHAIN_CONFIGS: Dict[str, ChainConfig] = {
    "ethereum": ChainConfig(
        chain="ethereum",
        chain_id=1,
        native_symbol="ETH",
        rpc_endpoints=("https://ethereum-rpc.publicnode.com", "https://ethereum.publicnode.com"),
    ),
    "arbitrum": ChainConfig(
        chain="arbitrum",
        chain_id=42161,
        native_symbol="ETH",
        rpc_endpoints=("https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"),
    ),
    "base": ChainConfig(
        chain="base",
        chain_id=8453,
        native_symbol="ETH",
        rpc_endpoints=("https://base-rpc.publicnode.com", "https://mainnet.base.org"),
    ),
    "optimism": ChainConfig(
        chain="optimism",
        chain_id=10,
        native_symbol="ETH",
        rpc_endpoints=("https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"),
    ),
}


EXPLORER_TO_CHAIN = {
    "etherscan.io": "ethereum",
    "arbiscan.io": "arbitrum",
    "basescan.org": "base",
    "optimistic.etherscan.io": "optimism",
}


COMMON_TOKENS: Dict[str, List[Dict[str, Any]]] = {
    "ethereum": [
        {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
        {"symbol": "USDT", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
        {"symbol": "DAI", "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "decimals": 18},
        {"symbol": "WETH", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": 18},
    ],
    "arbitrum": [
        {"symbol": "USDC", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
        {"symbol": "USDT", "address": "0xfd086bC7CD5C481DCC9C85ebe478A1C0b69FCbb9", "decimals": 6},
        {"symbol": "DAI", "address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18},
        {"symbol": "WETH", "address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
    ],
    "base": [
        {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
        {"symbol": "DAI", "address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
        {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    ],
    "optimism": [
        {"symbol": "USDC", "address": "0x7F5c764cBc14f9669B88837ca1490cCa17c31607", "decimals": 6},
        {"symbol": "USDT", "address": "0x94b008aA00579c1307B0EF2c499aD98a8Ce58e58", "decimals": 6},
        {"symbol": "DAI", "address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18},
        {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    ],
}


ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
UINT_TYPE_RE = re.compile(r"^uint([0-9]{0,3})$")
INT_TYPE_RE = re.compile(r"^int([0-9]{0,3})$")
BYTESN_TYPE_RE = re.compile(r"^bytes([1-9]|[12][0-9]|3[0-2])$")
IDENTITY_CACHE: Dict[Tuple[int, str], Dict[str, Any]] = {}


def now_iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def addr_norm(address: str) -> str:
    return "0x" + address[2:].lower()


def is_address(value: Any) -> bool:
    return isinstance(value, str) and bool(ADDRESS_RE.fullmatch(value))


def hex_to_int(value: Optional[str]) -> Optional[int]:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def word_to_address(word_hex: str) -> Optional[str]:
    if not isinstance(word_hex, str) or not word_hex.startswith("0x"):
        return None
    v = hex_to_int(word_hex)
    if v is None or v == 0:
        return None
    return "0x" + word_hex[-40:].lower()


def pad_address_topic(address: str) -> str:
    return "0x" + ("0" * 24) + address[2:].lower()


def chunks(data: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


def safe_decimal_ratio(num: int, den: int) -> Optional[float]:
    if den == 0:
        return None
    return float(Decimal(num) / Decimal(den))


def decode_int_256(word: str) -> Optional[int]:
    value = hex_to_int(word)
    if value is None:
        return None
    if value >= 2**255:
        value -= 2**256
    return value


def parse_domain_chain(host: str) -> Optional[str]:
    h = host.lower().strip()
    if h in EXPLORER_TO_CHAIN:
        return EXPLORER_TO_CHAIN[h]
    if h.endswith(".etherscan.io"):
        return "ethereum"
    if h.endswith(".arbiscan.io"):
        return "arbitrum"
    if h.endswith(".basescan.org"):
        return "base"
    return None


def parse_scope_item(item: str) -> Tuple[Optional[str], Optional[str]]:
    item = item.strip()
    if not item:
        return None, None

    if ADDRESS_RE.fullmatch(item):
        # Unknown chain when passed as raw address
        return None, addr_norm(item)

    if item.startswith("http://") or item.startswith("https://"):
        parsed = urlparse(item)
        host = parsed.netloc.lower()
        chain = parse_domain_chain(host)
        match = ADDRESS_RE.search(item)
        if match:
            return chain, addr_norm(match.group(0))
    return None, None


class RpcClient:
    def __init__(self, endpoints: Tuple[str, ...], timeout: int = 10, retries: int = 2):
        self.endpoints = endpoints
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self._selector_cache: Dict[str, str] = {}

    def _post(self, endpoint: str, payload: Any) -> requests.Response:
        return self.session.post(endpoint, json=payload, timeout=self.timeout)

    def _request(self, payload: Any) -> Any:
        last_error = None
        for _ in range(self.retries):
            for endpoint in self.endpoints:
                try:
                    response = self._post(endpoint, payload)
                    response.raise_for_status()
                    data = response.json()
                    return data
                except Exception as exc:
                    last_error = exc
            time.sleep(0.2)
        raise RuntimeError(f"RPC request failed after retries: {last_error}")

    def call(self, method: str, params: List[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        result = self._request(payload)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(f"RPC error for {method}: {result['error']}")
        return result.get("result")

    def batch_call(self, entries: List[Tuple[str, List[Any]]]) -> List[Any]:
        payload = []
        for idx, (method, params) in enumerate(entries, 1):
            payload.append({"jsonrpc": "2.0", "id": idx, "method": method, "params": params})
        result = self._request(payload)
        if not isinstance(result, list):
            raise RuntimeError(f"Invalid batch RPC response type: {type(result)}")
        by_id = {item.get("id"): item for item in result if isinstance(item, dict)}
        ordered = []
        for idx in range(1, len(entries) + 1):
            item = by_id.get(idx, {})
            if "error" in item:
                ordered.append({"error": item["error"]})
            else:
                ordered.append(item.get("result"))
        return ordered

    def selector(self, signature: str) -> str:
        if signature in self._selector_cache:
            return self._selector_cache[signature]
        input_hex = "0x" + signature.encode("utf-8").hex()
        try:
            digest = self.call("web3_sha3", [input_hex])
            selector = digest[:10]
            self._selector_cache[signature] = selector
            return selector
        except Exception as exc:
            raise RuntimeError(f"Cannot compute selector for {signature}: {exc}")

    def eth_call(self, to: str, data: str, block: str = "latest") -> Optional[str]:
        try:
            return self.call("eth_call", [{"to": to, "data": data}, block])
        except Exception:
            return None


def decode_single_output(raw_hex: Optional[str], abi_type: str) -> Any:
    if raw_hex in (None, "0x"):
        return None
    if not isinstance(raw_hex, str) or not raw_hex.startswith("0x"):
        return raw_hex
    data = raw_hex[2:]

    if abi_type == "address":
        return "0x" + data[-40:].lower()
    if abi_type == "bool":
        return bool(int(data, 16))
    if UINT_TYPE_RE.fullmatch(abi_type):
        return int(data, 16)
    if INT_TYPE_RE.fullmatch(abi_type):
        bits = 256
        if abi_type != "int":
            try:
                bits = int(abi_type[3:])
            except ValueError:
                bits = 256
        n = int(data, 16)
        if n >= 2 ** (bits - 1):
            n -= 2**bits
        return n
    if abi_type == "bytes32":
        return "0x" + data[:64]
    if BYTESN_TYPE_RE.fullmatch(abi_type):
        # bytesN
        try:
            size = int(abi_type[5:])
            return "0x" + data[: size * 2]
        except Exception:
            return raw_hex
    if abi_type == "string":
        # ABI dynamic string encoding
        if len(data) < 128:
            # bytes32-like fallback
            try:
                return bytes.fromhex(data).rstrip(b"\x00").decode("utf-8", errors="ignore")
            except Exception:
                return raw_hex
        try:
            offset = int(data[:64], 16)
            if offset * 2 + 64 > len(data):
                return raw_hex
            length = int(data[offset * 2 : offset * 2 + 64], 16)
            start = offset * 2 + 64
            end = start + length * 2
            return bytes.fromhex(data[start:end]).decode("utf-8", errors="ignore")
        except Exception:
            return raw_hex
    if abi_type == "bytes":
        return raw_hex
    return raw_hex


def decode_symbol_or_name(raw_hex: Optional[str]) -> Optional[str]:
    if raw_hex in (None, "0x"):
        return None
    # try string decode first
    decoded = decode_single_output(raw_hex, "string")
    if isinstance(decoded, str) and decoded:
        return decoded
    # bytes32 fallback
    if isinstance(raw_hex, str) and raw_hex.startswith("0x"):
        try:
            return bytes.fromhex(raw_hex[2:]).rstrip(b"\x00").decode("utf-8", errors="ignore") or None
        except Exception:
            return None
    return None


def abi_function_signature(fn_abi: Dict[str, Any]) -> str:
    input_types = ",".join(inp.get("type", "") for inp in fn_abi.get("inputs", []))
    return f"{fn_abi.get('name')}({input_types})"


def abi_event_signature(event_abi: Dict[str, Any]) -> str:
    input_types = ",".join(inp.get("type", "") for inp in event_abi.get("inputs", []))
    return f"{event_abi.get('name')}({input_types})"


def fetch_sourcify_metadata(chain_id: int, address: str) -> Optional[Dict[str, Any]]:
    for match_type in ("full_match", "partial_match"):
        url = f"https://repo.sourcify.dev/contracts/{match_type}/{chain_id}/{address}/metadata.json"
        try:
            resp = requests.get(url, timeout=15)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        try:
            metadata = resp.json()
        except json.JSONDecodeError:
            continue
        metadata["_sourcify_match_type"] = match_type
        return metadata
    return None


def fetch_etherscan_source(
    chain_id: int, address: str, api_key: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    if not api_key:
        return None, None
    base = "https://api.etherscan.io/v2/api"

    params_source = {
        "chainid": str(chain_id),
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
        "apikey": api_key,
    }
    params_abi = {
        "chainid": str(chain_id),
        "module": "contract",
        "action": "getabi",
        "address": address,
        "apikey": api_key,
    }
    source_payload = None
    abi_payload = None
    try:
        src_resp = requests.get(base, params=params_source, timeout=15).json()
        if src_resp.get("status") == "1" and isinstance(src_resp.get("result"), list) and src_resp["result"]:
            source_payload = src_resp["result"][0]
    except Exception:
        source_payload = None

    try:
        abi_resp = requests.get(base, params=params_abi, timeout=15).json()
        if abi_resp.get("status") == "1":
            raw_abi = abi_resp.get("result")
            if isinstance(raw_abi, str):
                abi_payload = json.loads(raw_abi)
    except Exception:
        abi_payload = None

    return source_payload, abi_payload


def extract_contract_identity(
    address: str,
    chain_id: int,
    etherscan_api_key: Optional[str],
) -> Dict[str, Any]:
    cache_key = (chain_id, addr_norm(address))
    cached = IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    identity: Dict[str, Any] = {
        "contract_name": None,
        "source_file": None,
        "compiler_version": None,
        "abi": None,
        "verification_source": None,
    }

    metadata = fetch_sourcify_metadata(chain_id, address)
    if metadata:
        ct = metadata.get("settings", {}).get("compilationTarget", {})
        if ct:
            src_file, contract_name = list(ct.items())[0]
            identity["contract_name"] = contract_name
            identity["source_file"] = src_file
        identity["compiler_version"] = metadata.get("compiler", {}).get("version")
        abi = metadata.get("output", {}).get("abi")
        if isinstance(abi, list):
            identity["abi"] = abi
        identity["verification_source"] = f"sourcify:{metadata.get('_sourcify_match_type', 'unknown')}"

    if identity["abi"] is None or identity["contract_name"] is None:
        source_payload, abi_payload = fetch_etherscan_source(chain_id, address, etherscan_api_key)
        if source_payload:
            identity["contract_name"] = identity["contract_name"] or source_payload.get("ContractName")
            identity["source_file"] = identity["source_file"] or source_payload.get("SourceFile", None)
            identity["compiler_version"] = identity["compiler_version"] or source_payload.get("CompilerVersion")
            identity["verification_source"] = identity["verification_source"] or "etherscan"
        if abi_payload and identity["abi"] is None:
            identity["abi"] = abi_payload

    IDENTITY_CACHE[cache_key] = copy.deepcopy(identity)
    return identity


def classify_proxy_type(proxy_info: Dict[str, Any]) -> str:
    if proxy_info["beacon"]:
        return "beacon"
    if proxy_info["implementation"] and proxy_info["admin"]:
        return "transparent"
    if proxy_info["implementation"] and not proxy_info["admin"]:
        return "uups | unknown"
    return "unknown"


def detect_proxy(client: RpcClient, address: str) -> Dict[str, Any]:
    implementation = word_to_address(client.call("eth_getStorageAt", [address, EIP1967_IMPLEMENTATION_SLOT, "latest"]))
    admin = word_to_address(client.call("eth_getStorageAt", [address, EIP1967_ADMIN_SLOT, "latest"]))
    beacon = word_to_address(client.call("eth_getStorageAt", [address, EIP1967_BEACON_SLOT, "latest"]))

    if beacon and not implementation:
        # For beacon proxies, resolve beacon implementation()
        selector = client.selector("implementation()")
        beacon_impl_raw = client.eth_call(beacon, selector)
        beacon_impl = decode_single_output(beacon_impl_raw, "address")
        if is_address(beacon_impl):
            implementation = addr_norm(beacon_impl)

    proxy_info = {
        "is_proxy": bool(implementation or beacon),
        "implementation": implementation,
        "admin": admin,
        "beacon": beacon,
    }
    proxy_info["proxy_type"] = classify_proxy_type(proxy_info)
    return proxy_info


def get_block_and_timestamp(client: RpcClient) -> Tuple[int, int]:
    block_hex = client.call("eth_blockNumber", [])
    block_number = int(block_hex, 16)
    block = client.call("eth_getBlockByNumber", [block_hex, False])
    timestamp = int(block["timestamp"], 16)
    return block_number, timestamp


def fetch_logs_chunked(
    client: RpcClient,
    base_filter: Dict[str, Any],
    from_block: int,
    to_block: int,
    step: int = 2_000,
    sleep_s: float = 0.0,
    max_logs: Optional[int] = None,
    newest_first: bool = False,
    max_chunk_calls: Optional[int] = None,
) -> List[Dict[str, Any]]:
    logs: List[Dict[str, Any]] = []
    if from_block > to_block:
        return logs

    calls = 0
    if newest_first:
        cur_end = to_block
        while cur_end >= from_block:
            cur_start = max(from_block, cur_end - step + 1)
            f = dict(base_filter)
            f["fromBlock"] = hex(cur_start)
            f["toBlock"] = hex(cur_end)
            try:
                part = client.call("eth_getLogs", [f]) or []
                if isinstance(part, list):
                    logs.extend(part)
                    if max_logs is not None and len(logs) >= max_logs:
                        return logs[:max_logs]
            except Exception:
                if step > 200:
                    step = max(200, step // 2)
                    continue
            calls += 1
            if max_chunk_calls is not None and calls >= max_chunk_calls:
                break
            cur_end = cur_start - 1
            if sleep_s > 0:
                time.sleep(sleep_s)
    else:
        cur = from_block
        while cur <= to_block:
            end = min(cur + step - 1, to_block)
            f = dict(base_filter)
            f["fromBlock"] = hex(cur)
            f["toBlock"] = hex(end)
            try:
                part = client.call("eth_getLogs", [f]) or []
                if isinstance(part, list):
                    logs.extend(part)
                    if max_logs is not None and len(logs) >= max_logs:
                        return logs[:max_logs]
            except Exception:
                # Shrink range on failure
                if step > 200:
                    step = max(200, step // 2)
                    continue
            calls += 1
            if max_chunk_calls is not None and calls >= max_chunk_calls:
                break
            cur = end + 1
            if sleep_s > 0:
                time.sleep(sleep_s)
    return logs


def decode_topic_word(topic: str, abi_type: str) -> Any:
    bt = base_type(abi_type)
    if bt == "address":
        return "0x" + topic[-40:].lower()
    if bt == "uint":
        return int(topic, 16)
    if bt == "int":
        return decode_int_256(topic)
    if bt == "bool":
        return bool(int(topic, 16))
    return topic


def decode_data_words(data_hex: str) -> List[str]:
    if not data_hex or data_hex == "0x":
        return []
    d = data_hex[2:]
    if len(d) % 64 != 0:
        return []
    return ["0x" + d[i : i + 64] for i in range(0, len(d), 64)]


def decode_event_sample(log: Dict[str, Any], event_abi: Dict[str, Any]) -> Dict[str, Any]:
    decoded: Dict[str, Any] = {}
    inputs = event_abi.get("inputs", [])
    topics = log.get("topics", []) or []
    data_words = decode_data_words(log.get("data", "0x"))
    topic_idx = 1  # topic0 is signature
    data_idx = 0

    for inp in inputs:
        name = inp.get("name") or f"arg_{topic_idx + data_idx}"
        typ = inp.get("type", "bytes32")
        indexed = bool(inp.get("indexed"))

        if indexed:
            if topic_idx >= len(topics):
                decoded[name] = None
            else:
                val = topics[topic_idx]
                if typ in ("string", "bytes") or typ.endswith("]") or typ.startswith("tuple"):
                    # indexed dynamic types are hashed
                    decoded[name] = val
                else:
                    decoded[name] = decode_topic_word(val, typ)
            topic_idx += 1
        else:
            if data_idx >= len(data_words):
                decoded[name] = None
            else:
                word = data_words[data_idx]
                # simple static decode only
                bt = base_type(typ)
                if bt in ("address", "bool", "bytes32", "uint", "int"):
                    decoded[name] = decode_single_output(word, typ)
                else:
                    decoded[name] = word
            data_idx += 1
    return decoded


def interesting_event(event_name: str) -> bool:
    name = event_name.lower()
    keywords = (
        "deposit",
        "withdraw",
        "request",
        "claim",
        "harvest",
        "settlement",
        "liquid",
        "pause",
        "unpause",
        "transfer",
        "upgrade",
        "reward",
        "report",
        "open",
        "close",
    )
    return any(k in name for k in keywords)


def collect_events_discovered(
    client: RpcClient,
    address: str,
    abi: Optional[List[Dict[str, Any]]],
    latest_block: int,
    sample_window_blocks: int,
    max_event_samples: int,
    max_events: int,
) -> Dict[str, Any]:
    if not abi:
        return {}
    start_block = max(0, latest_block - sample_window_blocks)
    events = [e for e in abi if e.get("type") == "event" and e.get("name")]
    events = [e for e in events if interesting_event(e["name"])][:max_events]
    discovered: Dict[str, Any] = {}

    for ev in events:
        sig = abi_event_signature(ev)
        try:
            topic0 = client.call("web3_sha3", ["0x" + sig.encode("utf-8").hex()])
        except Exception:
            continue
        logs = fetch_logs_chunked(
            client,
            base_filter={"address": address, "topics": [topic0]},
            from_block=start_block,
            to_block=latest_block,
            step=2_000,
        )
        sample_args = []
        for log in logs[:max_event_samples]:
            sample_args.append(decode_event_sample(log, ev))
        discovered[ev["name"]] = {
            "count_sampled": len(logs),
            "sample_args": sample_args,
        }
    return discovered


def infer_mappings_discovered(events_discovered: Dict[str, Any]) -> Dict[str, Any]:
    mappings: Dict[str, Any] = {}

    for event_name, payload in events_discovered.items():
        lower = event_name.lower()
        samples = payload.get("sample_args", [])
        if not isinstance(samples, list):
            continue
        if "request" in lower:
            keys = []
            for s in samples:
                for key_name in ("owner", "user", "account", "trader", "receiver"):
                    if key_name in s and is_address(s[key_name]):
                        keys.append(addr_norm(s[key_name]))
            keys = list(dict.fromkeys(keys))[:10]
            if keys:
                mappings[f"{event_name}Keys"] = {
                    "mapping_type": "heuristic_event_key_mapping",
                    "keys_source": f"{event_name} events",
                    "sample_keys": keys,
                    "sample_values": {},
                }
        if "open" in lower or "position" in lower:
            keys = []
            for s in samples:
                for key_name in ("id", "positionId", "orderId", "tradeId", "tokenId"):
                    if key_name in s:
                        keys.append(str(s[key_name]))
            keys = list(dict.fromkeys(keys))[:10]
            if keys:
                mappings[f"{event_name}Ids"] = {
                    "mapping_type": "heuristic_event_id_mapping",
                    "keys_source": f"{event_name} events",
                    "sample_keys": keys,
                    "sample_values": {},
                }
    return mappings


def collect_zero_arg_views(
    client: RpcClient,
    address: str,
    abi: Optional[List[Dict[str, Any]]],
    max_view_calls: int,
) -> Tuple[Dict[str, Any], Dict[str, str], List[Dict[str, Any]]]:
    if not abi:
        return {}, {}, []

    views: Dict[str, Any] = {}
    view_errors: Dict[str, str] = {}

    view_fns = [
        fn
        for fn in abi
        if fn.get("type") == "function"
        and fn.get("stateMutability") in ("view", "pure")
        and fn.get("name")
    ]

    # Priority order for common security-relevant getters
    priority_names = [
        "asset",
        "totalAssets",
        "totalSupply",
        "paused",
        "owner",
        "admin",
        "registry",
        "oracle",
        "strategy",
        "controller",
        "feeRecipient",
        "withdrawDelay",
        "withdrawSettlementDelay",
        "maxDeposit",
        "maxSupply",
        "tvl",
        "marketCap",
    ]
    priority_rank = {name.lower(): i for i, name in enumerate(priority_names)}

    view_fns_sorted = sorted(
        view_fns,
        key=lambda f: (
            priority_rank.get(f["name"].lower(), 1_000),
            f["name"].lower(),
        ),
    )

    zero_input = []
    needs_input = []
    for fn in view_fns_sorted:
        if fn.get("inputs"):
            needs_input.append(fn)
        else:
            zero_input.append(fn)

    for fn in needs_input[:80]:
        view_errors[fn["name"]] = "requires input parameters"

    for fn in zero_input[:max_view_calls]:
        signature = abi_function_signature(fn)
        try:
            selector = client.selector(signature)
        except Exception as exc:
            view_errors[fn["name"]] = f"selector error: {exc}"
            continue
        raw = client.eth_call(address, selector)
        if raw is None:
            view_errors[fn["name"]] = "eth_call failed/reverted"
            continue

        outputs = fn.get("outputs", [])
        if len(outputs) == 0:
            views[fn["name"]] = raw
            continue
        if len(outputs) == 1:
            out_type = outputs[0].get("type", "bytes32")
            decoded = decode_single_output(raw, out_type)
            views[fn["name"]] = decoded
            continue
        # Multi-output fallback
        views[fn["name"]] = raw
    return views, view_errors, needs_input


def base_type(abi_type: str) -> str:
    t = abi_type.strip()
    if t.endswith("]"):
        return "array"
    if t.startswith("tuple"):
        return "tuple"
    if t == "address":
        return "address"
    if t == "bool":
        return "bool"
    if UINT_TYPE_RE.fullmatch(t):
        return "uint"
    if INT_TYPE_RE.fullmatch(t):
        return "int"
    if t == "bytes32":
        return "bytes32"
    if BYTESN_TYPE_RE.fullmatch(t):
        return "bytesn"
    if t in ("string", "bytes"):
        return "dynamic"
    return "other"


def is_static_supported_type(abi_type: str) -> bool:
    return base_type(abi_type) in {"address", "bool", "uint", "int", "bytes32", "bytesn"}


def type_compatible(input_type: str, event_type: str) -> bool:
    a = base_type(input_type)
    b = base_type(event_type)
    if a == b:
        return True
    if a in {"uint", "int"} and b in {"uint", "int"}:
        return True
    return False


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def semantic_name_score(input_name: str, field_name: str) -> int:
    a = normalize_name(input_name)
    b = normalize_name(field_name)
    if not a or not b:
        return 0
    if a == b:
        return 5
    if a in b or b in a:
        return 3
    id_words = ("id", "order", "trade", "position", "token")
    addr_words = ("owner", "user", "trader", "account", "sender", "receiver", "maker")
    if any(w in a for w in id_words) and any(w in b for w in id_words):
        return 2
    if any(w in a for w in addr_words) and any(w in b for w in addr_words):
        return 2
    return 0


def to_uint_word(value: int) -> str:
    if value < 0:
        value = 0
    return f"{value:064x}"


def to_int_word(value: int) -> str:
    if value < 0:
        value = (1 << 256) + value
    return f"{value & ((1 << 256) - 1):064x}"


def encode_static_arg(value: Any, abi_type: str) -> Optional[str]:
    t = base_type(abi_type)
    if t == "address":
        if not is_address(value):
            return None
        v = int(addr_norm(value), 16)
        return to_uint_word(v)
    if t == "bool":
        return to_uint_word(1 if bool(value) else 0)
    if t == "uint":
        try:
            v = int(value)
        except Exception:
            return None
        if v < 0:
            return None
        return to_uint_word(v)
    if t == "int":
        try:
            v = int(value)
        except Exception:
            return None
        return to_int_word(v)
    if t == "bytes32":
        if isinstance(value, str) and value.startswith("0x") and len(value) == 66:
            return value[2:].lower()
        return None
    if t == "bytesn":
        if not (isinstance(value, str) and value.startswith("0x")):
            return None
        raw = value[2:]
        try:
            n = int(abi_type[5:])
        except Exception:
            return None
        if len(raw) != n * 2:
            return None
        # bytesN is left-aligned and right-padded in ABI encoding
        return (raw + ("0" * (64 - len(raw)))).lower()
    return None


def encode_function_call_data(
    client: RpcClient,
    fn_abi: Dict[str, Any],
    args: List[Any],
) -> Optional[str]:
    inputs = fn_abi.get("inputs", [])
    if len(inputs) != len(args):
        return None
    sig = abi_function_signature(fn_abi)
    selector = client.selector(sig)
    encoded_words = []
    for inp, arg in zip(inputs, args):
        word = encode_static_arg(arg, inp.get("type", ""))
        if word is None:
            return None
        encoded_words.append(word)
    return selector + "".join(encoded_words)


def decode_multi_static_outputs(raw_hex: str, outputs: List[Dict[str, Any]]) -> Any:
    if raw_hex in (None, "0x") or not isinstance(raw_hex, str):
        return None
    if not outputs:
        return raw_hex
    if len(outputs) == 1:
        return decode_single_output(raw_hex, outputs[0].get("type", "bytes32"))

    data = raw_hex[2:]
    if len(data) < 64 * len(outputs):
        return raw_hex
    result: Dict[str, Any] = {}
    for i, out in enumerate(outputs):
        typ = out.get("type", "bytes32")
        name = out.get("name") or f"out_{i}"
        word = "0x" + data[i * 64 : (i + 1) * 64]
        if is_static_supported_type(typ):
            result[name] = decode_single_output(word, typ)
        else:
            result[name] = word
    return result


def function_name_tokens(name: str) -> List[str]:
    chunks = re.findall(r"[A-Z]?[a-z]+|[0-9]+", name or "")
    return [c.lower() for c in chunks if c]


def event_match_score(fn_abi: Dict[str, Any], ev_abi: Dict[str, Any]) -> int:
    fn_name = (fn_abi.get("name") or "").lower()
    ev_name = (ev_abi.get("name") or "").lower()
    score = 0
    if fn_name and ev_name and (fn_name in ev_name or ev_name in fn_name):
        score += 5

    fn_tokens = set(function_name_tokens(fn_abi.get("name") or ""))
    ev_tokens = set(function_name_tokens(ev_abi.get("name") or ""))
    score += 2 * len(fn_tokens.intersection(ev_tokens))

    fn_input_names = {normalize_name(inp.get("name", "")) for inp in fn_abi.get("inputs", [])}
    for ev_inp in ev_abi.get("inputs", []):
        e_name = normalize_name(ev_inp.get("name", ""))
        if not e_name:
            continue
        if e_name in fn_input_names:
            score += 3
    return score


def find_code_deploy_block(client: RpcClient, address: str, latest_block: int) -> int:
    # Binary search first block where code exists when archive state is available.
    # Some public RPCs are non-archive and return "missing trie node" for older blocks.
    try:
        _ = client.call("eth_getCode", [address, "0x1"])
    except Exception:
        # Fallback to a deep recent window when archive state is unavailable.
        return max(0, latest_block - 1_000_000)

    lo = 0
    hi = latest_block
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            code = client.call("eth_getCode", [address, hex(mid)])
        except Exception:
            # If historical state is unavailable mid-search, degrade gracefully.
            return max(0, latest_block - 1_000_000)
        if isinstance(code, str) and code != "0x":
            hi = mid
        else:
            lo = mid + 1
    return lo


def build_window_specs(latest_block: int, deploy_block: int) -> List[Tuple[str, int]]:
    specs = [
        ("last_50k", max(deploy_block, latest_block - 50_000)),
        ("last_250k", max(deploy_block, latest_block - 250_000)),
        ("last_1m", max(deploy_block, latest_block - 1_000_000)),
        ("from_deployment", deploy_block),
    ]
    out = []
    seen = set()
    for label, start in specs:
        key = (label, start)
        if key in seen:
            continue
        seen.add(key)
        out.append((label, start))
    return out


def looks_like_state_getter(fn_abi: Dict[str, Any]) -> bool:
    name = (fn_abi.get("name") or "").lower()
    hot = (
        "open",
        "pending",
        "request",
        "locked",
        "position",
        "order",
        "trade",
        "withdraw",
        "deposit",
        "convert",
        "claim",
        "epoch",
        "settlement",
        "balance",
    )
    return any(k in name for k in hot)


def discover_input_getter_mappings(
    client: RpcClient,
    address: str,
    abi: Optional[List[Dict[str, Any]]],
    latest_block: int,
    max_functions: int = 5,
    max_candidate_events: int = 3,
    max_logs_per_event: int = 8,
    max_candidates_per_input: int = 3,
    max_call_attempts: int = 8,
    time_budget_s: float = 20.0,
    per_function_time_budget_s: float = 4.0,
) -> Dict[str, Any]:
    if not abi:
        return {}
    skip_getters = {
        "balanceOf",
        "allowance",
        "ownerOf",
        "getApproved",
        "isApprovedForAll",
        "tokenURI",
        "supportsInterface",
    }
    all_view_fns = [
        fn
        for fn in abi
        if fn.get("type") == "function"
        and fn.get("stateMutability") in ("view", "pure")
        and fn.get("inputs")
        and fn.get("name")
        and str(fn.get("name", "")) not in skip_getters
        and not str(fn.get("name", "")).startswith("__DEPRECATED")
    ]
    if not all_view_fns:
        return {}

    events = [e for e in abi if e.get("type") == "event" and e.get("name")]
    if not events:
        return {}

    # Focus first on likely state mappings with lightweight, static key types.
    static_small = []
    for fn in all_view_fns:
        inputs = fn.get("inputs", [])
        if len(inputs) > 2:
            continue
        if any(not is_static_supported_type(inp.get("type", "")) for inp in inputs):
            continue
        static_small.append(fn)

    hot = [fn for fn in static_small if looks_like_state_getter(fn)]
    view_fns = hot if hot else static_small
    view_fns = sorted(view_fns, key=lambda fn: len(fn.get("inputs", [])))[:max_functions]
    if not view_fns:
        return {}

    deploy_block = find_code_deploy_block(client, address, latest_block)
    windows = build_window_specs(latest_block, deploy_block)
    event_log_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    start_t = time.monotonic()
    mappings: Dict[str, Any] = {}
    for fn in view_fns:
        if time.monotonic() - start_t > time_budget_s:
            mappings["_meta_truncated"] = {
                "reason": "time_budget_reached",
                "time_budget_s": time_budget_s,
                "processed_functions": len([k for k in mappings.keys() if not k.startswith("_meta_")]),
            }
            break
        fn_start_t = time.monotonic()
        fn_name = fn.get("name", "unknown")
        fn_key = f"{fn_name}({','.join(inp.get('type', '') for inp in fn.get('inputs', []))})"

        ranked_events = sorted(events, key=lambda ev: event_match_score(fn, ev), reverse=True)
        ranked_events = [ev for ev in ranked_events if event_match_score(fn, ev) > 2][:max_candidate_events]
        if not ranked_events:
            mappings[fn_name] = {
                "mapping_type": fn_key,
                "keys_source": "no matching events found",
                "sample_keys": [],
                "sample_values": {},
            }
            continue

        per_input_candidates: List[Dict[str, int]] = [defaultdict(int) for _ in fn.get("inputs", [])]
        source_events = set()
        source_windows = set()

        for window_label, from_block in windows:
            if time.monotonic() - start_t > time_budget_s:
                break
            if time.monotonic() - fn_start_t > per_function_time_budget_s:
                break
            for ev in ranked_events:
                if time.monotonic() - start_t > time_budget_s:
                    break
                if time.monotonic() - fn_start_t > per_function_time_budget_s:
                    break
                sig = abi_event_signature(ev)
                cache_key = (ev.get("name", "UnknownEvent"), window_label)
                if cache_key not in event_log_cache:
                    try:
                        topic0 = client.call("web3_sha3", ["0x" + sig.encode("utf-8").hex()])
                    except Exception:
                        event_log_cache[cache_key] = []
                        continue
                    recent_logs = fetch_logs_chunked(
                        client,
                        base_filter={"address": address, "topics": [topic0]},
                        from_block=from_block,
                        to_block=latest_block,
                        step=5_000,
                        max_logs=max_logs_per_event // 2 if max_logs_per_event > 1 else 1,
                        newest_first=True,
                        max_chunk_calls=2,
                    )
                    older_logs = fetch_logs_chunked(
                        client,
                        base_filter={"address": address, "topics": [topic0]},
                        from_block=from_block,
                        to_block=latest_block,
                        step=5_000,
                        max_logs=max_logs_per_event - len(recent_logs),
                        newest_first=False,
                        max_chunk_calls=2,
                    )
                    merged = []
                    seen_tx_log = set()
                    for log in recent_logs + older_logs:
                        key = (log.get("transactionHash"), log.get("logIndex"))
                        if key in seen_tx_log:
                            continue
                        seen_tx_log.add(key)
                        merged.append(log)
                        if len(merged) >= max_logs_per_event:
                            break
                    event_log_cache[cache_key] = merged
                logs = event_log_cache.get(cache_key, [])
                if not logs:
                    continue
                source_events.add(ev.get("name", "UnknownEvent"))
                source_windows.add(window_label)
                ev_inputs = ev.get("inputs", [])
                ev_input_by_name = {normalize_name(i.get("name", "")): i for i in ev_inputs}

                for log in logs:
                    decoded = decode_event_sample(log, ev)
                    if not decoded:
                        continue
                    for idx, fn_inp in enumerate(fn.get("inputs", [])):
                        f_name = fn_inp.get("name", "")
                        f_type = fn_inp.get("type", "")
                        best_values: List[Tuple[Any, int]] = []
                        for d_name, d_val in decoded.items():
                            if d_val is None:
                                continue
                            ev_inp = ev_input_by_name.get(normalize_name(d_name))
                            ev_type = ev_inp.get("type", "") if ev_inp else ""
                            if ev_type and not type_compatible(f_type, ev_type):
                                continue
                            # Guard against huge raw hex words from dynamic args.
                            if isinstance(d_val, str) and d_val.startswith("0x") and len(d_val) > 66:
                                continue
                            score = semantic_name_score(f_name, d_name)
                            if score <= 0:
                                continue
                            best_values.append((d_val, score))
                        for val, score in best_values:
                            key = str(val)
                            per_input_candidates[idx][key] = max(per_input_candidates[idx][key], score)

            # Stop early once every input has at least one candidate.
            if all(len(cands) > 0 for cands in per_input_candidates):
                break

        candidate_lists: List[List[Any]] = []
        for idx, cand_scores in enumerate(per_input_candidates):
            sorted_vals = sorted(cand_scores.items(), key=lambda kv: kv[1], reverse=True)
            vals: List[Any] = []
            for raw_val, _ in sorted_vals[:max_candidates_per_input]:
                typ = fn.get("inputs", [])[idx].get("type", "")
                if base_type(typ) == "address":
                    if is_address(raw_val):
                        vals.append(addr_norm(raw_val))
                elif base_type(typ) in {"uint", "int"}:
                    try:
                        vals.append(int(raw_val))
                    except Exception:
                        continue
                elif base_type(typ) == "bool":
                    vals.append(str(raw_val).lower() in ("1", "true"))
                elif base_type(typ) in {"bytes32", "bytesn"}:
                    if isinstance(raw_val, str) and raw_val.startswith("0x"):
                        vals.append(raw_val)
                else:
                    vals.append(raw_val)
            candidate_lists.append(vals)

        if any(len(vals) == 0 for vals in candidate_lists):
            mappings[fn_name] = {
                "mapping_type": fn_key,
                "keys_source": f"events={sorted(source_events)} windows={sorted(source_windows)}",
                "sample_keys": [],
                "sample_values": {},
            }
            continue

        # Generate limited combinations.
        combinations: List[List[Any]] = [[]]
        for vals in candidate_lists:
            next_combos: List[List[Any]] = []
            for combo in combinations:
                for v in vals:
                    next_combos.append(combo + [v])
                    if len(next_combos) >= max_call_attempts:
                        break
                if len(next_combos) >= max_call_attempts:
                    break
            combinations = next_combos[:max_call_attempts]
            if not combinations:
                break

        sample_values: Dict[str, Any] = {}
        sample_keys: List[str] = []
        for combo in combinations[:max_call_attempts]:
            calldata = encode_function_call_data(client, fn, combo)
            if not calldata:
                continue
            raw = client.eth_call(address, calldata)
            if raw in (None, "0x"):
                continue
            decoded = decode_multi_static_outputs(raw, fn.get("outputs", []))
            if len(combo) == 1:
                k = str(combo[0])
            else:
                k = json.dumps(combo)
            if k not in sample_values:
                sample_keys.append(k)
                sample_values[k] = decoded
            if len(sample_values) >= 5:
                break

        mappings[fn_name] = {
            "mapping_type": fn_key,
            "keys_source": f"events={sorted(source_events)} windows={sorted(source_windows)}",
            "sample_keys": sample_keys[:10],
            "sample_values": sample_values,
        }
    return mappings


def try_token_call(
    client: RpcClient, token: str, signature: str, out_type: str
) -> Any:
    try:
        selector = client.selector(signature)
    except Exception:
        return None
    raw = client.eth_call(token, selector)
    if raw in (None, "0x"):
        return None
    if signature in ("symbol()", "name()"):
        return decode_symbol_or_name(raw)
    return decode_single_output(raw, out_type)


def transfer_token_candidates(
    client: RpcClient,
    address: str,
    latest_block: int,
    sample_window_blocks: int,
    max_candidates: int,
) -> List[str]:
    start_block = max(0, latest_block - sample_window_blocks)
    try:
        transfer_topic = client.call("web3_sha3", ["0x" + TRANSFER_EVENT_SIG.encode("utf-8").hex()])
    except Exception:
        return []

    to_topic = pad_address_topic(address)
    from_topic = pad_address_topic(address)

    in_logs = fetch_logs_chunked(
        client,
        base_filter={"topics": [transfer_topic, None, to_topic]},
        from_block=start_block,
        to_block=latest_block,
        step=2_000,
    )
    out_logs = fetch_logs_chunked(
        client,
        base_filter={"topics": [transfer_topic, from_topic]},
        from_block=start_block,
        to_block=latest_block,
        step=2_000,
    )
    counter: Counter[str] = Counter()
    for log in in_logs + out_logs:
        token_addr = log.get("address")
        if is_address(token_addr):
            counter[addr_norm(token_addr)] += 1
    return [addr for addr, _ in counter.most_common(max_candidates)]


def erc20_balances(
    client: RpcClient,
    chain: str,
    address: str,
    latest_block: int,
    sample_window_blocks: int,
    max_tokens: int,
    discover_transfer_tokens: bool,
) -> List[Dict[str, Any]]:
    candidates = [addr_norm(t["address"]) for t in COMMON_TOKENS.get(chain, [])]
    if discover_transfer_tokens:
        candidates.extend(
            transfer_token_candidates(
                client, address, latest_block, sample_window_blocks, max_candidates=20
            )
        )
    # stable dedupe and keep order
    unique_candidates = []
    seen = set()
    for token in candidates:
        if token not in seen:
            seen.add(token)
            unique_candidates.append(token)
    unique_candidates = unique_candidates[:max_tokens]

    balances: List[Dict[str, Any]] = []
    for token in unique_candidates:
        # balanceOf(address)
        try:
            selector = client.selector("balanceOf(address)")
        except Exception:
            continue
        data = selector + ("0" * 24) + address[2:]
        raw_balance_hex = client.eth_call(token, data)
        raw_balance = hex_to_int(raw_balance_hex)
        if raw_balance is None or raw_balance == 0:
            continue

        decimals = try_token_call(client, token, "decimals()", "uint8")
        symbol = try_token_call(client, token, "symbol()", "string")
        name = try_token_call(client, token, "name()", "string")

        if not isinstance(decimals, int):
            # if decimals is not readable, skip from ERC20 bucket
            continue

        try:
            human = Decimal(raw_balance) / (Decimal(10) ** Decimal(decimals))
            human_str = format(human, "f")
        except (InvalidOperation, ZeroDivisionError):
            human_str = str(raw_balance)

        balances.append(
            {
                "token": token,
                "symbol": symbol or "unknown",
                "name": name or "unknown",
                "decimals": decimals,
                "raw_balance": str(raw_balance),
                "human_balance": human_str,
                "usd_value": "unknown",
            }
        )

    balances.sort(key=lambda x: int(x["raw_balance"]), reverse=True)
    return balances


def detect_nft_holdings(
    client: RpcClient,
    address: str,
    latest_block: int,
    sample_window_blocks: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    start_block = max(0, latest_block - sample_window_blocks)
    erc721: Dict[str, Dict[str, Any]] = {}
    erc1155: Dict[str, Dict[str, Any]] = {}

    try:
        transfer_topic = client.call("web3_sha3", ["0x" + TRANSFER_EVENT_SIG.encode("utf-8").hex()])
        transfer_single_topic = client.call("web3_sha3", ["0x" + TRANSFER_SINGLE_EVENT_SIG.encode("utf-8").hex()])
        transfer_batch_topic = client.call("web3_sha3", ["0x" + TRANSFER_BATCH_EVENT_SIG.encode("utf-8").hex()])
    except Exception:
        return [], []

    to_topic = pad_address_topic(address)

    # ERC721 candidates are Transfer logs with 4 topics (topic3 = tokenId)
    nft_logs = fetch_logs_chunked(
        client,
        base_filter={"topics": [transfer_topic, None, to_topic]},
        from_block=start_block,
        to_block=latest_block,
        step=2_000,
    )
    for log in nft_logs:
        topics = log.get("topics") or []
        if len(topics) < 4:
            continue
        token_addr = log.get("address")
        if not is_address(token_addr):
            continue
        token_addr = addr_norm(token_addr)
        token_id = str(int(topics[3], 16))
        item = erc721.setdefault(
            token_addr,
            {
                "token": token_addr,
                "name": "unknown",
                "balance": 0,
                "sample_token_ids": [],
            },
        )
        if token_id not in item["sample_token_ids"] and len(item["sample_token_ids"]) < 10:
            item["sample_token_ids"].append(token_id)

    # Fill 721 balances and names
    for token_addr, item in erc721.items():
        bal = None
        try:
            selector = client.selector("balanceOf(address)")
            data = selector + ("0" * 24) + address[2:]
            bal = hex_to_int(client.eth_call(token_addr, data))
        except Exception:
            bal = None
        if isinstance(bal, int):
            item["balance"] = str(bal)
        nm = try_token_call(client, token_addr, "name()", "string")
        if isinstance(nm, str) and nm:
            item["name"] = nm

    # ERC1155 transfer single/batch as heuristic presence
    t_single = fetch_logs_chunked(
        client,
        base_filter={"topics": [transfer_single_topic, None, None, to_topic]},
        from_block=start_block,
        to_block=latest_block,
        step=2_000,
    )
    for log in t_single:
        token_addr = log.get("address")
        if not is_address(token_addr):
            continue
        token_addr = addr_norm(token_addr)
        erc1155.setdefault(token_addr, {"token": token_addr, "name": "unknown", "sample_transfers": 0})
        erc1155[token_addr]["sample_transfers"] += 1

    t_batch = fetch_logs_chunked(
        client,
        base_filter={"topics": [transfer_batch_topic, None, None, to_topic]},
        from_block=start_block,
        to_block=latest_block,
        step=2_000,
    )
    for log in t_batch:
        token_addr = log.get("address")
        if not is_address(token_addr):
            continue
        token_addr = addr_norm(token_addr)
        erc1155.setdefault(token_addr, {"token": token_addr, "name": "unknown", "sample_transfers": 0})
        erc1155[token_addr]["sample_transfers"] += 1

    return list(erc721.values()), list(erc1155.values())


def collect_dependencies(
    client: RpcClient,
    chain_cfg: ChainConfig,
    parent_address: str,
    views: Dict[str, Any],
    proxy: Dict[str, Any],
    etherscan_api_key: Optional[str],
) -> List[Dict[str, Any]]:
    deps: Dict[str, str] = {}

    # from view outputs
    for name, value in views.items():
        if is_address(value):
            if addr_norm(value) != parent_address:
                deps[name] = addr_norm(value)
    # proxy internals
    for k in ("implementation", "admin", "beacon"):
        v = proxy.get(k)
        if is_address(v) and addr_norm(v) != parent_address:
            deps[f"proxy_{k}"] = addr_norm(v)

    out = []
    for dep_name, dep_addr in list(deps.items())[:12]:
        identity = extract_contract_identity(dep_addr, chain_cfg.chain_id, etherscan_api_key)
        dep_views = {}
        for key, signature, out_type in (
            ("owner", "owner()", "address"),
            ("paused", "paused()", "bool"),
            ("totalAssets", "totalAssets()", "uint256"),
            ("totalSupply", "totalSupply()", "uint256"),
            ("asset", "asset()", "address"),
            ("registry", "registry()", "address"),
            ("latestAnswer", "latestAnswer()", "int256"),
            ("decimals", "decimals()", "uint8"),
            ("heartbeat", "heartbeat()", "uint256"),
        ):
            try:
                selector = client.selector(signature)
                raw = client.eth_call(dep_addr, selector)
                if raw in (None, "0x"):
                    continue
                dep_views[key] = decode_single_output(raw, out_type)
            except Exception:
                continue
        out.append(
            {
                "name": identity.get("contract_name") or dep_name,
                "address": dep_addr,
                "source_file": identity.get("source_file") or "unknown",
                "views": dep_views,
                "balances": {"erc20": []},
            }
        )
    return out


def collect_recent_activity(
    events_discovered: Dict[str, Any],
    sample_window_blocks: int,
) -> Dict[str, Any]:
    large_deposits = []
    large_withdrawals = []
    admin_events = []

    for event_name, payload in events_discovered.items():
        lower = event_name.lower()
        sample_args = payload.get("sample_args", [])[:5]
        count = payload.get("count_sampled", 0)
        row = {"event": event_name, "count": count, "samples": sample_args}
        if "deposit" in lower:
            large_deposits.append(row)
        if "withdraw" in lower or "redeem" in lower:
            large_withdrawals.append(row)
        if any(k in lower for k in ("pause", "unpause", "upgrade", "owner", "admin")):
            admin_events.append(row)

    return {
        "sample_window_blocks": sample_window_blocks,
        "large_transfers": [],
        "large_deposits": large_deposits[:10],
        "large_withdrawals": large_withdrawals[:10],
        "admin_events": admin_events[:10],
    }


def infer_risk_notes(
    contract: Dict[str, Any],
) -> List[str]:
    notes = []
    erc20 = contract["balances"].get("erc20", [])
    if erc20:
        largest = erc20[0]
        notes.append(
            f"Contract holds non-trivial ERC20 balance, largest observed: {largest.get('symbol', 'token')} {largest.get('human_balance')}"
        )
    views = contract.get("views", {})
    if "totalAssets" in views and "totalSupply" in views:
        notes.append("totalAssets/totalSupply are exposed; share-accounting invariants should be tested.")
    if "paused" in views:
        notes.append("Paused state is externally visible; check freeze/unfreeze and privileged state transitions.")
    if any(k in views for k in ("oracle", "priceFeed", "priceRouter", "latestAnswer")):
        notes.append("Oracle/price dependencies detected; stale and invalid pricing paths should be tested.")
    if any(k in views for k in ("strategy", "controller", "marketMaker")):
        notes.append("External strategy/controller dependency detected; accounting sync and insolvency paths are relevant.")
    if not notes:
        notes.append("No strong heuristics found from zero-arg views; prioritize user flow invariants and value movement.")
    return notes


def infer_audit_focus(contract: Dict[str, Any]) -> List[str]:
    focus = [
        "direct theft via accounting mismatch",
        "permanent freezing via state-machine or request/finalization bugs",
        "protocol insolvency via stale/inflated asset accounting",
    ]
    views = contract.get("views", {})
    events = contract.get("events_discovered", {})
    if any("claim" in ev.lower() or "reward" in ev.lower() for ev in events.keys()):
        focus.append("yield/reward theft via claim-ordering or accrual mismatch")
    if any(key.lower().startswith("oracle") for key in views.keys()) or "latestAnswer" in views:
        focus.append("temporary freezing or mispricing via stale oracle/price feed")
    return focus


def identity_looks_like_proxy(identity: Dict[str, Any]) -> bool:
    name = str(identity.get("contract_name") or "").lower()
    src = str(identity.get("source_file") or "").lower()
    if "proxy" in name:
        return True
    if "proxy" in src:
        return True
    return False


def build_contract_context(
    client: RpcClient,
    chain_cfg: ChainConfig,
    protocol: str,
    address: str,
    latest_block: int,
    sample_window_blocks: int,
    max_view_calls: int,
    max_event_samples: int,
    max_events: int,
    max_erc20_tokens: int,
    discover_transfer_tokens: bool,
    include_nft_scan: bool,
    include_dependencies: bool,
    mapping_time_budget_s: float,
    etherscan_api_key: Optional[str],
) -> Dict[str, Any]:
    proxy = detect_proxy(client, address)
    proxy_identity = extract_contract_identity(address, chain_cfg.chain_id, etherscan_api_key)
    identity = proxy_identity

    # If address is proxy, prefer implementation ABI/source for real protocol context.
    implementation = proxy.get("implementation")
    if implementation:
        impl_identity = extract_contract_identity(implementation, chain_cfg.chain_id, etherscan_api_key)
        if impl_identity.get("abi"):
            if identity_looks_like_proxy(proxy_identity) or not proxy_identity.get("abi"):
                identity = impl_identity

    code = client.call("eth_getCode", [address, "latest"])
    is_contract = bool(isinstance(code, str) and code != "0x")

    native_raw_hex = client.call("eth_getBalance", [address, "latest"])
    native_raw = int(native_raw_hex, 16)
    native_human = str(Decimal(native_raw) / Decimal(10**18))

    abi = identity.get("abi")
    views, view_errors, _needs_input_views = collect_zero_arg_views(
        client, address, abi, max_view_calls=max_view_calls
    )
    events_discovered = collect_events_discovered(
        client,
        address,
        abi,
        latest_block=latest_block,
        sample_window_blocks=sample_window_blocks,
        max_event_samples=max_event_samples,
        max_events=max_events,
    )
    mappings_discovered = infer_mappings_discovered(events_discovered)
    deep_mappings = discover_input_getter_mappings(
        client=client,
        address=address,
        abi=abi,
        latest_block=latest_block,
        time_budget_s=mapping_time_budget_s,
    )
    # Deep mappings override/add heuristic buckets.
    mappings_discovered.update(deep_mappings)
    erc20 = erc20_balances(
        client,
        chain=chain_cfg.chain,
        address=address,
        latest_block=latest_block,
        sample_window_blocks=sample_window_blocks,
        max_tokens=max_erc20_tokens,
        discover_transfer_tokens=discover_transfer_tokens,
    )
    if include_nft_scan:
        erc721, erc1155 = detect_nft_holdings(
            client,
            address=address,
            latest_block=latest_block,
            sample_window_blocks=sample_window_blocks,
        )
    else:
        erc721, erc1155 = [], []

    if include_dependencies:
        dependencies = collect_dependencies(
            client=client,
            chain_cfg=chain_cfg,
            parent_address=address,
            views=views,
            proxy=proxy,
            etherscan_api_key=etherscan_api_key,
        )
    else:
        dependencies = []
    recent_activity = collect_recent_activity(events_discovered, sample_window_blocks=sample_window_blocks)

    contract_name = identity.get("contract_name")
    if not contract_name and not is_contract:
        contract_name = "EOA"
    contract_name = contract_name or "UnknownContract"

    contract_ctx = {
        "name": contract_name,
        "address": address,
        "source_file": identity.get("source_file") or "unknown",
        "proxy": {
            "is_proxy": proxy["is_proxy"],
            "implementation": proxy["implementation"],
            "admin": proxy["admin"],
            "proxy_type": proxy["proxy_type"],
        },
        "balances": {
            "native": {
                "raw": str(native_raw),
                "human": f"{native_human} {chain_cfg.native_symbol}",
                "usd": "unknown",
            },
            "erc20": erc20,
            "erc721": erc721,
            "erc1155": erc1155,
        },
        "views": views,
        "view_errors": view_errors,
        "events_discovered": events_discovered,
        "mappings_discovered": mappings_discovered,
        "dependencies": dependencies,
        "recent_activity": recent_activity,
        "risk_notes": [],
        "audit_focus": [],
        "_meta": {
            "is_contract": is_contract,
            "code_size_bytes": 0 if code == "0x" else (len(code) - 2) // 2,
            "verification_source": identity.get("verification_source"),
            "compiler_version": identity.get("compiler_version"),
            "proxy_contract_name": proxy_identity.get("contract_name"),
            "proxy_source_file": proxy_identity.get("source_file"),
        },
    }
    contract_ctx["risk_notes"] = infer_risk_notes(contract_ctx)
    contract_ctx["audit_focus"] = infer_audit_focus(contract_ctx)
    return contract_ctx


def infer_protocol_name(contracts: List[Dict[str, Any]], fallback: str = "UNKNOWN") -> str:
    prefixes = []
    for c in contracts:
        name = c.get("name") or ""
        if not isinstance(name, str):
            continue
        m = re.match(r"([A-Z][a-zA-Z0-9]{2,})", name)
        if m:
            prefix = m.group(1)
            # strip generic suffixes when possible
            for suffix in ("Vault", "Storage", "Registry", "Proxy", "Router", "Token", "Contract"):
                if prefix.endswith(suffix) and len(prefix) > len(suffix):
                    prefix = prefix[: -len(suffix)]
            if prefix:
                prefixes.append(prefix)
    if not prefixes:
        return fallback
    common, _ = Counter(prefixes).most_common(1)[0]
    return common.upper()


def scan_chain_scope(
    chain_cfg: ChainConfig,
    addresses: List[str],
    protocol_hint: Optional[str],
    sample_window_blocks: int,
    max_view_calls: int,
    max_event_samples: int,
    max_events: int,
    max_erc20_tokens: int,
    discover_transfer_tokens: bool,
    include_nft_scan: bool,
    include_dependencies: bool,
    mapping_time_budget_s: float,
    etherscan_api_key: Optional[str],
) -> Dict[str, Any]:
    client = RpcClient(chain_cfg.rpc_endpoints)
    block_number, _ = get_block_and_timestamp(client)
    contracts = []

    for idx, address in enumerate(addresses, 1):
        print(f"[scan] {chain_cfg.chain} {idx}/{len(addresses)} {address}", flush=True)
        contracts.append(
            build_contract_context(
                client=client,
                chain_cfg=chain_cfg,
                protocol=protocol_hint or "UNKNOWN",
                address=address,
                latest_block=block_number,
                sample_window_blocks=sample_window_blocks,
                max_view_calls=max_view_calls,
                max_event_samples=max_event_samples,
                max_events=max_events,
                max_erc20_tokens=max_erc20_tokens,
                discover_transfer_tokens=discover_transfer_tokens,
                include_nft_scan=include_nft_scan,
                include_dependencies=include_dependencies,
                mapping_time_budget_s=mapping_time_budget_s,
                etherscan_api_key=etherscan_api_key,
            )
        )

    protocol = protocol_hint or infer_protocol_name(contracts, fallback="UNKNOWN")
    return {
        "protocol": protocol,
        "chain": chain_cfg.chain,
        "generated_at": now_iso_utc(),
        "block_number": block_number,
        "contracts": contracts,
    }


def load_scope_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return []

    # JSON list
    if raw.startswith("["):
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    # Plain text
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def gather_scope_entries(
    urls: List[str],
    scope_file: Optional[str],
    from_questions: bool,
) -> List[str]:
    entries = list(urls)
    if scope_file:
        entries.extend(load_scope_file(scope_file))
    if from_questions:
        loaded = False
        try:
            from questions import scope_scan as questions_scope_scan  # type: ignore

            if isinstance(questions_scope_scan, list):
                entries.extend(str(x) for x in questions_scope_scan)
                loaded = True
        except Exception:
            loaded = False

        # Fallback: static-parse questions.py without importing dependencies.
        if not loaded:
            qfile = os.path.join(os.getcwd(), "questions.py")
            if os.path.exists(qfile):
                try:
                    with open(qfile, "r", encoding="utf-8") as f:
                        node = ast.parse(f.read(), filename=qfile)
                    for item in node.body:
                        if not isinstance(item, ast.Assign):
                            continue
                        if len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
                            continue
                        if item.targets[0].id != "scope_scan":
                            continue
                        if isinstance(item.value, ast.List):
                            for elt in item.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    entries.append(elt.value)
                            break
                except Exception:
                    pass
    return [x for x in entries if isinstance(x, str) and x.strip()]


def group_scope_by_chain(entries: List[str], default_chain: Optional[str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = defaultdict(list)
    seen = set()
    for item in entries:
        chain, address = parse_scope_item(item)
        if not address:
            continue
        chain = chain or default_chain
        if not chain:
            continue
        if chain not in CHAIN_CONFIGS:
            continue
        key = (chain, address)
        if key in seen:
            continue
        seen.add(key)
        grouped[chain].append(address)
    return grouped


def build_output_payload(contexts: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(contexts) == 1:
        return contexts[0]
    return {
        "generated_at": now_iso_utc(),
        "contexts": contexts,
    }


def write_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build live audit context JSON from explorer URLs or addresses.")
    parser.add_argument("--url", action="append", default=[], help="Explorer URL or raw address. Can be repeated.")
    parser.add_argument("--scope-file", help="Path to a JSON/text file containing URLs/addresses.")
    parser.add_argument(
        "--from-questions",
        action="store_true",
        help="Also load scope_scan from questions.py",
    )
    parser.add_argument(
        "--default-chain",
        choices=sorted(CHAIN_CONFIGS.keys()),
        help="Default chain for raw addresses (when no explorer domain is present).",
    )
    parser.add_argument("--protocol", help="Protocol name override (e.g. OSTIUM).")
    parser.add_argument("--out", default="setup/live_context.json", help="Output JSON path.")
    parser.add_argument(
        "--sample-window-blocks",
        type=int,
        default=1_200,
        help="Block window for event and activity sampling.",
    )
    parser.add_argument(
        "--max-view-calls",
        type=int,
        default=25,
        help="Max zero-arg view calls per contract.",
    )
    parser.add_argument(
        "--max-event-samples",
        type=int,
        default=1,
        help="Max decoded event samples per event name.",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=4,
        help="Max event names sampled per contract.",
    )
    parser.add_argument(
        "--max-erc20-tokens",
        type=int,
        default=10,
        help="Max candidate ERC20 tokens checked per contract.",
    )
    parser.add_argument(
        "--discover-transfer-tokens",
        action="store_true",
        help="Discover extra ERC20 candidates from recent Transfer logs (slower).",
    )
    parser.add_argument(
        "--include-nft-scan",
        action="store_true",
        help="Enable ERC721/ERC1155 scan via transfer logs (slower).",
    )
    parser.add_argument(
        "--include-dependencies",
        action="store_true",
        help="Resolve dependency contract metadata/views from discovered addresses (slower).",
    )
    parser.add_argument(
        "--mapping-time-budget-s",
        type=float,
        default=20.0,
        help="Per-contract time budget for input-getter mapping discovery.",
    )
    parser.add_argument(
        "--etherscan-api-key",
        default=os.getenv("ETHERSCAN_API_KEY"),
        help="Optional Etherscan V2 API key for ABI/source fallback.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    auto_mode = False
    if not args.url and not args.scope_file and not args.from_questions:
        # Zero-argument behavior: use scope_scan from questions.py
        args.from_questions = True
        auto_mode = True
        print("[auto] no inputs provided; using scope_scan from questions.py")

    entries = gather_scope_entries(args.url, args.scope_file, args.from_questions)
    if not entries:
        print("No scope entries provided. Use --url, --scope-file, or --from-questions.", file=sys.stderr)
        return 1

    grouped = group_scope_by_chain(entries, default_chain=args.default_chain)
    if not grouped:
        print("No valid chain/address entries found. Check URLs and --default-chain.", file=sys.stderr)
        return 1

    contexts = []
    for chain, addresses in grouped.items():
        cfg = CHAIN_CONFIGS[chain]
        print(f"[scan] chain={chain} contracts={len(addresses)}")
        context = scan_chain_scope(
            chain_cfg=cfg,
            addresses=addresses,
            protocol_hint=args.protocol,
            sample_window_blocks=args.sample_window_blocks,
            max_view_calls=args.max_view_calls,
            max_event_samples=args.max_event_samples,
            max_events=args.max_events,
            max_erc20_tokens=args.max_erc20_tokens,
            discover_transfer_tokens=args.discover_transfer_tokens,
            include_nft_scan=args.include_nft_scan,
            include_dependencies=args.include_dependencies,
            mapping_time_budget_s=args.mapping_time_budget_s,
            etherscan_api_key=args.etherscan_api_key,
        )
        contexts.append(context)

    payload = build_output_payload(contexts)
    write_json(args.out, payload)
    if auto_mode:
        print(f"[auto] scanned {sum(len(v) for v in grouped.values())} addresses from questions.py")
    print(f"[ok] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
