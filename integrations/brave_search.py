from __future__ import annotations
__version__ = "1.0.0"

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from helpers import redis_client

BRAVE_SEARCH_SETTINGS_KEY = "brave_search_settings"
BRAVE_SEARCH_API_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_SEARCH_DEFAULT_TIMEOUT_SECONDS = 15
BRAVE_SEARCH_MAX_RESPONSE_BYTES = int(os.getenv("TATER_WEB_SEARCH_MAX_RESPONSE_BYTES", "2000000"))
BRAVE_SEARCH_MAX_SNIPPET_CHARS = int(os.getenv("TATER_WEB_SEARCH_MAX_SNIPPET_CHARS", "600"))

INTEGRATION = {
    "id": "brave_search",
    "name": "Brave Search",
    "description": "Adds Brave Search as a downloadable web search provider for Tater.",
    "badge": "BR",
    "order": 10,
    "capabilities": ["web_search"],
    "fields": [
        {"key": "brave_api_key", "label": "Brave Search API Key", "type": "password", "default": ""},
    ],
    "actions": [
        {"id": "test", "label": "Test Search", "status": "Runs one Brave Search query."},
    ],
}


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore").strip()
    return str(value or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(_text(value)))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _read_hash(key: str) -> Dict[str, Any]:
    try:
        raw = redis_client.hgetall(key) or {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def read_brave_search_settings() -> Dict[str, str]:
    current = _read_hash(BRAVE_SEARCH_SETTINGS_KEY)
    try:
        legacy_api_key = redis_client.get("tater:web_search:brave_api_key")
    except Exception:
        legacy_api_key = ""
    api_key = (
        _text(current.get("BRAVE_API_KEY") or current.get("brave_api_key"))
        or _text(legacy_api_key)
        or _text(os.getenv("TATER_WEB_SEARCH_BRAVE_API_KEY") or os.getenv("BRAVE_SEARCH_API_KEY"))
    )
    return {"BRAVE_API_KEY": api_key}


def save_brave_search_settings(*, api_key: Any = None) -> Dict[str, str]:
    current = read_brave_search_settings()
    next_settings = {"BRAVE_API_KEY": _text(current.get("BRAVE_API_KEY") if api_key is None else api_key)}
    redis_client.hset(BRAVE_SEARCH_SETTINGS_KEY, mapping=next_settings)
    return read_brave_search_settings()


def read_integration_settings() -> Dict[str, Any]:
    settings = read_brave_search_settings()
    return {"brave_api_key": settings.get("BRAVE_API_KEY", "")}


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_brave_search_settings(api_key=(payload or {}).get("brave_api_key"))
    return {"brave_api_key": saved.get("BRAVE_API_KEY", "")}


def integration_status() -> Dict[str, Any]:
    configured = bool(read_brave_search_settings().get("BRAVE_API_KEY"))
    return {
        "configured": configured,
        "message": "Brave Search is configured." if configured else "Brave Search API key is required.",
    }


def _query_with_site(query: str, site: Any) -> str:
    site_val = _text(site)
    return f"site:{site_val} {query}" if site_val else query


def _truncate_snippet(value: Any) -> str:
    snippet = _text(value)
    if len(snippet) > BRAVE_SEARCH_MAX_SNIPPET_CHARS:
        snippet = snippet[:BRAVE_SEARCH_MAX_SNIPPET_CHARS].rstrip() + "..."
    return snippet


def _display_url(url: str) -> str:
    parsed = urllib.parse.urlparse(_text(url))
    return parsed.netloc or _text(url)


def _success(
    *,
    query: str,
    start_index: int,
    max_results: int,
    results: List[Dict[str, Any]],
    site_val: str,
) -> Dict[str, Any]:
    next_start = start_index + max_results if len(results) >= max_results else None
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "brave_search",
        "provider_label": "Brave Search",
        "query": query,
        "start": start_index,
        "count": len(results),
        "num_results": max_results,
        "results": results,
        "site_filter": site_val or None,
        "search_time_sec": None,
        "total_results": None,
        "has_more": bool(next_start),
        "next_start": next_start,
    }


def _error(message: str, *, needs: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "tool": "search_web",
        "ok": False,
        "provider": "brave_search",
        "provider_label": "Brave Search",
        "error": message,
    }
    if needs:
        out["needs"] = needs
    return out


def _read_json(req: urllib.request.Request, *, timeout_val: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout_val) as resp:
            raw = resp.read(BRAVE_SEARCH_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(1000).decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        return None, _error(f"Brave Search request failed ({exc.code}): {body or exc}")
    except Exception as exc:
        return None, _error(f"Brave Search failed: {exc}")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None, _error("Invalid response from Brave Search.")
    return payload if isinstance(payload, dict) else {"data": payload}, None


def integration_web_search(
    query: Any,
    *,
    num_results: Any = 5,
    start: Any = 1,
    site: Any = None,
    safe: Any = "active",
    country: Any = None,
    language: Any = None,
    timeout_sec: Any = BRAVE_SEARCH_DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    q = _text(query)
    if not q:
        return _error("query is required.")
    settings = read_brave_search_settings()
    if not settings.get("BRAVE_API_KEY"):
        return _error(
            "Brave Search is not configured.",
            needs=["Set Brave Search API Key in the Brave Search integration settings."],
        )

    max_results = _bounded_int(num_results, default=5, minimum=1, maximum=10)
    start_index = _bounded_int(start, default=1, minimum=1, maximum=91)
    timeout_val = _bounded_int(timeout_sec, default=BRAVE_SEARCH_DEFAULT_TIMEOUT_SECONDS, minimum=3, maximum=60)
    safe_mode = _text(safe).lower()
    if safe_mode not in {"active", "off"}:
        safe_mode = "active"
    country_val = _text(country).lower()
    language_val = _text(language).lower()
    site_val = _text(site)

    params: Dict[str, Any] = {
        "q": _query_with_site(q, site_val),
        "count": max_results,
        "offset": max(0, start_index - 1),
        "safesearch": "strict" if safe_mode == "active" else "off",
    }
    if country_val and re.fullmatch(r"[a-z]{2}", country_val):
        params["country"] = country_val
    if language_val and re.fullmatch(r"[a-z]{2}", language_val):
        params["search_lang"] = language_val

    req = urllib.request.Request(
        BRAVE_SEARCH_API_URL + "?" + urllib.parse.urlencode(params, doseq=True),
        headers={
            "Accept": "application/json",
            "User-Agent": "Tater-AgentLab/1.0",
            "X-Subscription-Token": settings.get("BRAVE_API_KEY") or "",
        },
    )
    payload, error = _read_json(req, timeout_val=timeout_val)
    if error:
        return error

    web_payload = payload.get("web") if isinstance(payload.get("web"), dict) else {}
    results: List[Dict[str, Any]] = []
    for item in web_payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        if not url:
            continue
        results.append(
            {
                "title": _text(item.get("title")) or url,
                "url": url,
                "snippet": _truncate_snippet(item.get("description")),
                "display_url": _display_url(url),
            }
        )
    return _success(query=q, start_index=start_index, max_results=max_results, results=results, site_val=site_val)


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported Brave Search action: {action_id}")
    query = _text((payload or {}).get("query")) or "tater search test"
    result = integration_web_search(query, num_results=1, timeout_sec=BRAVE_SEARCH_DEFAULT_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise RuntimeError(_text(result.get("error")) or "Brave Search test failed.")
    return {"ok": True, "message": "Brave Search returned a result.", "count": result.get("count", 0)}
