"""SRM Portal Network & Endpoint Discovery Tool.

Opens https://dld.srmist.edu.in, intercepts network requests/responses,
records navigation, XHR, and Fetch traffic, strips all sensitive tokens
or authentication headers, and outputs sanitized analysis artifacts.

Purpose:
Determine whether SRM exposes usable direct HTTP/REST endpoints to minimize
Playwright browser automation.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import async_playwright, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("srm_discovery")

SENSITIVE_PARAM_NAMES = {
    "password", "passwd", "pass", "pwd", "secret", "token", "auth",
    "access_token", "refresh_token", "api_key", "session", "sessionid",
    "jsessionid", "phpsessid", "code", "client_secret"
}


def sanitize_url(url: str) -> str:
    """Strip sensitive query parameter values from URLs."""
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        params = parse_qs(parsed.query, keep_blank_values=True)
        sanitized_params = {}
        for k, v in params.items():
            if any(s in k.lower() for s in SENSITIVE_PARAM_NAMES):
                sanitized_params[k] = ["[REDACTED]"]
            else:
                sanitized_params[k] = v
        new_query = urlencode(sanitized_params, doseq=True)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))
    except Exception:
        return url


async def run_discovery(
    target_url: str = "https://dld.srmist.edu.in",
    artifacts_dir: str = "artifacts",
    timeout_ms: int = 30000,
    headless: bool = True
) -> Dict[str, Any]:
    """Execute Playwright network inspection and produce sanitized discovery report."""
    output_dir = Path(artifacts_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "srm_discovery.json"
    md_path = output_dir / "srm_discovery_report.md"

    logger.info("Starting SRM Discovery on target: %s", target_url)

    captured_requests: Dict[str, Dict[str, Any]] = {}
    navigation_records: List[Dict[str, Any]] = []
    xhr_fetch_records: List[Dict[str, Any]] = []
    other_records: List[Dict[str, Any]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        def on_request(req: Request):
            req_id = id(req)
            sanitized_req_url = sanitize_url(req.url)
            entry = {
                "url": sanitized_req_url,
                "method": req.method,
                "resource_type": req.resource_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status_code": None,
                "content_type": None,
            }
            captured_requests[req_id] = entry

        def on_response(res: Response):
            req_id = id(res.request)
            if req_id in captured_requests:
                captured_requests[req_id]["status_code"] = res.status
                headers = res.headers
                content_type = headers.get("content-type", "")
                # Clean content-type
                captured_requests[req_id]["content_type"] = content_type.split(";")[0].strip()

        page.on("request", on_request)
        page.on("response", on_response)

        initial_url = target_url
        page_title = ""
        final_url = ""
        navigation_error = None

        try:
            logger.info("Navigating to %s (timeout: %d ms)...", target_url, timeout_ms)
            response = await page.goto(target_url, wait_until="load", timeout=timeout_ms)
            # Give short window for subsequent background XHR/fetch requests
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                logger.info("Network idle wait timed out, proceeding with captured traffic.")
            
            final_url = page.url
            page_title = await page.title()
            logger.info("Loaded page: '%s' -> '%s'", page_title, final_url)

        except Exception as exc:
            navigation_error = str(exc)
            final_url = page.url if page else target_url
            logger.warning("Navigation encountered notice/error: %s", exc)

        # Categorize requests
        for item in captured_requests.values():
            res_type = item.get("resource_type")
            if res_type == "document":
                navigation_records.append(item)
            elif res_type in ("xhr", "fetch"):
                xhr_fetch_records.append(item)
            else:
                other_records.append(item)

        # Identify candidate API endpoints
        api_candidates = []
        for item in xhr_fetch_records:
            url = item["url"]
            if url not in api_candidates:
                api_candidates.append(url)

        report_data = {
            "initial_url": initial_url,
            "final_url": final_url,
            "page_title": page_title,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "navigation_error": navigation_error,
            "statistics": {
                "total_requests": len(captured_requests),
                "navigation_requests": len(navigation_records),
                "xhr_fetch_requests": len(xhr_fetch_records),
                "asset_requests": len(other_records),
            },
            "candidate_api_endpoints": api_candidates,
            "navigation_requests": navigation_records,
            "xhr_fetch_requests": xhr_fetch_records,
            "sanitization_notice": (
                "All cookies, authorization headers, passwords, and sensitive tokens "
                "have been excluded from this report."
            ),
        }

        # Write JSON artifact
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)
        logger.info("Saved JSON discovery artifact to %s", json_path)

        # Write Markdown summary artifact
        md_content = generate_markdown_summary(report_data)
        md_path.write_text(md_content, encoding="utf-8")
        logger.info("Saved Markdown discovery artifact to %s", md_path)

        await context.close()
        await browser.close()

    return report_data


def generate_markdown_summary(data: Dict[str, Any]) -> str:
    stats = data["statistics"]
    lines = [
        "# SRM Portal Discovery & Network Analysis Report",
        "",
        f"**Target URL:** `{data['initial_url']}`  ",
        f"**Final Resolved URL:** `{data['final_url']}`  ",
        f"**Page Title:** `{data['page_title']}`  ",
        f"**Timestamp:** `{data['captured_at']}`  ",
        "",
        "## Traffic Summary",
        "",
        f"- **Total Intercepted Requests:** {stats['total_requests']}",
        f"- **Page Navigations / Documents:** {stats['navigation_requests']}",
        f"- **XHR / Fetch (Potential API Requests):** {stats['xhr_fetch_requests']}",
        f"- **Static Assets (CSS, JS, Fonts, Images):** {stats['asset_requests']}",
        "",
        "## Candidate Backend / API Endpoints (XHR & Fetch)",
        "",
    ]

    if data["candidate_api_endpoints"]:
        lines.append("| HTTP Method | Endpoint URL | Status Code | Content-Type |")
        lines.append("|---|---|---|---|")
        for req in data["xhr_fetch_requests"]:
            status = req.get("status_code") or "N/A"
            ctype = req.get("content_type") or "N/A"
            lines.append(f"| `{req['method']}` | `{req['url']}` | `{status}` | `{ctype}` |")
    else:
        lines.append("*No XHR or Fetch requests detected during initial page load.*")
        lines.append("*(The initial portal load may be server-side rendered or multi-step authenticated).*")

    lines.extend([
        "",
        "## Navigation History",
        "",
        "| Method | URL | Status Code | Content-Type |",
        "|---|---|---|---|",
    ])
    for req in data["navigation_requests"]:
        status = req.get("status_code") or "N/A"
        ctype = req.get("content_type") or "N/A"
        lines.append(f"| `{req['method']}` | `{req['url']}` | `{status}` | `{ctype}` |")

    lines.extend([
        "",
        "## Architectural Evaluation for SRM Automation",
        "",
        "1. **Direct HTTP Viability:**",
        "   - If dedicated JSON/REST endpoints are present, `SRMHttpClient` will invoke them directly.",
        "   - If pages are rendered via session-bound state or CSRF tokens, `SRMBrowserClient` serves as the headless fallback.",
        "2. **Security & Sanitization Compliance:**",
        "   - Zero credentials, tokens, session cookies, or authorization headers were written to disk.",
        "",
        "---",
        f"*Report auto-generated by SRM Automator Discovery on {data['captured_at']}*"
    ])

    return "\n".join(lines)


def main():
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://dld.srmist.edu.in"
    asyncio.run(run_discovery(target_url=url))


if __name__ == "__main__":
    main()
