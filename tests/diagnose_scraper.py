"""
Diagnose why the scraper says "Could not fetch the page".
Run OUTSIDE the app:   ./venv/bin/python diagnose_scraper.py
"""
import os
import sys

URL = sys.argv[1] if len(sys.argv) > 1 else "https://manhuaus.com/manga/genius-martial-arts-trainer/"

print("=" * 60)
print("1. WHICH PYTHON")
print("=" * 60)
print(sys.executable)

print()
print("=" * 60)
print("2. LIBRARIES")
print("=" * 60)
for mod in ["curl_cffi", "cloudscraper", "DrissionPage", "bs4", "lxml"]:
    try:
        m = __import__(mod)
        print(f"  OK   {mod:<15} {getattr(m, '__version__', '?')}")
    except ImportError as e:
        print(f"  MISS {mod:<15} {e}")

print()
print("=" * 60)
print("3. DIRECT HTTP FETCH (curl_cffi)")
print("=" * 60)
try:
    from curl_cffi.requests import Session as CurlSession

    s = CurlSession(impersonate="chrome131")
    r = s.get(URL, timeout=30)
    text = r.text
    print(f"  status  : {r.status_code}")
    print(f"  bytes   : {len(r.content)}")
    cf_markers = ["Just a moment", "cf-browser-verification", "_cf_chl",
                  "challenges.cloudflare.com", "Checking your browser"]
    hits = [m for m in cf_markers if m in text]
    print(f"  CF hits : {hits or 'none'}")
    print(f"  snippet : {text[:200]!r}")
except Exception as e:
    print(f"  EXCEPTION: {type(e).__name__}: {e}")

print()
print("=" * 60)
print("4. CHROME / DRISSIONPAGE LAUNCH")
print("=" * 60)
CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
print(f"  Chrome exists: {os.path.exists(CHROME_MAC)}")
try:
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions()
    print(f"  configured browser_path: {co.browser_path!r}")

    if os.path.exists(CHROME_MAC):
        co.set_browser_path(CHROME_MAC)
    co.set_argument("--window-size", "1280,800")
    co.auto_port()

    page = ChromiumPage(co)
    print("  LAUNCH OK")
    page.get(URL)
    import time

    for i in range(15):
        time.sleep(2)
        html = page.html or ""
        if "Just a moment" not in html and len(html) > 2000:
            print(f"  PAGE LOADED after ~{(i + 1) * 2}s, {len(html)} bytes")
            break
    else:
        print(f"  STILL BLOCKED after 30s ({len(page.html or '')} bytes)")
    page.quit()
except Exception as e:
    print(f"  LAUNCH FAILED: {type(e).__name__}: {str(e)[:400]}")

print()
print("=" * 60)
print("5. FULL analyze_url()")
print("=" * 60)
try:
    from pipeline.web_scraper import analyze_url

    res = analyze_url(URL)
    print(f"  manga_name : {res['manga_name']!r}")
    print(f"  chapters   : {res['total_chapters']}")
    print(f"  error      : {res['error']!r}")
except Exception as e:
    print(f"  EXCEPTION: {type(e).__name__}: {e}")
