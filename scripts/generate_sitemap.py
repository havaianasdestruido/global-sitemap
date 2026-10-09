#!/usr/bin/env python3
"""Build an XML sitemap for https://havaianasdestruido.github.io/ (all subpages).

Discovers every page of the site without any credentials:

1. Crawls the live site breadth-first from --site-url, following every
   same-host link (so subpages and GitHub Pages project sites under
   https://havaianasdestruido.github.io/<repo>/ are all found).
2. Seeds extra URLs from the helper index
   https://havaianasdestruido.github.io/gh-pages/URL.html (the weekly
   "Repo websites" list).
3. Seeds URLs from the public GitHub API
   (GET /users/<user>/repos) for every repo with GitHub Pages enabled —
   no token needed.  If the GITHUB_TOKEN environment variable is set it
   is used only to raise the API rate limit.

Only dependency-free Python 3.9+ stdlib.  Requests have bounded
timeouts with retries for transient failures, and output files are
replaced atomically so a crash never leaves a partial sitemap behind.

Usage:
    python scripts/generate_sitemap.py
    python scripts/generate_sitemap.py --output sitemap.xml --robots-out robots.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = "global-sitemap-generator/1.0 (+https://github.com/havaianasdestruido/global-sitemap)"

DEFAULT_SITE_URL = "https://havaianasdestruido.github.io/"
DEFAULT_HELPER_URL = "https://havaianasdestruido.github.io/gh-pages/URL.html"
DEFAULT_GITHUB_USER = "havaianasdestruido"

# 50,000 URLs / 50MB per sitemap file is the protocol limit; stay under it.
SITEMAP_PART_LIMIT = 45_000
# Cap the amount of HTML read per page so a runaway page can't eat memory.
MAX_PAGE_BYTES = 2 * 1024 * 1024

# Files that are never HTML pages: assets, fonts, media, archives, docs...
ASSET_EXTENSIONS = {
    ".7z", ".aac", ".ai", ".apk", ".avi", ".avif", ".bak", ".bat", ".bin",
    ".bmp", ".bz2", ".class", ".css", ".csv", ".dll", ".dmg", ".doc", ".docx",
    ".eot", ".eps", ".exe", ".flac", ".gif", ".gz", ".ico", ".iso", ".jar",
    ".jpeg", ".jpg", ".js", ".json", ".jsonl", ".map", ".m4a", ".m4v", ".mid",
    ".midi", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".otf", ".pdf",
    ".png", ".ppt", ".pptx", ".psd", ".rar", ".rss", ".svg", ".tar", ".tgz",
    ".tif", ".tiff", ".tsv", ".ttf", ".txt", ".wasm", ".wav", ".webm",
    ".webmanifest", ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".xml",
    ".xz", ".zip",
}

# Absolute-URL extraction, good enough for both the HTML helper page and
# the markdown URL.MD source behind it (tables use <https://...> autolinks).
URL_RE = re.compile(r"https?://[^\s<>\"'`\)\]]+", re.IGNORECASE)
TRAILING_PUNCT_RE = re.compile(r"[.,;:!?]+$")

# Upgraded to https when the target site is https; kept as-is for local
# testing against http:// dev servers.
PREFER_HTTPS = True


class LinkParser(HTMLParser):
    """Collect hrefs from <a>/<area>, the first <base href>, and robots meta."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.base_href: str | None = None
        self.noindex = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attr_map = {k.lower(): v for k, v in attrs if v is not None}
        if tag == "base" and self.base_href is None and attr_map.get("href"):
            self.base_href = attr_map["href"]
        elif tag in ("a", "area"):
            href = attr_map.get("href")
            if href:
                self.hrefs.append(href)
        elif tag == "meta":
            name = (attr_map.get("name") or "").strip().lower()
            content = (attr_map.get("content") or "").lower()
            if name == "robots" and "noindex" in content:
                self.noindex = True

    handle_startendtag = handle_starttag


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def normalize_url(url: str, base: str | None = None, keep_query: bool = False) -> str | None:
    """Return a canonical http(s) URL, or None if unusable."""
    if base:
        url = urllib.parse.urljoin(base, url)
    url = TRAILING_PUNCT_RE.sub("", url.strip())
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.hostname or ""
    if not host:
        return None
    host = host.lower()
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    path = parts.path or "/"
    # Collapse "." / ".." / "//" without touching trailing-slash meaning.
    trailing_slash = path.endswith("/") or path.endswith("/.") or path.endswith("/..")
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    path = "/" + "/".join(segments)
    if trailing_slash and not path.endswith("/"):
        path += "/"
    if not path:
        path = "/"

    # /sub/index.html and /sub/ are the same page on GitHub Pages.
    for index_name in ("/index.html", "/index.htm"):
        if path.lower().endswith(index_name):
            path = path[: -len(index_name)] + "/"
            break

    query = parts.query if keep_query else ""
    scheme = "https" if PREFER_HTTPS else parts.scheme
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def is_asset_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path
    suffix = Path(path).suffix.lower()
    return suffix in ASSET_EXTENSIONS


def fetch(url: str, timeout: float, retries: int = 2):
    """GET a URL with bounded timeouts and retries.

    Returns (status, final_url, content_type, body, last_modified_header).
    status is None when the request never produced an HTTP response.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,*/*;q=0.5",
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_PAGE_BYTES)
                return (
                    getattr(response, "status", 200) or 200,
                    response.geturl(),
                    response.headers.get("Content-Type", ""),
                    body,
                    response.headers.get("Last-Modified"),
                )
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < retries:
                last_error = error
                time.sleep(1.0 * (attempt + 1))
                continue
            return error.code, url, "", b"", None
        except Exception as error:  # URLError, timeout, ConnectionError, ...
            last_error = error
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
    log(f"  ! fetch failed: {url} ({last_error})")
    return None, url, "", b"", None


def decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def parse_page(html_text: str, page_url: str) -> tuple[list[str], bool]:
    """Return (normalized links on the page, page has robots noindex)."""
    parser = LinkParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass
    base = normalize_url(parser.base_href, page_url) if parser.base_href else page_url
    links: list[str] = []
    for href in parser.hrefs:
        normalized = normalize_url(href, base)
        if normalized:
            links.append(normalized)
    return links, parser.noindex


def extract_urls_any_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in URL_RE.finditer(text):
        normalized = normalize_url(match.group(0))
        if normalized:
            urls.append(normalized)
    return urls


def header_lastmod(last_modified: str | None) -> str | None:
    """Convert an HTTP Last-Modified header to a W3C date (YYYY-MM-DD)."""
    if not last_modified:
        return None
    try:
        parsed = parsedate_to_datetime(last_modified)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def github_pages_seeds(user: str, token: str | None) -> list[str]:
    """Seed URLs for every public repo with GitHub Pages enabled.

    Uses the public /users/<user>/repos endpoint, so no token is required;
    GITHUB_TOKEN is used only when present, to raise the rate limit.
    """
    host = f"{user}.github.io".lower()
    seeds: set[str] = set()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for page in range(1, 20):
        api_url = f"https://api.github.com/users/{user}/repos?per_page=100&page={page}"
        try:
            request = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                repos = json.load(response)
        except Exception as error:
            log(f"  ! GitHub API discovery failed on page {page}: {error}")
            break
        if not repos:
            break
        for repo in repos:
            if repo.get("archived"):
                continue
            name = repo.get("name") or ""
            if repo.get("has_pages") and name:
                if name.lower() == host:
                    seeds.add(f"https://{host}/")
                else:
                    seeds.add(f"https://{host}/{name}/")
            homepage = (repo.get("homepage") or "").strip()
            if homepage:
                normalized = normalize_url(homepage)
                if normalized and (urllib.parse.urlsplit(normalized).hostname or "").lower() == host:
                    seeds.add(normalized)
    return sorted(seeds)


def helper_seeds(helper_url: str, site_host: str, timeout: float) -> list[str]:
    """Seed URLs scraped from the helper index (URL.html / URL.MD)."""
    status, _final, content_type, body, _lastmod = fetch(helper_url, timeout)
    if status != 200 or not body:
        log(f"  ! helper page unavailable ({status}): {helper_url}")
        return []
    text = decode_body(body, content_type)
    seeds: set[str] = set()
    for url in extract_urls_any_text(text):
        if (urllib.parse.urlsplit(url).hostname or "").lower() == site_host:
            seeds.add(url)
    return sorted(seeds)


def parse_robots(text: str):
    """Minimal robots.txt parser (Disallow/Allow per user-agent + Sitemap lines)."""
    sitemaps: list[str] = []
    rules: dict[str, list[tuple[bool, str]]] = {}
    current_agents: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = line.split(":", 1)
        field = field.strip().lower()
        value = value.strip()
        if field == "sitemap":
            if value:
                sitemaps.append(value)
        elif field == "user-agent":
            current_agents.append(value.lower())
        elif field in ("allow", "disallow") and current_agents:
            for agent in current_agents:
                rules.setdefault(agent, []).append((field == "allow", value))
        else:
            current_agents = []
    return rules, sitemaps


def robots_allows(rules: dict[str, list[tuple[bool, str]]], url: str) -> bool:
    """Longest-match robots.txt check for our crawler user-agent."""
    if not rules:
        return True
    path = urllib.parse.urlsplit(url).path or "/"
    candidates: list[str] = []
    for agent, agent_rules in rules.items():
        if agent in ("*", USER_AGENT.lower()) or USER_AGENT.lower().startswith(agent):
            candidates.extend(agent_rules)
    if not candidates:
        return True
    decision = True
    best_length = -1
    for allow, pattern in candidates:
        if not pattern:
            continue
        prefix = pattern.rstrip("$")
        if pattern.endswith("$"):
            matched = path == prefix
        else:
            matched = path.startswith(prefix)
        if matched and len(prefix) > best_length:
            best_length = len(prefix)
            decision = allow
    return decision


def xml_escape(url: str) -> str:
    return url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def atomic_write(path: Path, content: str) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def write_sitemap(entries: list[tuple[str, str | None]], out_path: Path, site_url: str) -> list[Path]:
    """Write a sitemap.xml (urlset) or a sitemap index plus part files."""
    written: list[Path] = []

    def urlset_xml(chunk: list[tuple[str, str | None]]) -> str:
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for url, lastmod in chunk:
            lines.append("  <url>")
            lines.append(f"    <loc>{xml_escape(url)}</loc>")
            if lastmod:
                lines.append(f"    <lastmod>{lastmod}</lastmod>")
            lines.append("  </url>")
        lines.append("</urlset>")
        return "\n".join(lines) + "\n"

    if len(entries) <= SITEMAP_PART_LIMIT:
        atomic_write(out_path, urlset_xml(entries))
        written.append(out_path)
        return written

    part_names: list[str] = []
    for index, start in enumerate(range(0, len(entries), SITEMAP_PART_LIMIT), start=1):
        part_name = f"sitemap-{index}.xml"
        part_path = out_path.with_name(part_name)
        atomic_write(part_path, urlset_xml(entries[start : start + SITEMAP_PART_LIMIT]))
        written.append(part_path)
        part_names.append(part_name)

    index_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for part_name in part_names:
        part_url = urllib.parse.urljoin(site_url, part_name)
        index_lines.append("  <sitemap>")
        index_lines.append(f"    <loc>{xml_escape(part_url)}</loc>")
        index_lines.append(f"    <lastmod>{today}</lastmod>")
        index_lines.append("  </sitemap>")
    index_lines.append("</sitemapindex>")
    atomic_write(out_path, "\n".join(index_lines) + "\n")
    written.insert(0, out_path)
    return written


def write_robots(robots_path: Path, sitemap_url: str) -> None:
    atomic_write(
        robots_path,
        "User-agent: *\nAllow: /\n\nSitemap: " + sitemap_url + "\n",
    )


def crawl(
    seeds: list[str],
    site_host: str,
    max_depth: int,
    max_urls: int,
    delay: float,
    timeout: float,
    keep_query: bool,
    robots_rules: dict[str, list[tuple[bool, str]]] | None,
) -> dict[str, str | None]:
    """Breadth-first crawl. Returns {page_url: lastmod_or_None} for HTML pages."""
    queue: deque[tuple[str, int]] = deque()
    seen: set[str] = set()
    pages: dict[str, str | None] = {}

    def enqueue(url: str, depth: int) -> None:
        if len(seen) >= max_urls:
            return
        if (urllib.parse.urlsplit(url).hostname or "").lower() != site_host:
            return
        if is_asset_url(url) or url in seen:
            return
        if robots_rules is not None and not robots_allows(robots_rules, url):
            return
        seen.add(url)
        queue.append((url, depth))

    for seed in seeds:
        enqueue(seed, 0)

    while queue and len(pages) < max_urls:
        url, depth = queue.popleft()
        if delay:
            time.sleep(delay)
        status, final_url, content_type, body, last_modified = fetch(url, timeout)
        final = normalize_url(final_url, keep_query=keep_query) or url
        seen.add(final)  # don't re-queue a redirect target we just fetched
        if status != 200 or not body:
            continue
        ctype = (content_type or "").lower()
        path = urllib.parse.urlsplit(final).path.lower()
        looks_html = "html" in ctype or path.endswith((".html", ".htm", ".xhtml")) or path.endswith("/")
        if not looks_html and ctype:
            continue
        text = decode_body(body, content_type)
        links, is_noindex = parse_page(text, final)
        if not is_noindex:
            pages.setdefault(final, header_lastmod(last_modified))
        if depth < max_depth:
            for link in links:
                enqueue(link, depth + 1)
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site-url", default=DEFAULT_SITE_URL, help="site root to crawl (default: %(default)s)")
    parser.add_argument("--output", default="sitemap.xml", help="sitemap output path (default: %(default)s)")
    parser.add_argument("--robots-out", default=None, help="also write a robots.txt pointing at the sitemap")
    parser.add_argument("--helper-url", default=DEFAULT_HELPER_URL, help="helper page listing site URLs")
    parser.add_argument("--no-helper", action="store_true", help="skip the helper page")
    parser.add_argument("--github-user", default=DEFAULT_GITHUB_USER, help="GitHub user for Pages discovery")
    parser.add_argument("--no-github", action="store_true", help="skip GitHub Pages discovery")
    parser.add_argument("--seed", action="append", default=[], help="extra seed URL (repeatable)")
    parser.add_argument("--max-depth", type=int, default=8, help="max link depth to follow (default: %(default)s)")
    parser.add_argument("--max-urls", type=int, default=45_000, help="max pages in the sitemap (default: %(default)s)")
    parser.add_argument("--delay", type=float, default=0.05, help="seconds between requests (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-request timeout seconds (default: %(default)s)")
    parser.add_argument("--ignore-robots", action="store_true", help="crawl even if robots.txt disallows")
    parser.add_argument("--keep-query", action="store_true", help="keep query strings instead of stripping them")
    args = parser.parse_args()

    global PREFER_HTTPS
    PREFER_HTTPS = urllib.parse.urlsplit(args.site_url).scheme.lower() != "http"
    site_url = normalize_url(args.site_url)
    if not site_url:
        log(f"error: invalid --site-url: {args.site_url}")
        return 2
    site_host = urllib.parse.urlsplit(site_url).hostname or ""
    out_path = Path(args.output)

    log(f"Crawling {site_url} (host {site_host})")

    seeds: set[str] = {site_url}

    if not args.no_helper:
        log("Seed 1/3: helper URL index")
        helper = helper_seeds(args.helper_url, site_host, args.timeout)
        log(f"  + {len(helper)} same-host seed(s) from {args.helper_url}")
        seeds.update(helper)
    else:
        log("Seed 1/3: helper URL index (skipped)")

    if not args.no_github and args.github_user:
        log("Seed 2/3: GitHub Pages repos (public API, no token needed)")
        github = github_pages_seeds(args.github_user, os.environ.get("GITHUB_TOKEN"))
        log(f"  + {len(github)} Pages seed(s) for user '{args.github_user}'")
        seeds.update(github)
    else:
        log("Seed 2/3: GitHub Pages repos (skipped)")

    log("Seed 3/3: explicit --seed URLs")
    for seed in args.seed:
        normalized = normalize_url(seed)
        if normalized:
            seeds.add(normalized)

    robots_rules: dict[str, list[tuple[bool, str]]] | None = None
    if not args.ignore_robots:
        robots_url = urllib.parse.urljoin(site_url, "/robots.txt")
        status, _final, _ctype, body, _lastmod = fetch(robots_url, args.timeout)
        if status == 200 and body:
            robots_rules, robots_sitemaps = parse_robots(decode_body(body, "text/plain"))
            for sitemap in robots_sitemaps:
                normalized = normalize_url(sitemap)
                if normalized and (urllib.parse.urlsplit(normalized).hostname or "").lower() == site_host:
                    seeds.add(normalized)
            log(f"robots.txt: honoring rules, {len(robots_sitemaps)} Sitemap line(s) added as seeds")
        else:
            robots_rules = {}
            log("robots.txt: not present, crawling everything")
    else:
        log("robots.txt: ignored by request")

    log(f"Crawling {len(seeds)} seed URL(s)...")
    pages = crawl(
        sorted(seeds),
        site_host,
        max_depth=args.max_depth,
        max_urls=args.max_urls,
        delay=args.delay,
        timeout=args.timeout,
        keep_query=args.keep_query,
        robots_rules=robots_rules,
    )

    if not pages:
        log("error: no HTML pages found — nothing to put in the sitemap")
        return 1

    entries = sorted(pages.items())
    written = write_sitemap(entries, out_path, site_url)
    for path in written:
        log(f"wrote {path}")

    if args.robots_out:
        sitemap_url = urllib.parse.urljoin(site_url, out_path.name)
        robots_path = Path(args.robots_out)
        write_robots(robots_path, sitemap_url)
        log(f"wrote {robots_path} (points at {sitemap_url})")

    print(f"Sitemap: {len(entries)} URL(s) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
