"""Web research through your own SearXNG (private meta-search, no account, no tracking), plus a page reader for the AI.
Pages are untrusted text, and the reader only fetches public internet addresses, never anything on your network."""

import html
import ipaddress
import re
import socket
from urllib.parse import urlparse

import requests

UA = "Mozilla/5.0 (compatible; Jarvis research)"


class SearchError(Exception):
    pass


def searxng_url(cfg: dict) -> str:
    return ((cfg.get("search") or {}).get("url") or "http://searxng:8080").rstrip("/")


def web_search(cfg: dict, query: str, n: int = 8) -> list[dict]:
    url = searxng_url(cfg)
    try:
        r = requests.get(url + "/search", params={"q": query, "format": "json", "safesearch": 1}, timeout=20, headers={"User-Agent": UA})
    except requests.RequestException as e:
        raise SearchError(f"Can't reach SearXNG at {url} ({type(e).__name__}). Is the search container running?")
    if r.status_code == 403:
        raise SearchError("SearXNG refused JSON results: add 'json' under search > formats in its settings.yml")
    if not r.ok:
        raise SearchError(f"SearXNG answered HTTP {r.status_code}")
    results = r.json().get("results") or []
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:300]} for x in results[:n]]


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def read_page(url: str, limit: int = 6000) -> str:
    """The readable text of a public web page."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise SearchError("Only http(s) web addresses can be read")
    if not _public(p.hostname):
        raise SearchError("That address is on a private network. The page reader only reads the public internet")
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA}, allow_redirects=False, stream=True)
        hops = 0
        while r.is_redirect and hops < 4:  # check every redirect too, so a public page can't bounce us into the home network
            nxt = requests.compat.urljoin(url, r.headers.get("Location", ""))
            if not _public(urlparse(nxt).hostname or ""):
                raise SearchError("That page redirects to a private network address")
            url, hops = nxt, hops + 1
            r = requests.get(url, timeout=20, headers={"User-Agent": UA}, allow_redirects=False, stream=True)
        raw = r.raw.read(2_000_000, decode_content=True)
    except requests.RequestException as e:
        raise SearchError(f"Couldn't load the page: {type(e).__name__}")
    ctype = r.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        raise SearchError(f"That isn't a web page ({ctype or 'unknown type'})")
    text = raw.decode(r.encoding or "utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|nav|footer|header|aside|noscript|svg)[^>]*>.*?</\1>", " ", text)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    head = f"{html.unescape(title.group(1).strip())}\n{url}\n\n" if title else f"{url}\n\n"
    return (head + text)[:limit]


def test(cfg: dict) -> str:
    res = web_search(cfg, "homelab")
    return f"SearXNG works: {len(res)} results for a test search" + (f", e.g. {res[0]['title'][:60]}" if res else "")
