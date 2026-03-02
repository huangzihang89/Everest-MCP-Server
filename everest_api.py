#!/usr/bin/env python3
"""
Everest API MCP server (streamable HTTP).

This server exposes three tools:
- everest_query_v1: full-domain aggregate query.
- everest_query_v2: subdomain-only query with cross-TLD filtering.
- everest_query_batch: batch query for v1/v2.

API key priority:
1) tool argument `api_key` (highest priority)
2) env var `EVEREST_API_KEY`

Run:
  pip install mcp requests
  EVEREST_API_KEY=your_key python3 everest_api.py

Default MCP transport is streamable-http. Override with MCP_TRANSPORT.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - runtime dependency check
    FastMCP = None  # type: ignore[assignment]


API_BASE_URL = "https://api.everest.validity.com/api/2.0"
REQUEST_INTERVAL = float(os.getenv("EVEREST_REQUEST_INTERVAL", "0.5"))
TIMEOUT = int(os.getenv("EVEREST_TIMEOUT", "30"))
RETRY_DELAY = float(os.getenv("EVEREST_RETRY_DELAY", "8.0"))

DOMAIN_PATTERN = re.compile(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$")


def _clean_key(api_key: Optional[str]) -> str:
    key = (api_key or os.getenv("EVEREST_API_KEY", "")).strip()
    key = key.replace("\ufeff", "").replace("\u200b", "").replace("\r", "").replace("\n", "")
    return "".join(ch for ch in key if 32 <= ord(ch) < 127)


def _extract_domain_name(match_item: Any) -> str:
    if isinstance(match_item, str):
        return match_item
    if isinstance(match_item, dict):
        return str(match_item.get("domain") or match_item.get("name") or match_item.get("value") or match_item)
    return str(match_item)


def _format_percent(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0%"
    if number == int(number):
        return f"{int(number)}%"
    return f"{number}%"


def _is_valid_subdomain(match_domain: str, base_domain: str) -> bool:
    match_domain = match_domain.lower().strip()
    base_domain = base_domain.lower().strip()
    return match_domain == base_domain or match_domain.endswith("." + base_domain)


def _filter_subdomains(matches: List[str], base_domain: str) -> Tuple[List[str], List[str]]:
    valid_subdomains: List[str] = []
    filtered_out: List[str] = []
    for domain in matches:
        if _is_valid_subdomain(domain, base_domain):
            valid_subdomains.append(domain)
        else:
            filtered_out.append(domain)
    return valid_subdomains, filtered_out


def _parse_domains(domains: Union[str, List[str]]) -> List[str]:
    if isinstance(domains, list):
        return [str(d).strip() for d in domains if str(d).strip()]
    parts = re.split(r"[,;\n\t ]+", str(domains).strip())
    return [p for p in parts if p]


def _normalize_matches(matches: Any) -> List[str]:
    if matches is None:
        return []

    raw_items: List[Any]
    if isinstance(matches, str):
        if not matches.strip():
            return []
        raw_items = [item for item in re.split(r"[,;\n\t ]+", matches.strip()) if item]
    elif isinstance(matches, dict):
        raw_items = [matches]
    elif isinstance(matches, (list, tuple, set)):
        raw_items = list(matches)
    else:
        raw_items = [matches]

    normalized: List[str] = []
    seen = set()
    for item in raw_items:
        domain = _extract_domain_name(item).strip()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        normalized.append(domain)
    return normalized


def _normalize_search_id(search_id: Any) -> Optional[int]:
    try:
        return int(str(search_id).strip())
    except (TypeError, ValueError, AttributeError):
        return None


class EverestClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"X-API-KEY": api_key}
        self.last_request_time = 0.0

    def _rate_limit(self) -> None:
        elapsed = time.time() - self.last_request_time
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)
        self.last_request_time = time.time()

    @staticmethod
    def _handle_error(response: requests.Response, context: str) -> str:
        status = response.status_code
        if status == 401:
            return "API_ERROR:401_无效或过期的API_Key"
        if status == 403:
            return "API_ERROR:403_无权限"
        if status == 404:
            return "API_ERROR:404_未找到"
        if status == 429:
            return "API_ERROR:429_请求过于频繁"
        return f"API_ERROR:{status}_{context}"

    def step1_create_search(self, domain: str) -> Dict[str, Any]:
        self._rate_limit()
        url = f"{API_BASE_URL}/prospect/search"
        files = {"domain": (None, domain)}
        try:
            response = requests.post(url, headers=self.headers, files=files, timeout=TIMEOUT)
            if response.status_code != 200:
                return {"success": False, "error": self._handle_error(response, "创建搜索失败")}
            data = response.json()
            results = data.get("results")
            if isinstance(results, dict) and (
                results.get("id") is not None or results.get("matches") is not None
            ):
                return {
                    "success": True,
                    "search_id": results.get("id"),
                    "matches": _normalize_matches(results.get("matches", [])),
                }
            return {
                "success": True,
                "search_id": data.get("id"),
                "matches": _normalize_matches(data.get("matches", [])),
            }
        except requests.exceptions.RequestException as exc:
            return {"success": False, "error": f"REQUEST_ERROR:{str(exc)[:120]}"}
        except ValueError:
            return {"success": False, "error": "JSON_PARSE_ERROR"}

    def step2_confirm_matches(self, search_id: int, matches: str) -> Dict[str, Any]:
        self._rate_limit()
        url = f"{API_BASE_URL}/prospect/search/{search_id}"
        data = {"matches": matches}
        try:
            response = requests.put(url, headers=self.headers, data=data, timeout=TIMEOUT)
            if response.status_code != 200:
                return {"success": False, "error": self._handle_error(response, "确认匹配失败")}
            body = response.json()
            results = body.get("results")
            if isinstance(results, dict) and (
                results.get("volume") is not None
                or results.get("traps") is not None
                or results.get("domain") is not None
            ):
                return {
                    "success": True,
                    "volume": results.get("volume", "N/A"),
                    "traps": results.get("traps", 0),
                    "domain": results.get("domain", ""),
                }
            return {
                "success": True,
                "volume": body.get("volume", "N/A"),
                "traps": body.get("traps", 0),
                "domain": body.get("domain", ""),
            }
        except requests.exceptions.RequestException as exc:
            return {"success": False, "error": f"REQUEST_ERROR:{str(exc)[:120]}"}
        except ValueError:
            return {"success": False, "error": "JSON_PARSE_ERROR"}

    def step3_get_esps(self, search_id: int) -> Dict[str, Any]:
        self._rate_limit()
        url = f"{API_BASE_URL}/prospect/search/{search_id}/esps"
        try:
            response = requests.get(url, headers=self.headers, timeout=TIMEOUT)
            if response.status_code != 200:
                return {"success": False, "error": self._handle_error(response, "获取ESP失败")}
            data = response.json()
            results = data.get("results", data)
            esps: List[Dict[str, Any]] = []

            if isinstance(results, dict):
                if "esps" in results:
                    esps_data = results.get("esps", {})
                    total = results.get("total", 0)
                    if isinstance(esps_data, dict):
                        if not total and esps_data:
                            total = sum(v for v in esps_data.values() if isinstance(v, (int, float)))
                        for esp_name, count in esps_data.items():
                            percent = round((count / total) * 100, 2) if total and count else 0
                            esps.append({"esp": esp_name, "count": count, "percent": percent})
                    elif isinstance(esps_data, list):
                        for item in esps_data:
                            if isinstance(item, dict):
                                esps.append(
                                    {
                                        "esp": item.get("esp") or item.get("name") or "Unknown",
                                        "count": item.get("count", 0),
                                        "percent": item.get("percent", 0),
                                    }
                                )
                elif {"esp", "count", "percent", "name"} & set(results.keys()):
                    esps.append(
                        {
                            "esp": results.get("esp") or results.get("name") or "Unknown",
                            "count": results.get("count", 0),
                            "percent": results.get("percent", 0),
                        }
                    )
                elif results and all(isinstance(v, (int, float)) for v in results.values()):
                    total = sum(results.values())
                    for esp_name, count in results.items():
                        percent = round((count / total) * 100, 2) if total and count else 0
                        esps.append({"esp": esp_name, "count": count, "percent": percent})
            elif isinstance(results, list):
                for item in results:
                    if isinstance(item, dict):
                        esps.append(
                            {
                                "esp": item.get("esp") or item.get("name") or "Unknown",
                                "count": item.get("count", 0),
                                "percent": item.get("percent", 0),
                            }
                        )

            esps.sort(key=lambda x: x.get("count", 0), reverse=True)
            return {"success": True, "esps": esps}
        except requests.exceptions.RequestException as exc:
            return {"success": False, "error": f"REQUEST_ERROR:{str(exc)[:120]}"}
        except ValueError:
            return {"success": False, "error": "JSON_PARSE_ERROR"}

    def _validate_domain(self, domain: str) -> Optional[str]:
        domain = domain.strip().lower()
        if not domain:
            return "INVALID_DOMAIN:empty"
        if not DOMAIN_PATTERN.match(domain):
            return "INVALID_DOMAIN:pattern"
        return None

    def query_v1(self, domain: str) -> Dict[str, Any]:
        domain_error = self._validate_domain(domain)
        if domain_error:
            return {"success": False, "error": domain_error, "domain": domain}

        result: Dict[str, Any] = {
            "success": False,
            "domain": domain,
            "subdomains": [],
            "volume": "N/A",
            "esps": [],
            "error": None,
        }

        search_result = self.step1_create_search(domain)
        search_id = _normalize_search_id(search_result.get("search_id"))
        matches_raw = search_result.get("matches", []) if search_result.get("success") else []
        all_domains = _normalize_matches(matches_raw)

        if (not search_result.get("success")) or (not search_id) or (not all_domains):
            time.sleep(RETRY_DELAY)
            search_result = self.step1_create_search(domain)
            search_id = _normalize_search_id(search_result.get("search_id"))
            matches_raw = search_result.get("matches", []) if search_result.get("success") else []
            all_domains = _normalize_matches(matches_raw)
            if (not search_result.get("success")) or (not search_id) or (not all_domains):
                result["error"] = search_result.get("error") or "NO_MATCHES_FOUND"
                return result

        result["subdomains"] = all_domains
        joined_matches = ",".join(all_domains)

        confirm_result = self.step2_confirm_matches(search_id, joined_matches)
        if not confirm_result.get("success"):
            time.sleep(RETRY_DELAY)
            confirm_result = self.step2_confirm_matches(search_id, joined_matches)
            if not confirm_result.get("success"):
                result["error"] = confirm_result.get("error")
                return result

        volume = confirm_result.get("volume", "N/A")
        if not volume or str(volume).strip().upper() == "N/A":
            time.sleep(RETRY_DELAY)
            confirm_retry = self.step2_confirm_matches(search_id, joined_matches)
            if confirm_retry.get("success"):
                volume = confirm_retry.get("volume", "N/A")
        result["volume"] = volume

        esp_result = self.step3_get_esps(search_id)
        if not esp_result.get("success") or not esp_result.get("esps"):
            time.sleep(RETRY_DELAY)
            esp_result = self.step3_get_esps(search_id)
        if esp_result.get("success") and esp_result.get("esps"):
            result["esps"] = esp_result["esps"]

        result["success"] = True
        return result

    def query_v2(self, domain: str) -> Dict[str, Any]:
        domain_error = self._validate_domain(domain)
        if domain_error:
            return {"success": False, "error": domain_error, "domain": domain}

        result: Dict[str, Any] = {
            "success": False,
            "domain": domain,
            "subdomains": [],
            "filtered_out": [],
            "volume": "N/A",
            "esps": [],
            "error": None,
        }

        search_result = self.step1_create_search(domain)
        search_id = _normalize_search_id(search_result.get("search_id"))
        matches_raw = search_result.get("matches", []) if search_result.get("success") else []
        all_domains = _normalize_matches(matches_raw)

        if (not search_result.get("success")) or (not search_id) or (not all_domains):
            time.sleep(RETRY_DELAY)
            search_result = self.step1_create_search(domain)
            search_id = _normalize_search_id(search_result.get("search_id"))
            matches_raw = search_result.get("matches", []) if search_result.get("success") else []
            all_domains = _normalize_matches(matches_raw)
            if (not search_result.get("success")) or (not search_id) or (not all_domains):
                result["error"] = search_result.get("error") or "NO_MATCHES_FOUND"
                return result

        valid_subdomains, filtered_out = _filter_subdomains(all_domains, domain)
        result["subdomains"] = valid_subdomains
        result["filtered_out"] = filtered_out

        if not valid_subdomains:
            time.sleep(RETRY_DELAY)
            retry_search = self.step1_create_search(domain)
            if not retry_search.get("success"):
                result["error"] = retry_search.get("error")
                return result
            search_id = _normalize_search_id(retry_search.get("search_id"))
            matches_raw = retry_search.get("matches", [])
            all_domains = _normalize_matches(matches_raw)
            if not search_id:
                result["error"] = "NO_SEARCH_ID"
                return result
            valid_subdomains, filtered_out = _filter_subdomains(all_domains, domain)
            result["subdomains"] = valid_subdomains
            result["filtered_out"] = filtered_out
            if not valid_subdomains:
                result["success"] = True
                result["error"] = "NO_VALID_SUBDOMAINS"
                return result

        joined_matches = ",".join(valid_subdomains)
        confirm_result = self.step2_confirm_matches(search_id, joined_matches)
        if not confirm_result.get("success"):
            time.sleep(RETRY_DELAY)
            confirm_result = self.step2_confirm_matches(search_id, joined_matches)
            if not confirm_result.get("success"):
                result["error"] = confirm_result.get("error")
                return result

        volume = confirm_result.get("volume", "N/A")
        if not volume or str(volume).strip().upper() == "N/A":
            time.sleep(RETRY_DELAY)
            confirm_retry = self.step2_confirm_matches(search_id, joined_matches)
            if confirm_retry.get("success"):
                volume = confirm_retry.get("volume", "N/A")
        result["volume"] = volume

        esp_result = self.step3_get_esps(search_id)
        if not esp_result.get("success") or not esp_result.get("esps"):
            time.sleep(RETRY_DELAY)
            esp_result = self.step3_get_esps(search_id)
        if esp_result.get("success") and esp_result.get("esps"):
            result["esps"] = esp_result["esps"]

        result["success"] = True
        return result


def _new_client(api_key: Optional[str]) -> EverestClient:
    clean_key = _clean_key(api_key)
    if not clean_key:
        raise ValueError("缺少 API Key。请传入 api_key 或设置环境变量 EVEREST_API_KEY")
    return EverestClient(clean_key)


def _format_v1_view(raw: Dict[str, Any]) -> Dict[str, Any]:
    esps = raw.get("esps", [])
    esp_names = [str(item.get("esp", "")) for item in esps if isinstance(item, dict) and item.get("esp")]
    esp_ratios = [_format_percent(item.get("percent", 0)) for item in esps if isinstance(item, dict) and item.get("esp")]
    subdomains = _normalize_matches(raw.get("subdomains", []))
    return {
        "domain": raw.get("domain", ""),
        "ESP": "; ".join(esp_names) if esp_names else "无ESP数据",
        "ESP占比": "; ".join(esp_ratios),
        "完整匹配域名": "; ".join(subdomains) if subdomains else "无匹配域名",
        "发信量估计": raw.get("volume", "N/A") or "N/A",
    }


def _format_v2_view(raw: Dict[str, Any]) -> Dict[str, Any]:
    esps = raw.get("esps", [])
    esp_names = [str(item.get("esp", "")) for item in esps if isinstance(item, dict) and item.get("esp")]
    esp_ratios = [_format_percent(item.get("percent", 0)) for item in esps if isinstance(item, dict) and item.get("esp")]
    subdomains = _normalize_matches(raw.get("subdomains", []))
    filtered_out = _normalize_matches(raw.get("filtered_out", []))

    if raw.get("error") and raw.get("error") != "NO_VALID_SUBDOMAINS":
        return {
            "domain": raw.get("domain", ""),
            "ESP(仅子域名)": f"ERROR: {raw.get('error')}",
            "ESP占比": "",
            "有效子域名": "",
            "被过滤域名(不同顶级域)": "",
            "发信量估计(仅子域名)": "",
        }

    return {
        "domain": raw.get("domain", ""),
        "ESP(仅子域名)": "; ".join(esp_names) if esp_names else "无ESP数据",
        "ESP占比": "; ".join(esp_ratios),
        "有效子域名": "; ".join(subdomains) if subdomains else "无有效子域名",
        "被过滤域名(不同顶级域)": "; ".join(filtered_out),
        "发信量估计(仅子域名)": raw.get("volume", "N/A") or "N/A",
    }


if FastMCP is not None:
    mcp = FastMCP("everest_api")

    @mcp.tool()
    def everest_query_v1(domain: str, api_key: Optional[str] = None) -> Dict[str, Any]:
        """
        查询完整域名聚合结果（v1）。

        何时使用：
        - 你想看「完整匹配域名集合」的聚合结果（不做不同顶级域过滤）。
        - 你需要快速评估目标域名整体的发信量与 ESP 分布。

        关键行为：
        - 使用 search -> confirm -> esps 三步流程。
        - 会自动重试关键步骤一次（网络抖动/空结果容错）。
        - `api_key` 传参优先；不传则使用 `EVEREST_API_KEY`。

        参数：
        - domain: 主域名，例如 example.com
        - api_key: 可选，调用方临时覆盖服务端环境变量

        返回结构：
        - mode: 固定 "v1"
        - success: 是否成功
        - error: 错误码或 None
        - view: 面向业务展示的字段（ESP/ESP占比/完整匹配域名/发信量估计）
        - raw: 原始结构化结果（含 subdomains/esps/volume）
        """
        client = _new_client(api_key)
        raw = client.query_v1(domain)
        return {
            "mode": "v1",
            "success": raw.get("success", False),
            "error": raw.get("error"),
            "view": _format_v1_view(raw),
            "raw": raw,
        }

    @mcp.tool()
    def everest_query_v2(domain: str, api_key: Optional[str] = None) -> Dict[str, Any]:
        """
        查询仅子域名结果（v2），包含不同顶级域过滤信息。

        何时使用（推荐默认）：
        - 你希望只统计 base_domain 及其真正子域名（*.base_domain）。
        - 你不希望把 baidu.jp 这类不同顶级域混入 baidu.com 结果。
        - 你要导出标准五列：
          ESP(仅子域名) / ESP占比 / 有效子域名 / 被过滤域名(不同顶级域) / 发信量估计(仅子域名)

        关键行为：
        - search 后会先做子域名过滤，再 confirm + esps。
        - 若无有效子域名，返回 success=True 且 error=NO_VALID_SUBDOMAINS（空数据但非硬失败）。
        - `api_key` 传参优先；不传则使用 `EVEREST_API_KEY`。

        参数：
        - domain: 主域名，例如 example.com
        - api_key: 可选，调用方临时覆盖服务端环境变量

        返回结构：
        - mode: 固定 "v2"
        - success: 是否成功
        - error: 错误码或 None
        - view: 五列业务字段
        - raw: 原始结构化结果（含 subdomains/filtered_out/esps/volume）
        """
        client = _new_client(api_key)
        raw = client.query_v2(domain)
        return {
            "mode": "v2",
            "success": raw.get("success", False),
            "error": raw.get("error"),
            "view": _format_v2_view(raw),
            "raw": raw,
        }

    @mcp.tool()
    def everest_query_batch(
        domains: Union[str, List[str]],
        mode: str = "v2",
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        批量查询域名，适合表格化处理或批量打分。

        何时使用：
        - 一次处理多个域名，减少客户端循环调用复杂度。
        - 需要统一统计成功数/失败数，并保留逐条结果。

        参数：
        - domains: 支持列表，或字符串（可用逗号/分号/空格/换行分隔）
        - mode: "v1" 或 "v2"
          - v1: 完整匹配域名聚合
          - v2: 仅子域名并过滤不同顶级域（推荐）
        - api_key: 可选，调用方临时覆盖服务端环境变量

        返回结构：
        - mode / total / success_count / failed_count
        - results: 每个域名的 success/error/view/raw

        示例（语义）：
        - domains="a.com,b.com c.com" mode="v2"
        - domains=["a.com","b.com"] mode="v1"
        """
        mode = mode.lower().strip()
        if mode not in {"v1", "v2"}:
            raise ValueError("mode 必须是 v1 或 v2")

        client = _new_client(api_key)
        domain_list = _parse_domains(domains)
        results: List[Dict[str, Any]] = []
        success_count = 0

        for domain in domain_list:
            raw = client.query_v1(domain) if mode == "v1" else client.query_v2(domain)
            view = _format_v1_view(raw) if mode == "v1" else _format_v2_view(raw)
            if raw.get("success"):
                success_count += 1
            results.append(
                {
                    "domain": domain,
                    "success": raw.get("success", False),
                    "error": raw.get("error"),
                    "view": view,
                    "raw": raw,
                }
            )

        return {
            "mode": mode,
            "total": len(domain_list),
            "success_count": success_count,
            "failed_count": len(domain_list) - success_count,
            "results": results,
        }
else:
    mcp = None


def main() -> None:
    if mcp is None:
        raise SystemExit("未安装 mcp。请先执行: pip install mcp requests")
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
