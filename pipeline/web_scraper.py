"""
Web Scraper - Download manhwa chapters from various sites.
Supports:
  - MangaDex (via official REST API — recommended, no Cloudflare)
  - Madara-based WordPress sites (manhuaus, manhwatop, etc.)
  - VioletScans and generic manga/manhwa readers
Downloads go into: manhwa_download/<manga-name>/Chapter_00001/
"""
import os
import re
import time
import json
import tempfile
import threading
import requests
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

# Default download root - now using new organized structure
DOWNLOAD_ROOT = config.MANHWA_DOWNLOAD_DIR

# ═══════════════════════════════════════════════════════════
#  MangaDex API  (https://api.mangadex.org)
#  Free, no auth required, no Cloudflare — use this first!
# ═══════════════════════════════════════════════════════════
MANGADEX_API = "https://api.mangadex.org"
MANGADEX_COVERS = "https://uploads.mangadex.org/covers"
MANGADEX_DOMAIN = "mangadex.org"


def _is_mangadex_url(url):
    """Return True if the URL is a MangaDex manga or chapter URL."""
    return MANGADEX_DOMAIN in urlparse(url).netloc


def _mangadex_extract_id(url):
    """Extract manga UUID from a MangaDex URL like
    https://mangadex.org/title/<uuid>/slug"""
    m = re.search(r'/title/([0-9a-f-]{36})', url)
    return m.group(1) if m else None


def _mangadex_analyze(url):
    """Fetch manga metadata and chapter list from MangaDex API."""
    result = {
        'manga_name': '', 'manga_slug': '', 'total_chapters': 0,
        'chapters': [], 'cover_url': None, 'description': '',
        'site': MANGADEX_DOMAIN, 'error': None
    }

    manga_id = _mangadex_extract_id(url)
    if not manga_id:
        result['error'] = "Could not extract manga ID from MangaDex URL."
        return result

    # ── Manga metadata ──────────────────────────────────────
    try:
        r = _session.get(f"{MANGADEX_API}/manga/{manga_id}",
                         params={'includes[]': ['cover_art', 'author']},
                         timeout=15)
        r.raise_for_status()
        data = r.json().get('data', {})
    except Exception as e:
        result['error'] = f"MangaDex API error: {e}"
        return result

    attrs = data.get('attributes', {})
    titles = attrs.get('title', {})
    # Prefer English title, fall back to first available
    title = titles.get('en') or next(iter(titles.values()), 'Unknown')
    result['manga_name'] = title
    result['manga_slug'] = manga_id

    desc = attrs.get('description', {})
    result['description'] = desc.get('en') or next(iter(desc.values()), '')

    # Cover image
    for rel in data.get('relationships', []):
        if rel['type'] == 'cover_art':
            fname = rel.get('attributes', {}).get('fileName', '')
            if fname:
                result['cover_url'] = f"{MANGADEX_COVERS}/{manga_id}/{fname}.256.jpg"
            break

    # ── Chapter list ────────────────────────────────────────
    # Try English first, fall back to all available languages
    chapters = []
    for lang_filter in [['en'], None]:
        chapters = []
        offset = 0
        limit = 100
        while True:
            params = {
                'order[chapter]': 'asc',
                'limit': limit,
                'offset': offset,
                'includes[]': ['scanlation_group'],
            }
            if lang_filter:
                params['translatedLanguage[]'] = lang_filter
            try:
                r = _session.get(f"{MANGADEX_API}/manga/{manga_id}/feed",
                                 params=params, timeout=15)
                r.raise_for_status()
                body = r.json()
            except Exception as e:
                result['error'] = f"MangaDex chapter feed error: {e}"
                break

            items = body.get('data', [])
            if not items:
                break

            for ch in items:
                ch_attrs = ch.get('attributes', {})
                ch_num_str = ch_attrs.get('chapter') or ''
                if not ch_num_str:
                    continue
                try:
                    ch_num = float(ch_num_str)
                    if ch_num == int(ch_num):
                        ch_num = int(ch_num)
                except ValueError:
                    continue

                pub_date = (ch_attrs.get('publishAt') or '')[:10]
                chapters.append({
                    'number': ch_num,
                    'title': f"Chapter {ch_num}",
                    'url': f"https://mangadex.org/chapter/{ch['id']}",
                    'date': pub_date,
                    '_md_chapter_id': ch['id'],  # internal: used by downloader
                })

            total = body.get('total', 0)
            offset += limit
            if offset >= total:
                break

        if chapters:  # Found something — stop trying other language filters
            break

    # Deduplicate by chapter number (keep first)
    seen = set()
    unique = []
    for ch in chapters:
        if ch['number'] not in seen:
            seen.add(ch['number'])
            unique.append(ch)

    result['chapters'] = unique
    result['total_chapters'] = len(unique)
    if not unique:
        result['error'] = "No chapters found on MangaDex for this title."
    return result


def _mangadex_get_chapter_images(chapter_id):
    """Fetch page image URLs for a MangaDex chapter via the at-home API."""
    try:
        r = _session.get(f"{MANGADEX_API}/at-home/server/{chapter_id}", timeout=15)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        print(f"[MangaDex] at-home API error for {chapter_id}: {e}")
        return []

    base_url = body.get('baseUrl', '')
    ch_data = body.get('chapter', {})
    hash_ = ch_data.get('hash', '')
    pages = ch_data.get('data', [])  # lossless; use dataSaver for compressed
    return [f"{base_url}/data/{hash_}/{page}" for page in pages]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# ── Session Setup ────────────────────────────────────────────────
# Priority: curl_cffi (best TLS fingerprint) > cloudscraper > plain requests
_use_curl_cffi = False
try:
    from curl_cffi.requests import Session as CurlSession
    _session = CurlSession(impersonate="chrome131")
    _session.headers.update(HEADERS)
    _use_curl_cffi = True
    print("[Scraper] Using curl_cffi (Chrome TLS fingerprint impersonation)")
except ImportError:
    try:
        import cloudscraper
        _session = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'darwin', 'desktop': True},
        )
        _session.headers.update(HEADERS)
        print("[Scraper] Using cloudscraper (JS challenge bypass)")
    except ImportError:
        _session = requests.Session()
        _session.headers.update(HEADERS)
        print("[Scraper] Using plain requests (Cloudflare sites may fail)")


# Track domains where we've already solved Cloudflare
_cf_solved_domains = set()

# Persistent DrissionPage browser for CF-protected sites
_drission_browser = None

# Real reason the last fetch failed (so the UI can show it instead of
# always blaming Cloudflare)
_last_fetch_error = None

# Chrome/Chromium locations. DrissionPage's default browser_path is the bare
# name "chrome", which is NOT on PATH on macOS — so it must be set explicitly.
_CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _find_chrome():
    """Return a usable Chrome/Chromium binary path, or None."""
    for path in _CHROME_PATHS:
        if os.path.isfile(path):
            return path
    from shutil import which
    return which("google-chrome") or which("chromium") or which("chrome")


def _get_drission_browser():
    """Get or create a persistent DrissionPage browser session."""
    global _drission_browser, _last_fetch_error
    if _drission_browser is not None:
        try:
            _ = _drission_browser.title  # Check if still alive
            return _drission_browser
        except Exception:
            _drission_browser = None

    try:
        from DrissionPage import ChromiumPage, ChromiumOptions
    except ImportError:
        _last_fetch_error = "DrissionPage is not installed. Run: pip install DrissionPage"
        print(f"[Scraper] {_last_fetch_error}")
        return None

    chrome_path = _find_chrome()
    if not chrome_path:
        _last_fetch_error = (
            "Google Chrome was not found. Install Chrome from https://google.com/chrome "
            "— it is required to bypass Cloudflare."
        )
        print(f"[Scraper] {_last_fetch_error}")
        return None

    # Use a dedicated profile so we never collide with the user's own open
    # Chrome window (DrissionPage aborts if the profile dir is already in use).
    profile_dir = os.path.join(tempfile.gettempdir(), "mrm_scraper_chrome")

    co = ChromiumOptions()
    co.set_browser_path(chrome_path)
    co.set_user_data_path(profile_dir)
    co.set_argument("--window-size", "1280,800")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.auto_port()  # Avoid conflict with port 9222

    try:
        _drission_browser = ChromiumPage(co)
        print(f"[Scraper] Launched browser: {chrome_path}")
        return _drission_browser
    except Exception as e:
        _last_fetch_error = f"Could not launch Chrome: {e}"
        print(f"[Scraper] {_last_fetch_error}")
        _drission_browser = None
        return None


def close_drission_browser():
    """Close the persistent DrissionPage browser (call on shutdown)."""
    global _drission_browser
    if _drission_browser:
        try:
            _drission_browser.quit()
        except Exception:
            pass
        _drission_browser = None


def _get_soup(url, referer=None):
    """Fetch a page and return BeautifulSoup object.
    1. If domain is known CF-protected → use persistent DrissionPage browser
    2. Otherwise try curl_cffi / requests first (fast, no browser)
    3. If Cloudflare blocks → switch to DrissionPage for this domain
    """
    global _last_fetch_error
    _last_fetch_error = None
    domain = urlparse(url).netloc

    # If this domain is known to be CF-protected, go straight to browser
    if domain in _cf_solved_domains:
        return _fetch_with_drission(url, referer)

    # ── Attempt 1: Direct HTTP (fast path) ──────────────────
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    for attempt in range(2):
        try:
            if _use_curl_cffi:
                r = _session.get(url, headers=headers, timeout=30, allow_redirects=True)
            else:
                if referer:
                    _session.headers["Referer"] = referer
                r = _session.get(url, timeout=30)

            content = r.content if hasattr(r, 'content') else r.text.encode()
            text = content.decode('utf-8', errors='ignore')

            # Check if Cloudflare blocked us
            is_cf = r.status_code in (403, 503) and (
                'Just a moment' in text or 'cf-browser-verification' in text
                or '_cf_chl' in text or 'challenges.cloudflare.com' in text
            )

            if is_cf:
                print(f"[Scraper] Cloudflare detected on {url}, switching to browser...")
                _cf_solved_domains.add(domain)
                return _fetch_with_drission(url, referer)

            # A non-CF error status is a real server response — report it as-is
            if r.status_code >= 400:
                _last_fetch_error = f"The site returned HTTP {r.status_code}."
                print(f"[Scraper] HTTP {r.status_code} for {url}")
                return None

            if len(content) > 500:
                return BeautifulSoup(content, "lxml")
            _last_fetch_error = f"The site returned an unexpectedly short page ({len(content)} bytes)."
            print(f"[Scraper] Short response ({len(content)} bytes), retrying...")
            time.sleep(1)
        except Exception as e:
            err_str = str(e)
            if '403' in err_str or '503' in err_str:
                print(f"[Scraper] Cloudflare block ({e}), switching to browser...")
                _cf_solved_domains.add(domain)
                return _fetch_with_drission(url, referer)
            _last_fetch_error = f"{type(e).__name__}: {e}"
            print(f"[Scraper] Error fetching {url} (attempt {attempt+1}): {e}")
            time.sleep(1)

    return None  # All attempts failed


def _fetch_with_drission(url, referer=None):
    """Fetch a page using the persistent DrissionPage browser.
    Reuses the same Chrome session across multiple requests,
    so Cloudflare only needs to be solved once per domain.
    """
    global _last_fetch_error
    page = _get_drission_browser()
    if not page:
        return None  # _get_drission_browser already set _last_fetch_error

    try:
        print(f"[Scraper] DrissionPage fetching: {url}")
        page.get(url, timeout=40)

        # Wait for page to load (and CF to clear if needed)
        for i in range(15):
            time.sleep(2)
            html = page.html or ""
            if 'Just a moment' not in html and len(html) > 2000:
                if i > 0:
                    print(f"[Scraper] Cloudflare cleared in ~{(i+1)*2}s")
                return BeautifulSoup(html, "lxml")

        print(f"[Scraper] Page did not load within 30s: {url}")
        html = page.html or ""
        if html and len(html) > 500 and 'Just a moment' not in html:
            return BeautifulSoup(html, "lxml")
        _last_fetch_error = (
            "Cloudflare did not clear within 30s. The browser window may need a "
            "manual click on the verification checkbox."
        )
        return None

    except Exception as e:
        _last_fetch_error = f"Browser fetch failed: {type(e).__name__}: {e}"
        print(f"[Scraper] DrissionPage fetch error: {e}")
        return None


def _download_image_drission(img_url, save_path):
    """Download an image using the persistent DrissionPage browser."""
    page = _get_drission_browser()
    if not page:
        return False

    save_dir = os.path.dirname(save_path)
    save_name = os.path.basename(save_path)

    try:
        # DrissionPage.download(url, save_directory, rename=filename)
        # save_path must be a DIRECTORY, not a full file path
        result = page.download(img_url, save_dir, rename=save_name)

        # DrissionPage may add a suffix (_2, _3) if file exists — find the actual file
        if os.path.isfile(save_path):
            return True

        # Check if DrissionPage created save_name as a directory (known quirk)
        if os.path.isdir(save_path):
            # Move the actual file out of the wrongly-created directory
            for f in os.listdir(save_path):
                actual_file = os.path.join(save_path, f)
                if os.path.isfile(actual_file):
                    temp_path = save_path + ".tmp"
                    os.rename(actual_file, temp_path)
                    os.rmdir(save_path)
                    os.rename(temp_path, save_path)
                    return True

        # Check for suffixed versions (001_2.webp, etc.)
        base, ext = os.path.splitext(save_name)
        for f in os.listdir(save_dir):
            if f.startswith(base) and f.endswith(ext):
                actual = os.path.join(save_dir, f)
                if actual != save_path and os.path.isfile(actual):
                    os.rename(actual, save_path)
                    return True

        return os.path.isfile(save_path)
    except Exception as e:
        print(f"[Scraper] DrissionPage image download failed: {e}")
        return False


def _sanitize_name(name):
    """Convert URL slug or title to a clean folder name."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip().strip('-').replace('--', '-')
    return name or "unknown-manga"


def _extract_manga_slug(url):
    """Extract the manga name/slug from the URL."""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    # Handle paths like /manga/slug/ or /comics/slug/
    parts = [p for p in path.split('/') if p and p not in ('manga', 'comics', 'manhwa', 'manhua', 'series', 'title')]
    return parts[-1] if parts else "unknown"


def analyze_url(url):
    """
    Analyze a manga URL and return chapter list.
    Supports MangaDex (via API) and generic scraping sites.
    Returns: {
        'manga_name': str,
        'manga_slug': str,
        'total_chapters': int,
        'chapters': [{'number': int/float, 'title': str, 'url': str, 'date': str}, ...],
        'cover_url': str or None,
        'description': str,
        'site': str,
        'error': str or None
    }
    """
    url = url.rstrip('/')

    # ── MangaDex: use the official API ──────────────────────
    if _is_mangadex_url(url):
        print(f"[Scraper] MangaDex URL detected — using API")
        return _mangadex_analyze(url)

    result = {
        'manga_name': '', 'manga_slug': '', 'total_chapters': 0,
        'chapters': [], 'cover_url': None, 'description': '',
        'site': urlparse(url).netloc, 'error': None
    }

    soup = _get_soup(url)
    if not soup:
        if _last_fetch_error:
            result['error'] = (
                f"Could not fetch the page. {_last_fetch_error}\n"
                "If the site is Cloudflare-protected, try a MangaDex URL instead: "
                "https://mangadex.org"
            )
        else:
            result['error'] = (
                "Could not fetch the page. The site may be Cloudflare-protected.\n"
                "Try using a MangaDex URL instead: https://mangadex.org"
            )
        return result

    slug = _extract_manga_slug(url)
    result['manga_slug'] = slug

    # Try to get manga title
    title_el = (
        soup.select_one('.post-title h1') or  # Madara theme
        soup.select_one('.entry-title') or
        soup.select_one('h1.text-xl') or
        soup.select_one('h1') or
        soup.select_one('title')
    )
    title = title_el.get_text(strip=True) if title_el else slug.replace('-', ' ').title()
    # Clean up title
    title = re.sub(r'\s*[-–|]\s*(Read|Manhwa|Manga|Online|Free).*$', '', title, flags=re.IGNORECASE)
    result['manga_name'] = title.strip()

    # Try to get cover image
    cover = (
        soup.select_one('.summary_image img') or  # Madara
        soup.select_one('.thumb img') or
        soup.select_one('.comic-thumb img') or
        soup.select_one('img.wp-post-image') or
        soup.select_one('.entry-content img')
    )
    if cover:
        result['cover_url'] = cover.get('src') or cover.get('data-src') or cover.get('data-lazy-src')

    # Try to get description
    desc_el = (
        soup.select_one('.description-summary .summary__content p') or  # Madara
        soup.select_one('.summary__content p') or
        soup.select_one('.entry-content p') or
        soup.select_one('.comic-description p') or
        soup.select_one('meta[property="og:description"]')
    )
    if desc_el:
        result['description'] = desc_el.get('content', '') if desc_el.name == 'meta' else desc_el.get_text(strip=True)

    # ── Extract chapters ─────────────────────────────────────
    chapters = []

    # Strategy 1: Madara theme chapter list
    chapter_links = soup.select('.wp-manga-chapter a, .version-chap a')
    if not chapter_links:
        # Strategy 2: Generic list items with chapter links
        chapter_links = soup.select('.chapters a, .chapter-list a, ul.main a, .eplister a')
    if not chapter_links:
        # Strategy 3: Violet/Luminous scans pattern - look for links with "chapter" in href
        domain = urlparse(url).netloc
        all_links = soup.find_all('a', href=True)
        chapter_links = [a for a in all_links if re.search(r'chapter[_-]?\d+', a['href'], re.IGNORECASE)
                         and domain in a['href']]
    if not chapter_links:
        # Strategy 4: Any link containing the slug + chapter
        chapter_links = [a for a in soup.find_all('a', href=True)
                         if slug in a['href'] and re.search(r'chapter', a['href'], re.IGNORECASE)]

    seen_urls = set()
    for a in chapter_links:
        href = a.get('href', '')
        if not href or href in seen_urls or href == '#' or href.endswith('#/'):
            continue
        seen_urls.add(href)

        # Extract chapter number
        ch_text = a.get_text(strip=True)
        num_match = re.search(r'(?:chapter|ch)[.\s_-]*(\d+(?:\.\d+)?)', href, re.IGNORECASE)
        if not num_match:
            num_match = re.search(r'(?:chapter|ch)[.\s_-]*(\d+(?:\.\d+)?)', ch_text, re.IGNORECASE)
        if not num_match:
            num_match = re.search(r'(\d+(?:\.\d+)?)', ch_text)

        if num_match:
            ch_num = float(num_match.group(1))
            if ch_num == int(ch_num):
                ch_num = int(ch_num)
        else:
            continue

        # Extract date if present
        date_el = a.find_next_sibling(class_='chapter-release-date') or a.find_parent('li')
        date_str = ""
        if date_el:
            date_text = date_el.get_text()
            date_match = re.search(r'(\w+\s+\d+,?\s*\d{4})', date_text)
            if date_match:
                date_str = date_match.group(1)

        chapters.append({
            'number': ch_num,
            'title': f"Chapter {ch_num}",
            'url': urljoin(url, href),
            'date': date_str
        })

    # Deduplicate by chapter number, keep first occurrence
    seen_nums = set()
    unique_chapters = []
    for ch in chapters:
        if ch['number'] not in seen_nums:
            seen_nums.add(ch['number'])
            unique_chapters.append(ch)

    # Sort by chapter number
    unique_chapters.sort(key=lambda c: c['number'])
    result['chapters'] = unique_chapters
    result['total_chapters'] = len(unique_chapters)

    if not unique_chapters:
        result['error'] = "No chapters found. The site may use JavaScript rendering."

    return result


def _get_chapter_images(chapter_url, manga_url):
    """Extract image URLs from a single chapter page.
    Handles multiple reader types:
    - ts_reader.run() (Luminous, Violet, Reaper scans)
    - Madara WordPress reader (manhuaus, manhwatop, etc.)
    - Generic img-based readers
    """
    soup = _get_soup(chapter_url, referer=manga_url)
    if not soup:
        return []

    raw_html = str(soup)
    images = []

    # ─── Strategy 1: ts_reader.run() JSON data ──────────────────
    # Many scanlation sites use this JS reader that embeds images in a JSON call
    ts_match = re.search(r'ts_reader\.run\((\{.*?\})\)', raw_html, re.DOTALL)
    if ts_match:
        try:
            ts_data = json.loads(ts_match.group(1))
            sources = ts_data.get('sources', [])
            if sources and isinstance(sources, list):
                img_list = sources[0].get('images', [])
                if img_list:
                    print(f"[Scraper] ts_reader found {len(img_list)} images")
                    return [url.replace('\/', '/') for url in img_list]
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"[Scraper] ts_reader parse error: {e}")

    # ─── Strategy 2: JSON images array in any script tag ─────────
    # Some sites embed images as a plain JSON array
    for script in soup.find_all('script'):
        text = script.string or ''
        if not text:
            continue
        # Look for patterns like: "images":["url1","url2"]
        img_array = re.search(r'"images"\s*:\s*(\[(?:"[^"]*",?\s*)*\])', text)
        if img_array:
            try:
                urls = json.loads(img_array.group(1))
                if urls and len(urls) > 2:
                    clean = [u.replace('\/', '/') for u in urls if isinstance(u, str)]
                    if clean:
                        print(f"[Scraper] JSON images array found {len(clean)} images")
                        return clean
            except json.JSONDecodeError:
                pass
        # Look for chapter_preloaded_images or similar
        preload_match = re.search(r'(?:chapter_preloaded_images|chapter_images|imageList|page_image)\s*=\s*(\[.*?\])', text, re.DOTALL)
        if preload_match:
            try:
                urls = json.loads(preload_match.group(1))
                if urls and len(urls) > 2:
                    clean = [u.replace('\/', '/') for u in urls if isinstance(u, str)]
                    if clean:
                        print(f"[Scraper] Preloaded images found {len(clean)} images")
                        return clean
            except json.JSONDecodeError:
                pass

    # ─── Strategy 3: Madara reader page-break divs ───────────────
    page_breaks = soup.select('.page-break img, .reading-content .page-break img')
    if page_breaks:
        for img in page_breaks:
            src = img.get('data-src') or img.get('data-lazy-src') or img.get('src', '')
            src = src.strip()
            if src and not src.startswith('data:'):
                images.append(urljoin(chapter_url, src))
        if images:
            print(f"[Scraper] page-break found {len(images)} images")
            return images

    # ─── Strategy 4: Standard reader containers ──────────────────
    for selector in [
        '#readerarea img',
        '.reading-content img',
        '.chapter-content img',
        '.reader-area img',
        '.rdminimal img',
        '.entry-content .separator img',
        '.chapter-images img',
    ]:
        img_els = soup.select(selector)
        # Filter out non-content images
        content_imgs = []
        for img in img_els:
            src = img.get('data-src') or img.get('data-lazy-src') or img.get('src', '')
            src = src.strip()
            if not src or src.startswith('data:') or src.endswith('.svg'):
                continue
            if any(x in src.lower() for x in ['logo', 'icon', 'avatar', 'banner', 'ad-', 'advertisement', 'reaction']):
                continue
            content_imgs.append(urljoin(chapter_url, src))
        if len(content_imgs) > 2:  # Must have at least 3 images to be a chapter
            print(f"[Scraper] Selector '{selector}' found {len(content_imgs)} images")
            return content_imgs

    # ─── Strategy 5: Brute-force find manga image URLs in HTML ───
    # Look for image URLs that match common manga CDN patterns
    cdn_patterns = [
        r'(https?://[^\s"\'<>]+/(?:manga|chapter|uploads/manga)/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp))',
        r'(https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>]*)?)' ,
    ]
    for pattern in cdn_patterns:
        found_urls = re.findall(pattern, raw_html)
        # Filter and deduplicate
        content_urls = []
        seen = set()
        for u in found_urls:
            u = u.replace('\\/', '/')
            if u in seen:
                continue
            seen.add(u)
            if any(x in u.lower() for x in ['logo', 'icon', 'avatar', 'banner', 'emoji', 'theme', 'assets', 'reaction', 'gravatar']):
                continue
            content_urls.append(u)
        if len(content_urls) > 3:
            print(f"[Scraper] CDN pattern found {len(content_urls)} images")
            return content_urls

    print(f"[Scraper] No images found for {chapter_url}")
    return images


_DRISSION_LOCK = threading.Lock()  # the shared browser is NOT thread-safe


def _download_image_with_retry(img_url, save_path, referer, retries=3):
    """✅ RELIABILITY: download one image with retry + exponential backoff.
    Cleans up zero-byte/partial files between attempts. Returns bool."""
    delays = (2, 5, 10)
    for attempt in range(retries + 1):
        # Remove leftover partial file from a previous failed attempt
        try:
            if os.path.exists(save_path) and os.path.getsize(save_path) <= 1000:
                os.remove(save_path)
        except OSError:
            pass
        try:
            if _download_image(img_url, save_path, referer):
                if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                    return True
        except Exception as e:
            print(f"[Download] attempt {attempt + 1}/{retries + 1} failed for "
                  f"{os.path.basename(save_path)}: {e}")
        if attempt < retries:
            delay = delays[min(attempt, len(delays) - 1)]
            print(f"[Download] retrying {os.path.basename(save_path)} in {delay}s…")
            time.sleep(delay)
    print(f"[Download] ✗ giving up on {os.path.basename(save_path)} after {retries + 1} attempts")
    return False


def _download_image(img_url, save_path, referer):
    """Download a single image. Falls back to DrissionPage for CF-protected CDNs.
    Thread-safe: browser fallback calls are serialized via _DRISSION_LOCK."""
    domain = urlparse(img_url).netloc

    # If image CDN is CF-protected, use DrissionPage directly
    if domain in _cf_solved_domains:
        with _DRISSION_LOCK:
            return _download_image_drission(img_url, save_path)

    try:
        hdrs = {**HEADERS, 'Referer': referer}
        if _use_curl_cffi:
            r = _session.get(img_url, timeout=30, headers=hdrs)
        else:
            r = _session.get(img_url, timeout=30, headers=hdrs)

        # Check for Cloudflare block on image CDN
        if r.status_code in (403, 503):
            text = r.text if hasattr(r, 'text') else ''
            if 'cloudflare' in text.lower() or 'Just a moment' in text:
                _cf_solved_domains.add(domain)
                with _DRISSION_LOCK:
                    return _download_image_drission(img_url, save_path)

        r.raise_for_status()
        content = r.content if hasattr(r, 'content') else r.text.encode()
        if len(content) < 1000:
            print(f"[Scraper] Suspiciously small image ({len(content)} bytes): {img_url}")
            return False
        with open(save_path, 'wb') as f:
            f.write(content)
        return True
    except Exception as e:
        err_str = str(e)
        if '403' in err_str or '503' in err_str:
            _cf_solved_domains.add(domain)
            with _DRISSION_LOCK:
                return _download_image_drission(img_url, save_path)
        print(f"[Scraper] Failed to download {img_url}: {e}")
        return False


def download_chapters(manga_url, chapters, progress_callback=None):
    """
    Download selected chapters into organized folder structure.
    
    New structure: manhwa_download/{sanitized_manga_name}/Chapter_{num:05d}/

    Args:
        manga_url: The manga index URL
        chapters: List of chapter dicts with 'number' and 'url'
        progress_callback: Optional callback(current, total, message)

    Returns: {
        'manga_name': str,
        'download_dir': str,
        'downloaded': [{'chapter': num, 'path': str, 'images': int}, ...],
        'failed': [{'chapter': num, 'error': str}, ...],
        'total_images': int
    }
    """
    info = analyze_url(manga_url)
    manga_name_raw = info['manga_name'] or _extract_manga_slug(manga_url).replace('-', ' ').title()
    
    # Sanitize manhwa name for folder paths (spaces to hyphens, remove special chars)
    manga_name = config.sanitize_manhwa_name(manga_name_raw)

    manga_dir = os.path.join(DOWNLOAD_ROOT, manga_name)
    os.makedirs(manga_dir, exist_ok=True)

    result = {
        'manga_name': manga_name,
        'download_dir': manga_dir,
        'downloaded': [],
        'failed': [],
        'total_images': 0
    }

    total = len(chapters)
    for idx, ch in enumerate(chapters):
        ch_num = ch['number']
        ch_url = ch['url']
        
        # Use 5-digit chapter numbering (supports 99,999 chapters)
        try:
            chapter_int = int(float(ch_num))  # Handle "23.5" as 23
        except:
            chapter_int = idx + 1  # Fallback to index
        
        ch_dir = os.path.join(manga_dir, f"Chapter_{chapter_int:05d}")
        os.makedirs(ch_dir, exist_ok=True)

        if progress_callback:
            progress_callback(idx + 1, total, f"Downloading Chapter {ch_num} ({idx + 1}/{total})")

        # Get images for this chapter
        # MangaDex chapters have a stored chapter ID — use the API
        md_id = ch.get('_md_chapter_id')
        if md_id:
            images = _mangadex_get_chapter_images(md_id)
            referer = "https://mangadex.org/"
        else:
            images = _get_chapter_images(ch_url, manga_url)
            referer = ch_url

        if not images:
            result['failed'].append({'chapter': ch_num, 'error': 'No images found'})
            continue

        # ✅ RELIABILITY: resume + parallel + retry.
        # 1. Skip images already on disk (a stalled/interrupted download resumes)
        # 2. Fetch the remaining pages with a small thread pool (4-5 concurrent)
        # 3. Every page gets _download_image_with_retry (3 retries w/ backoff)
        img_jobs = []
        for i, img_url in enumerate(images):
            ext = os.path.splitext(urlparse(img_url).path)[1] or '.jpg'
            if ext.lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
                ext = '.jpg'
            save_path = os.path.join(ch_dir, f"{i + 1:03d}{ext}")

            if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                continue  # already downloaded — resume
            img_jobs.append((img_url, save_path))

        if img_jobs and progress_callback:
            progress_callback(idx + 1, total,
                              f"Chapter {ch_num}: fetching {len(img_jobs)} pages…")

        if img_jobs:
            max_workers = min(5, len(img_jobs))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_download_image_with_retry, url, sp, referer): sp
                           for (url, sp) in img_jobs}
                for fut in as_completed(futures):
                    fut.result()  # exceptions already logged inside the wrapper

        # Count what actually landed on disk (source of truth for resume)
        downloaded = sum(
            1 for f in os.listdir(ch_dir)
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))
            and os.path.getsize(os.path.join(ch_dir, f)) > 1000
        )

        result['downloaded'].append({
            'chapter': ch_num,
            'chapter_num': chapter_int,
            'path': ch_dir,
            'images': downloaded,
            'total': len(images)
        })
        result['total_images'] += downloaded

        # Delay between chapters
        time.sleep(0.5)

    # Save metadata
    meta_path = os.path.join(manga_dir, "manga_info.json")
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump({
            'name': manga_name,
            'original_name': manga_name_raw,
            'url': manga_url,
            'total_chapters': info['total_chapters'],
            'description': info['description'],
            'cover_url': info['cover_url'],
            'downloaded_chapters': [d['chapter_num'] for d in result['downloaded']],
            'download_date': datetime.now().isoformat(),
        }, f, indent=2, ensure_ascii=False)

    return result


def get_download_root():
    """Return the download root directory."""
    return DOWNLOAD_ROOT


def list_downloaded_manga():
    """List all downloaded manga with chapter counts."""
    if not os.path.exists(DOWNLOAD_ROOT):
        return []

    manga_list = []
    for name in sorted(os.listdir(DOWNLOAD_ROOT)):
        manga_dir = os.path.join(DOWNLOAD_ROOT, name)
        if not os.path.isdir(manga_dir):
            continue

        chapters = [d for d in os.listdir(manga_dir)
                     if os.path.isdir(os.path.join(manga_dir, d)) and d.startswith('Chapter')]

        # Load metadata if exists
        meta_path = os.path.join(manga_dir, "manga_info.json")
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                pass

        manga_list.append({
            'name': name,
            'path': manga_dir,
            'chapters_downloaded': len(chapters),
            'chapter_list': sorted(chapters),
            'url': meta.get('url', ''),
            'description': meta.get('description', ''),
        })

    return manga_list
