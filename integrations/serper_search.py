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

SERPER_SEARCH_SETTINGS_KEY = "serper_search_settings"
SERPER_SEARCH_API_URL = "https://google.serper.dev/search"
SERPER_SEARCH_DEFAULT_TIMEOUT_SECONDS = 15
SERPER_SEARCH_MAX_RESPONSE_BYTES = int(os.getenv("TATER_WEB_SEARCH_MAX_RESPONSE_BYTES", "2000000"))
SERPER_SEARCH_MAX_SNIPPET_CHARS = int(os.getenv("TATER_WEB_SEARCH_MAX_SNIPPET_CHARS", "600"))

INTEGRATION = {
    "id": "serper_search",
    "name": "Serper",
    "description": "Adds Serper Google Search API as a downloadable web search provider for Tater.",
    "badge": "SP",
    "order": 13,
    "capabilities": ["web_search"],
    "fields": [
        {"key": "serper_api_key", "label": "Serper API Key", "type": "password", "default": ""},
    ],
    "actions": [
        {"id": "test", "label": "Test Search", "status": "Runs one Serper search query."},
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


def read_serper_search_settings() -> Dict[str, str]:
    current = _read_hash(SERPER_SEARCH_SETTINGS_KEY)
    try:
        legacy_api_key = redis_client.get("tater:web_search:serper_api_key")
    except Exception:
        legacy_api_key = ""
    api_key = (
        _text(current.get("SERPER_API_KEY") or current.get("serper_api_key"))
        or _text(legacy_api_key)
        or _text(os.getenv("TATER_WEB_SEARCH_SERPER_API_KEY") or os.getenv("SERPER_API_KEY"))
    )
    return {"SERPER_API_KEY": api_key}


def save_serper_search_settings(*, api_key: Any = None) -> Dict[str, str]:
    current = read_serper_search_settings()
    next_settings = {"SERPER_API_KEY": _text(current.get("SERPER_API_KEY") if api_key is None else api_key)}
    redis_client.hset(SERPER_SEARCH_SETTINGS_KEY, mapping=next_settings)
    return read_serper_search_settings()


def read_integration_settings() -> Dict[str, Any]:
    settings = read_serper_search_settings()
    return {"serper_api_key": settings.get("SERPER_API_KEY", "")}


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_serper_search_settings(api_key=(payload or {}).get("serper_api_key"))
    return {"serper_api_key": saved.get("SERPER_API_KEY", "")}


def integration_status() -> Dict[str, Any]:
    configured = bool(read_serper_search_settings().get("SERPER_API_KEY"))
    return {
        "configured": configured,
        "message": "Serper is configured." if configured else "Serper API key is required.",
    }


def _query_with_site(query: str, site: Any) -> str:
    site_val = _text(site)
    return f"site:{site_val} {query}" if site_val else query


def _truncate_snippet(value: Any) -> str:
    snippet = _text(value)
    if len(snippet) > SERPER_SEARCH_MAX_SNIPPET_CHARS:
        snippet = snippet[:SERPER_SEARCH_MAX_SNIPPET_CHARS].rstrip() + "..."
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
    total_results: Any = None,
) -> Dict[str, Any]:
    next_start = start_index + max_results if len(results) >= max_results else None
    try:
        total_results_int = int(total_results) if total_results is not None else None
    except Exception:
        total_results_int = None
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "serper_search",
        "provider_label": "Serper",
        "query": query,
        "start": start_index,
        "count": len(results),
        "num_results": max_results,
        "results": results,
        "site_filter": site_val or None,
        "search_time_sec": None,
        "total_results": total_results_int,
        "has_more": bool(next_start),
        "next_start": next_start,
    }


def _error(message: str, *, needs: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "tool": "search_web",
        "ok": False,
        "provider": "serper_search",
        "provider_label": "Serper",
        "error": message,
    }
    if needs:
        out["needs"] = needs
    return out


def _read_json(req: urllib.request.Request, *, timeout_val: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout_val) as resp:
            raw = resp.read(SERPER_SEARCH_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(1000).decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        return None, _error(f"Serper request failed ({exc.code}): {body or exc}")
    except Exception as exc:
        return None, _error(f"Serper search failed: {exc}")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None, _error("Invalid response from Serper.")
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
    timeout_sec: Any = SERPER_SEARCH_DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    del safe
    q = _text(query)
    if not q:
        return _error("query is required.")
    settings = read_serper_search_settings()
    if not settings.get("SERPER_API_KEY"):
        return _error(
            "Serper is not configured.",
            needs=["Set Serper API Key in the Serper integration settings."],
        )

    max_results = _bounded_int(num_results, default=5, minimum=1, maximum=10)
    start_index = _bounded_int(start, default=1, minimum=1, maximum=91)
    timeout_val = _bounded_int(timeout_sec, default=SERPER_SEARCH_DEFAULT_TIMEOUT_SECONDS, minimum=3, maximum=60)
    country_val = _text(country).lower()
    language_val = _text(language).lower()
    site_val = _text(site)

    body: Dict[str, Any] = {
        "q": _query_with_site(q, site_val),
        "num": max_results,
        "page": max(1, ((start_index - 1) // max(1, max_results)) + 1),
    }
    if country_val and re.fullmatch(r"[a-z]{2}", country_val):
        body["gl"] = country_val
    if language_val and re.fullmatch(r"[a-z]{2}", language_val):
        body["hl"] = language_val

    req = urllib.request.Request(
        SERPER_SEARCH_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Tater-AgentLab/1.0",
            "X-API-KEY": settings.get("SERPER_API_KEY") or "",
        },
        method="POST",
    )
    payload, error = _read_json(req, timeout_val=timeout_val)
    if error:
        return error

    results: List[Dict[str, Any]] = []
    for item in payload.get("organic") or []:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("link") or item.get("url"))
        if not url:
            continue
        results.append(
            {
                "title": _text(item.get("title")) or url,
                "url": url,
                "snippet": _truncate_snippet(item.get("snippet")),
                "display_url": _display_url(url),
            }
        )
    search_info = payload.get("searchInformation") if isinstance(payload.get("searchInformation"), dict) else {}
    return _success(
        query=q,
        start_index=start_index,
        max_results=max_results,
        results=results,
        site_val=site_val,
        total_results=search_info.get("totalResults") if isinstance(search_info, dict) else None,
    )


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported Serper action: {action_id}")
    query = _text((payload or {}).get("query")) or "tater search test"
    result = integration_web_search(query, num_results=1, timeout_sec=SERPER_SEARCH_DEFAULT_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise RuntimeError(_text(result.get("error")) or "Serper test failed.")
    return {"ok": True, "message": "Serper returned a result.", "count": result.get("count", 0)}
