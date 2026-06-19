#!/usr/bin/env python3
"""
check_blogs.py

CLI tool that verifies whether B2B SaaS companies have a real, active blog
or resources section on their website.

Input:  CSV with at least 'Company Name' and 'Website' columns
Output: CSV with additional verdict columns:
        - Has Content
        - Blog URL
        - Reason
        - Evidence

Verdicts:
    PASS          - Clear active blog with multiple recent articles
    WEAK_PASS     - Found something blog-like but signals are weak
    NEWS_ONLY     - Only has /news or /press (FAIL per ICP rules)
    NO_BLOG       - No blog or resources section found
    SITE_ERROR    - Couldn't reach the site or got blocked
    CHECK_MANUAL  - Heavy JavaScript site, needs human review
"""

import argparse
import csv
import re
import sys
import threading
import time
import urllib.parse
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =============================================================================
# CONFIGURATION - TUNE THESE CONSTANTS AS NEEDED
# =============================================================================

BLOG_PATHS: List[str] = [
    "/blog",
    "/blogs",
    "/resources",
    "/insights",
    "/articles",
    "/learn",
    "/library",
    "/posts",
]

NEWS_PATHS: List[str] = [
    "/news",
    "/newsroom",
    "/press",
    "/media",
]

HOME_BLOG_KEYWORDS: List[str] = [
    "blog",
    "resources",
    "insights",
    "articles",
    "learn",
]

PASS_MIN_DATES: int = 3
PASS_MIN_POST_LINKS: int = 3
PASS_MIN_MIN_READ: int = 2
PASS_MIN_ARTICLES: int = 3
WEAK_MIN_SIGNALS: int = 1

DEFAULT_WORKERS: int = 10
DEFAULT_TIMEOUT: int = 10
MAX_RETRIES: int = 3
BACKOFF_FACTOR: float = 0.5

FLUSH_EVERY: int = 10

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_HEADERS: Dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

DATE_REGEXES: List[re.Pattern] = [
    re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}[,.]?\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[,.]?\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{2}/\d{2}/\d{4}\b"),
]

MIN_READ_REGEX: re.Pattern = re.compile(
    r"\b\d+\s*(?:min|minute)s?\s*(?:read|lecture|de lectura)?\b",
    re.IGNORECASE,
)

POST_LINK_REGEX: re.Pattern = re.compile(
    r"^/(?:blog|blogs|post|posts|article|articles|insight|insights|resource|resources|learn|library|guide|guides)/[a-zA-Z0-9\-_]+/?$"
)

PLACEHOLDER_TERMS: List[str] = [
    "coming soon",
    "under construction",
    "stay tuned",
    "launching soon",
    "be right back",
    "page not found",
    "404 not found",
    "error 404",
]

GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

VERDICT_COLORS: Dict[str, str] = {
    "PASS": GREEN,
    "WEAK_PASS": YELLOW,
    "NEWS_ONLY": CYAN,
    "NO_BLOG": RED,
    "SITE_ERROR": MAGENTA,
    "CHECK_MANUAL": BLUE,
}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Signals:
    """Container for blog-like signals extracted from a page."""
    article_count: int = 0
    date_count: int = 0
    min_read_count: int = 0
    post_link_count: int = 0
    unique_post_links: List[str] = field(default_factory=list)


@dataclass
class VerdictResult:
    """Result of evaluating a single company."""
    company: str
    verdict: str
    blog_url: str = ""
    reason: str = ""
    evidence: str = ""


# =============================================================================
# URL HANDLING
# =============================================================================

def normalize_url(raw: str) -> str:
    """Normalize a website string into a valid http(s) URL.

    - Strips whitespace
    - Adds https:// if no scheme is present
    - Removes trailing slash when the path is empty

    Args:
        raw: Raw website value from the CSV.

    Returns:
        Normalized URL string ready for requests.
    """
    raw = raw.strip().lower()
    if not raw:
        return ""

    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"

    parsed = urllib.parse.urlparse(raw)
    if parsed.path == "/":
        raw = raw.rstrip("/")

    return raw


def strip_www(netloc: str) -> str:
    """Remove the www. prefix from a netloc for domain comparison."""
    if netloc.startswith("www."):
        return netloc[4:]
    return netloc


def same_root_domain(a: str, b: str) -> bool:
    """Return True if two netlocs belong to the same root domain."""
    a = strip_www(a)
    b = strip_www(b)
    return a == b or a.endswith(f".{b}") or b.endswith(f".{a}")


# =============================================================================
# SESSION & NETWORKING
# =============================================================================

_thread_local = threading.local()


def create_session() -> requests.Session:
    """Create a requests Session tuned for browser-like behavior and retries.

    Returns:
        Configured requests.Session.
    """
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=2,
        pool_maxsize=10,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def get_session() -> requests.Session:
    """Return a thread-local requests Session.

    Sessions are not fully thread-safe, so each worker thread keeps its own.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = create_session()
        _thread_local.session = session
    return session


def fetch_page(
    session: requests.Session,
    url: str,
    timeout: int,
    allow_insecure: bool = False,
) -> Tuple[int, str]:
    """Fetch a single page and return its HTTP status and HTML body.

    Handles SSL errors by optionally retrying with verify=False.

    Args:
        session: Configured requests Session.
        url: Target URL.
        timeout: Per-request timeout in seconds.
        allow_insecure: If True, disables SSL verification.

    Returns:
        Tuple of (status_code, html_text). status_code 0 indicates a
        network-level failure.
    """
    try:
        response = session.get(
            url,
            timeout=timeout,
            verify=not allow_insecure,
            allow_redirects=True,
        )
        return response.status_code, response.text

    except requests.exceptions.SSLError:
        if not allow_insecure:
            return fetch_page(session, url, timeout, allow_insecure=True)
        return 0, "ssl_error"

    except requests.exceptions.Timeout:
        return 0, "timeout"

    except requests.exceptions.RequestException as exc:
        return 0, f"request_error:{exc}"


# =============================================================================
# HTML SIGNAL EXTRACTION
# =============================================================================

def is_likely_empty_page(html: str) -> bool:
    """Detect pages that returned 200 but have essentially no usable HTML.

    Heavy JS sites often return a near-empty shell.

    Args:
        html: Raw HTML text.

    Returns:
        True if the page looks like a JS-rendered shell.
    """
    if not html or len(html.strip()) < 500:
        return True
    soup = BeautifulSoup(html, "html.parser")
    if len(soup.find_all(True)) < 20:
        return True
    return False


def is_placeholder_page(html: str) -> bool:
    """Detect 'coming soon' or similar placeholder pages.

    Args:
        html: Raw HTML text.

    Returns:
        True if the page appears to be a placeholder.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
    return any(term in text for term in PLACEHOLDER_TERMS)


def strip_noisy_elements(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove <nav>, <footer>, <header> to avoid counting menu links/articles.

    Args:
        soup: Parsed BeautifulSoup object.

    Returns:
        Same object with noisy elements removed.
    """
    for tag_name in ("nav", "footer", "header"):
        for tag in soup.find_all(tag_name):
            tag.decompose()
    return soup


def extract_dates(text: str) -> List[str]:
    """Extract date strings from text using multiple human-readable patterns.

    Args:
        text: Visible page text.

    Returns:
        List of matched date strings.
    """
    found: List[str] = []
    for regex in DATE_REGEXES:
        found.extend(regex.findall(text))
    return found


def extract_min_reads(text: str) -> List[str]:
    """Extract 'X min read' style strings.

    Args:
        text: Visible page text.

    Returns:
        List of matched min-read strings.
    """
    return MIN_READ_REGEX.findall(text)


def extract_post_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    """Extract unique internal links that look like blog/article posts.

    Args:
        soup: Parsed BeautifulSoup object.
        base_url: Base URL used to resolve relative links.

    Returns:
        List of unique post-style hrefs.
    """
    parsed_base = urllib.parse.urlparse(base_url)
    seen: set = set()
    links: List[str] = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        full = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(full)

        if parsed.netloc != parsed_base.netloc:
            continue

        path = parsed.path
        if POST_LINK_REGEX.match(path) and path not in seen:
            seen.add(path)
            links.append(full)

    return links


def extract_signals(html: str, base_url: str) -> Signals:
    """Extract all blog-like signals from an HTML page.

    Args:
        html: Raw HTML text.
        base_url: URL the HTML came from.

    Returns:
        Signals dataclass with counts and example links.
    """
    soup = BeautifulSoup(html, "html.parser")
    soup = strip_noisy_elements(soup)

    article_count = len(soup.find_all("article"))
    visible_text = soup.get_text(" ", strip=True)

    date_count = len(extract_dates(visible_text))
    min_read_count = len(extract_min_reads(visible_text))
    post_links = extract_post_links(soup, base_url)
    post_link_count = len(post_links)

    return Signals(
        article_count=article_count,
        date_count=date_count,
        min_read_count=min_read_count,
        post_link_count=post_link_count,
        unique_post_links=post_links[:5],
    )


# =============================================================================
# VERDICT LOGIC
# =============================================================================

def evaluate_signals(signals: Signals) -> Tuple[str, str]:
    """Map extracted signals to a page-level verdict.

    PASS thresholds:
        - 3+ post links, OR
        - 3+ dates, OR
        - 2+ min read, OR
        - 3+ <article> tags

    WEAK_PASS:
        - 1-2 total signals present

    NO:
        - No blog-like signals

    Args:
        signals: Extracted signals.

    Returns:
        Tuple of (verdict, evidence_string).
    """
    parts: List[str] = []
    if signals.article_count:
        parts.append(f"{signals.article_count} article tags")
    if signals.date_count:
        parts.append(f"{signals.date_count} dates")
    if signals.min_read_count:
        parts.append(f"{signals.min_read_count} min-read")
    if signals.post_link_count:
        parts.append(f"{signals.post_link_count} post links")
        if signals.unique_post_links:
            parts.append(f"e.g. {signals.unique_post_links[0]}")

    evidence = "; ".join(parts) if parts else "no blog signals"
    has_signals = sum(
        [
            signals.article_count > 0,
            signals.date_count > 0,
            signals.min_read_count > 0,
            signals.post_link_count > 0,
        ]
    )

    strong = (
        signals.post_link_count >= PASS_MIN_POST_LINKS
        or signals.date_count >= PASS_MIN_DATES
        or signals.min_read_count >= PASS_MIN_MIN_READ
        or signals.article_count >= PASS_MIN_ARTICLES
    )

    if strong:
        return "PASS", evidence
    elif has_signals >= WEAK_MIN_SIGNALS:
        return "WEAK_PASS", evidence
    else:
        return "NO", evidence


def evaluate_blog_path(
    session: requests.Session,
    base_url: str,
    path: str,
    timeout: int,
) -> Optional[VerdictResult]:
    """Try a single candidate blog path and evaluate it.

    Args:
        session: Configured requests Session.
        base_url: Normalized base domain.
        path: Candidate path like /blog.
        timeout: Per-request timeout in seconds.

    Returns:
        VerdictResult if the path yielded a usable page, else None.
    """
    url = urllib.parse.urljoin(base_url, path)
    status, html = fetch_page(session, url, timeout)

    if status != 200 or not html:
        return None

    if is_likely_empty_page(html):
        return VerdictResult(
            company="",
            verdict="CHECK_MANUAL",
            blog_url=url,
            reason="Page loaded but appears to be JS-rendered or empty",
            evidence="Very little HTML returned; likely requires browser rendering",
        )

    if is_placeholder_page(html):
        return VerdictResult(
            company="",
            verdict="NO_BLOG",
            blog_url=url,
            reason="Path exists but shows a placeholder page",
            evidence="Detected 'coming soon' / 'under construction' language",
        )

    signals = extract_signals(html, url)
    verdict, evidence = evaluate_signals(signals)

    if verdict == "PASS":
        return VerdictResult(
            company="",
            verdict="PASS",
            blog_url=url,
            reason=f"Strong blog signals on {path}",
            evidence=evidence,
        )

    if verdict == "WEAK_PASS":
        return VerdictResult(
            company="",
            verdict="WEAK_PASS",
            blog_url=url,
            reason=f"Weak blog signals on {path}",
            evidence=evidence,
        )

    return None


def evaluate_news_path(
    session: requests.Session,
    base_url: str,
    timeout: int,
) -> Tuple[bool, str]:
    """Check whether the site has a newsroom/press section.

    Args:
        session: Configured requests Session.
        base_url: Normalized base domain.
        timeout: Per-request timeout in seconds.

    Returns:
        Tuple of (found, url). url is empty if no news section found.
    """
    for path in NEWS_PATHS:
        url = urllib.parse.urljoin(base_url, path)
        status, html = fetch_page(session, url, timeout)
        if status == 200 and html and not is_likely_empty_page(html):
            if not is_placeholder_page(html):
                return True, url
    return False, ""


def hunt_blog_link_on_homepage(
    session: requests.Session,
    base_url: str,
    timeout: int,
) -> Optional[VerdictResult]:
    """As a last resort, scan the homepage for links named Blog/Resources/etc.

    Args:
        session: Configured requests Session.
        base_url: Normalized base domain.
        timeout: Per-request timeout in seconds.

    Returns:
        VerdictResult with CHECK_MANUAL if a relevant link is found, else None.
    """
    status, html = fetch_page(session, base_url, timeout)
    if status != 200 or not html:
        return None

    if is_likely_empty_page(html):
        return VerdictResult(
            company="",
            verdict="CHECK_MANUAL",
            blog_url=base_url,
            reason="Homepage appears JS-rendered; manual review needed",
            evidence="Empty or near-empty HTML returned by GET",
        )

    soup = BeautifulSoup(html, "html.parser")
    soup = strip_noisy_elements(soup)

    parsed_base = urllib.parse.urlparse(base_url)

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        text = a_tag.get_text(strip=True).lower()

        full = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(full)

        matched_keyword = any(
            kw in text or kw in href.lower() for kw in HOME_BLOG_KEYWORDS
        )

        if matched_keyword and same_root_domain(parsed.netloc, parsed_base.netloc):
            return VerdictResult(
                company="",
                verdict="CHECK_MANUAL",
                blog_url=full,
                reason="Homepage links to possible blog/resources section",
                evidence=f"Found link: {full}",
            )

    return None


def check_resources_fallback(
    session: requests.Session,
    base_url: str,
    timeout: int,
) -> Optional[VerdictResult]:
    """Last-chance check for an educational /resources page.

    Some sites host real content under /resources but lack the typical blog
    signals (dates, min-read, post links). This fallback catches them by
    looking for substantial text content before giving up.

    Args:
        session: Configured requests Session.
        base_url: Normalized base domain.
        timeout: Per-request timeout in seconds.

    Returns:
        VerdictResult if /resources has real content, else None.
    """
    for path in ("/resources", "/resource"):
        url = urllib.parse.urljoin(base_url, path)
        status, html = fetch_page(session, url, timeout)
        if status != 200 or not html:
            continue

        if is_likely_empty_page(html):
            return VerdictResult(
                company="",
                verdict="CHECK_MANUAL",
                blog_url=url,
                reason="Resources section detected but appears JS-rendered",
                evidence="Page loaded but HTML is too light; may need manual review",
            )

        if is_placeholder_page(html):
            continue

        soup = BeautifulSoup(html, "html.parser")
        soup = strip_noisy_elements(soup)
        visible_text = soup.get_text(" ", strip=True)

        if len(visible_text) >= 1500:
            return VerdictResult(
                company="",
                verdict="WEAK_PASS",
                blog_url=url,
                reason="Resources section found with educational content",
                evidence=f"{len(visible_text)} chars of content on {path}",
            )

    return None


# =============================================================================
# COMPANY CHECK ORCHESTRATION
# =============================================================================

def find_blog_for_company(
    session: requests.Session,
    base_url: str,
    timeout: int,
) -> VerdictResult:
    """Determine the blog/resources verdict for one company.

    Tries primary blog paths in order, bailing on PASS. Falls back to news
    paths, then homepage link hunting.

    Args:
        session: Configured requests Session.
        base_url: Normalized company website.
        timeout: Per-request timeout in seconds.

    Returns:
        VerdictResult with the best verdict found.
    """
    best_weak: Optional[VerdictResult] = None

    for path in BLOG_PATHS:
        result = evaluate_blog_path(session, base_url, path, timeout)
        if result is None:
            continue

        if result.verdict == "PASS":
            return result

        if result.verdict == "WEAK_PASS" and best_weak is None:
            best_weak = result

    if best_weak is not None:
        return best_weak

    resources_fallback = check_resources_fallback(session, base_url, timeout)
    if resources_fallback is not None:
        return resources_fallback

    has_news, news_url = evaluate_news_path(session, base_url, timeout)
    if has_news:
        return VerdictResult(
            company="",
            verdict="NEWS_ONLY",
            blog_url=news_url,
            reason="Only newsroom/press section found, no real blog",
            evidence="News path returned 200 with content",
        )

    homepage_result = hunt_blog_link_on_homepage(session, base_url, timeout)
    if homepage_result is not None:
        return homepage_result

    return VerdictResult(
        company="",
        verdict="NO_BLOG",
        blog_url="",
        reason="No blog, resources, or news section detected",
        evidence="No relevant paths or homepage links found",
    )


def check_company(row: Dict[str, str], timeout: int) -> VerdictResult:
    """Evaluate a single CSV row.

    Args:
        row: Dictionary representing one CSV row.
        timeout: Per-request timeout in seconds.

    Returns:
        VerdictResult populated with company details.
    """
    company = row.get("Company Name", "Unknown").strip()
    raw_website = row.get("Website", "").strip()
    base_url = normalize_url(raw_website)

    if not base_url:
        return VerdictResult(
            company=company,
            verdict="SITE_ERROR",
            blog_url="",
            reason="Missing or invalid website URL",
            evidence="No website value provided in CSV",
        )

    session = get_session()

    try:
        result = find_blog_for_company(session, base_url, timeout)
    except Exception as exc:
        result = VerdictResult(
            company=company,
            verdict="SITE_ERROR",
            blog_url=base_url,
            reason="Unexpected error during evaluation",
            evidence=str(exc),
        )

    result.company = company
    return result


# =============================================================================
# CSV I/O
# =============================================================================

def read_companies(path: str) -> List[Dict[str, str]]:
    """Read companies from a CSV file.

    Args:
        path: Input CSV path.

    Returns:
        List of row dictionaries.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def ensure_output_headers(path: str, fieldnames: List[str]) -> None:
    """Write the CSV header if the file does not yet exist.

    Args:
        path: Output CSV path.
        fieldnames: Final column order.
    """
    import os

    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()


def append_results(
    path: str,
    fieldnames: List[str],
    rows: List[Dict[str, str]],
) -> None:
    """Append a batch of result rows to the output CSV.

    Args:
        path: Output CSV path.
        fieldnames: Final column order.
        rows: List of result dictionaries.
    """
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerows(rows)


# =============================================================================
# REPORTING
# =============================================================================

def verdict_color(verdict: str) -> str:
    """Return ANSI color prefix for a verdict."""
    return VERDICT_COLORS.get(verdict, "")


def print_live_result(result: VerdictResult) -> None:
    """Print one colored line per company as results arrive."""
    color = verdict_color(result.verdict)
    print(
        f"{color}[{result.verdict}]{RESET} {BOLD}{result.company}{RESET} "
        f"-> {result.blog_url or 'n/a'} ({result.reason})"
    )


def print_summary(
    results: List[VerdictResult],
    elapsed: float,
    errored_urls: List[str],
) -> None:
    """Print a summary table of verdict counts and timing.

    Args:
        results: All VerdictResult objects.
        elapsed: Total elapsed seconds.
        errored_urls: List of URLs that errored.
    """
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1

    print("\n" + "=" * 60)
    print(f"{BOLD}SUMMARY{RESET} ({len(results)} companies in {elapsed:.1f}s)")
    print("=" * 60)

    order = ["PASS", "WEAK_PASS", "NEWS_ONLY", "NO_BLOG", "CHECK_MANUAL", "SITE_ERROR"]
    for verdict in order:
        count = counts.get(verdict, 0)
        color = verdict_color(verdict)
        print(f"  {color}{verdict:<14}{RESET} {count:>4}")

    print("-" * 60)
    print(f"  {'TOTAL':<14} {len(results):>4}")

    if errored_urls:
        print("\nSites that raised network/unexpected errors:")
        for url in errored_urls:
            print(f"  - {url}")


# =============================================================================
# MAIN
# =============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Verify whether B2B SaaS companies have an active blog or resources section.",
    )
    parser.add_argument("--input", "-i", required=True, help="Input CSV path")
    parser.add_argument("--output", "-o", required=True, help="Output CSV path")
    parser.add_argument(
        "--workers", "-w", type=int, default=DEFAULT_WORKERS, help="Parallel workers"
    )
    parser.add_argument(
        "--timeout", "-t", type=int, default=DEFAULT_TIMEOUT, help="Per-request timeout (seconds)"
    )
    return parser.parse_args()


def result_to_row(
    original: Dict[str, str],
    result: VerdictResult,
    extra_fields: List[str],
) -> Dict[str, str]:
    """Merge original CSV row with new verdict columns.

    Args:
        original: Original CSV row.
        result: VerdictResult for the row.
        extra_fields: Names of the new columns.

    Returns:
        Combined dictionary.
    """
    row = dict(original)
    row.update(
        {
            extra_fields[0]: result.verdict,
            extra_fields[1]: result.blog_url,
            extra_fields[2]: result.reason,
            extra_fields[3]: result.evidence,
        }
    )
    return row


def main() -> int:
    """CLI entry point."""
    args = parse_args()

    companies = read_companies(args.input)
    if not companies:
        print("No rows found in input CSV.", file=sys.stderr)
        return 1

    original_fields = list(companies[0].keys())
    extra_fields = ["Has Content", "Blog URL", "Reason", "Evidence"]
    output_fields = original_fields + extra_fields

    ensure_output_headers(args.output, output_fields)

    results: List[VerdictResult] = []
    errored_urls: List[str] = []
    buffer: List[Dict[str, str]] = []

    start_time = time.time()

    with tqdm(
        total=len(companies),
        desc="Checking companies",
        unit="co",
        ncols=80,
    ) as pbar:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_row = {
                executor.submit(check_company, row, args.timeout): row
                for row in companies
            }

            for future in as_completed(future_to_row):
                row = future_to_row[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = VerdictResult(
                        company=row.get("Company Name", "Unknown").strip(),
                        verdict="SITE_ERROR",
                        blog_url=row.get("Website", "").strip(),
                        reason="Thread-level failure",
                        evidence=str(exc),
                    )

                results.append(result)
                if result.verdict == "SITE_ERROR" and result.blog_url:
                    errored_urls.append(result.blog_url)

                print_live_result(result)

                buffer.append(result_to_row(row, result, extra_fields))

                if len(buffer) >= FLUSH_EVERY:
                    append_results(args.output, output_fields, buffer)
                    buffer.clear()

                pbar.update(1)

    if buffer:
        append_results(args.output, output_fields, buffer)

    elapsed = time.time() - start_time
    print_summary(results, elapsed, errored_urls)

    return 0


if __name__ == "__main__":
    sys.exit(main())
