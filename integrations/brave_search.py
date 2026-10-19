from __future__ import annotations
__version__ = "1.1.0"

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


def _location_address(item: Dict[str, Any]) -> str:
    address = item.get("postal_address")
    if not isinstance(address, dict):
        return ""
    display_address = _text(address.get("displayAddress") or address.get("display_address"))
    if display_address:
        return display_address
    return ", ".join(
        value
        for value in (
            _text(address.get("streetAddress") or address.get("street_address")),
            _text(address.get("addressLocality") or address.get("address_locality")),
            _text(address.get("addressRegion") or address.get("address_region")),
            _text(address.get("postalCode") or address.get("postal_code")),
            _text(address.get("country")),
        )
        if value
    )


def _location_hours(item: Dict[str, Any]) -> str:
    opening_hours = item.get("opening_hours")
    if not isinstance(opening_hours, dict):
        return ""
    current_day = opening_hours.get("current_day")
    if not isinstance(current_day, list):
        return ""
    periods: List[str] = []
    for period in current_day:
        if not isinstance(period, dict):
            continue
        day = _text(period.get("full_name") or period.get("abbr_name"))
        opens = _text(period.get("opens"))
        closes = _text(period.get("closes"))
        if opens and closes:
            periods.append(f"{day + ' ' if day else ''}{opens}-{closes}")
        elif opens:
            periods.append(f"{day + ' ' if day else ''}opens {opens}")
        elif day:
            periods.append(day)
    return ", ".join(periods)


def _location_snippet(item: Dict[str, Any]) -> str:
    parts: List[str] = []
    description = _text(item.get("description"))
    address = _location_address(item)
    hours = _location_hours(item)
    contact = item.get("contact")
    telephone = _text(contact.get("telephone")) if isinstance(contact, dict) else ""
    rating = item.get("rating")
    if isinstance(rating, dict):
        rating_value = _text(rating.get("ratingValue") or rating.get("rating_value"))
        best_rating = _text(rating.get("bestRating") or rating.get("best_rating"))
        review_count = _text(rating.get("reviewCount") or rating.get("review_count"))
    else:
        rating_value = ""
        best_rating = ""
        review_count = ""

    if description:
        parts.append(description)
    if address:
        parts.append(f"Address: {address}")
    if hours:
        parts.append(f"Hours today: {hours}")
    if telephone:
        parts.append(f"Phone: {telephone}")
    if rating_value:
        rating_text = rating_value + (f"/{best_rating}" if best_rating else "")
        if review_count:
            rating_text += f" ({review_count} reviews)"
        parts.append(f"Rating: {rating_text}")
    return _truncate_snippet(". ".join(parts))


def _normalize_result(item: Dict[str, Any], *, location: bool = False) -> Optional[Dict[str, Any]]:
    url = _text(item.get("url") or (item.get("provider_url") if location else ""))
    if not url:
        return None
    return {
        "title": _text(item.get("title")) or url,
        "url": url,
        "snippet": _location_snippet(item) if location else _truncate_snippet(item.get("description")),
        "display_url": _display_url(url),
    }


def _success(
    *,
    query: str,
    start_index: int,
    max_results: int,
    results: List[Dict[str, Any]],
    site_val: str,
    original_query: str,
    corrected_query: str,
    cleaned_query: str,
    spellcheck_off: Optional[bool],
    has_more: bool,
) -> Dict[str, Any]:
    effective_query = corrected_query or cleaned_query or original_query or query
    spellcheck_applied = bool(corrected_query and corrected_query.casefold() != original_query.casefold())
    next_start = start_index + max_results if has_more and results else None
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "brave_search",
        "provider_label": "Brave Search",
        "query": query,
        "original_query": original_query or query,
        "corrected_query": corrected_query or None,
        "cleaned_query": cleaned_query or None,
        "effective_query": effective_query,
        "spellcheck_applied": spellcheck_applied,
        "spellcheck_off": spellcheck_off,
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
        "offset": min(9, max(0, (start_index - 1) // max_results)),
        "safesearch": "strict" if safe_mode == "active" else "off",
        "spellcheck": "true",
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
    if payload.get("error"):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            error_message = _text(error_payload.get("detail") or error_payload.get("message") or error_payload.get("code"))
        else:
            error_message = _text(error_payload)
        return _error(error_message or "Unknown Brave Search error.")

    query_payload = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    original_query = _text(query_payload.get("original")) or q
    corrected_query = _text(query_payload.get("altered"))
    cleaned_query = _text(query_payload.get("cleaned"))
    spellcheck_off_value = query_payload.get("spellcheck_off")
    spellcheck_off = spellcheck_off_value if isinstance(spellcheck_off_value, bool) else None
    location_payload = payload.get("locations") if isinstance(payload.get("locations"), dict) else {}
    web_payload = payload.get("web") if isinstance(payload.get("web"), dict) else {}
    results: List[Dict[str, Any]] = []
    seen_urls = set()
    location_rows = location_payload.get("results") if isinstance(location_payload.get("results"), list) else []
    web_rows = web_payload.get("results") if isinstance(web_payload.get("results"), list) else []
    for rows, is_location in ((location_rows, True), (web_rows, False)):
        for item in rows:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_result(item, location=is_location)
            if not normalized or normalized["url"] in seen_urls:
                continue
            seen_urls.add(normalized["url"])
            results.append(normalized)
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    more_results_available = query_payload.get("more_results_available")
    if isinstance(more_results_available, bool):
        has_more = more_results_available
    else:
        has_more = len(location_rows) + len(web_rows) > len(results) or len(web_rows) >= max_results
    if int(params["offset"]) >= 9:
        has_more = False
    return _success(
        query=q,
        start_index=start_index,
        max_results=max_results,
        results=results,
        site_val=site_val,
        original_query=original_query,
        corrected_query=corrected_query,
        cleaned_query=cleaned_query,
        spellcheck_off=spellcheck_off,
        has_more=has_more,
    )


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported Brave Search action: {action_id}")
    query = _text((payload or {}).get("query")) or "tater search test"
    result = integration_web_search(query, num_results=1, timeout_sec=BRAVE_SEARCH_DEFAULT_TIMEOUT_SECONDS)
    if not result.get("ok"):
        raise RuntimeError(_text(result.get("error")) or "Brave Search test failed.")
    return {"ok": True, "message": "Brave Search returned a result.", "count": result.get("count", 0)}
