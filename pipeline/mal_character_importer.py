"""
MyAnimeList Character Importer
==============================

Pulls the character cast directly from a MyAnimeList manga characters page
(e.g. https://myanimelist.net/manga/138533/Nan_Hao_Shang_Feng/characters)
so the user doesn't have to hand-build the cast face-by-face.

The characters page is server-rendered — each character is a <tr> containing:
    <a href="https://myanimelist.net/character/<id>/<Name>">
    <h3 class="h3_character_name">Feng, Shang</h3>
    <small>Main</small>            <- role
    <img data-src="https://cdn.myanimelist.net/r/42x62/images/characters/10/462649.jpg">

Imported characters are written in the SAME dict format the Character Builder
uses ({char_id: {...}}), with profile images saved under <cast>/faces/ so the
existing /api/charbuilder/charface/<cast>/<file> endpoint serves them and the
narrator picks the names up automatically via _load_cast_characters().
"""
import re
import json
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime

MAL_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def normalize_mal_characters_url(url: str) -> str:
    """Accept a manga URL or a /characters URL; always return the /characters page."""
    url = (url or '').strip()
    if not url:
        return url
    if '/characters' not in url:
        url = url.rstrip('/') + '/characters'
    return url


def search_mal_manga(query: str, limit: int = 5) -> list:
    """Search MAL manga by free-text name. Returns [{'name', 'url', 'id'}]."""
    query = (query or '').strip().replace('-', ' ')
    if not query:
        return []
    try:
        resp = requests.get(
            'https://myanimelist.net/search/prefix.json',
            params={'type': 'manga', 'keyword': query, 'limit': limit},
            headers={'User-Agent': MAL_UA}, timeout=20)
        resp.raise_for_status()
        results = []
        for cat in resp.json().get('categories', []):
            for item in cat.get('items', [])[:limit]:
                results.append({'name': item.get('name', ''),
                                'url': item.get('url', ''),
                                'id': item.get('id')})
        return results
    except Exception as e:
        print(f"[MAL] Search failed for '{query}': {e}")
        return []


def _full_size_image(url: str) -> str:
    """Upgrade a resized CDN thumbnail to the full-size image."""
    if not url:
        return url
    # https://cdn.myanimelist.net/r/42x62/images/characters/10/462649.jpg?s=...
    #   -> https://cdn.myanimelist.net/images/characters/10/462649.jpg?s=...
    return re.sub(r'/r/\d+x\d+/', '/', url)


def scrape_characters_from_mal(mal_url: str, max_characters: int = 20) -> list:
    """Scrape {name, role, image_url, mal_character_id, mal_url} from MAL."""
    mal_url = normalize_mal_characters_url(mal_url)
    resp = requests.get(mal_url, headers={'User-Agent': MAL_UA}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, 'html.parser')

    characters = []
    seen_ids = set()

    # ── Primary: manga characters table rows ──────────────────────
    for a in soup.find_all('a', href=re.compile(r'myanimelist\.net/character/\d+/')):
        char_id_m = re.search(r'/character/(\d+)/', a.get('href', ''))
        if not char_id_m:
            continue
        char_id = char_id_m.group(1)
        if char_id in seen_ids:
            continue

        row = a.find_parent('tr') or a.find_parent('div', class_=re.compile(r'js-anime-character'))
        if row is None:
            continue

        # Name: h3_character_name, else img alt, else anchor text
        h3 = row.find('h3', class_='h3_character_name')
        name = (h3.get_text(strip=True) if h3 else '')
        if not name:
            img = a.find('img')
            name = (img.get('alt', '').strip() if img else '') or a.get_text(strip=True)
        name = name.replace('More', '').strip()
        if not name or len(name) < 2:
            continue

        # Role: the <small>Main</small> / <small>Supporting</small> tag
        role = 'Supporting'
        small = row.find('small')
        if small:
            t = small.get_text(strip=True).lower()
            if 'main' in t:
                role = 'Main'

        # Image: prefer the row thumbnail, upgraded to full size
        img_url = ''
        img = a.find('img') or row.find('img')
        if img:
            srcset = img.get('data-srcset') or ''
            # 2x variant is the biggest offered in the row (\S+ keeps the ?s= query)
            m2 = re.search(r'(\S+) 2x', srcset)
            img_url = m2.group(1) if m2 else (img.get('data-src') or img.get('src') or '')
            img_url = _full_size_image(img_url)

        seen_ids.add(char_id)
        characters.append({
            'name': name,
            'role': role,
            'image_url': img_url,
            'mal_character_id': char_id,
            'mal_url': f'https://myanimelist.net/character/{char_id}/',
        })
        if len(characters) >= max_characters:
            break

    # ── Fallback: anime-style pages use js-anime-character blocks ──
    if not characters:
        for block in soup.find_all('div', class_=re.compile(r'js-anime-character')):
            name_el = block.select_one('.js-anime-character-name')
            name = name_el.get_text(strip=True) if name_el else ''
            img = block.find('img')
            img_url = _full_size_image(img.get('data-src') or img.get('src') or '') if img else ''
            role = 'Supporting'
            role_el = block.select_one('.js-anime-character-role')
            if role_el and 'main' in role_el.get_text(strip=True).lower():
                role = 'Main'
            if name and len(name) >= 2:
                characters.append({
                    'name': name, 'role': role, 'image_url': img_url,
                    'mal_character_id': '', 'mal_url': mal_url,
                })
            if len(characters) >= max_characters:
                break

    return characters


def download_character_image(image_url: str, save_path: str) -> bool:
    """Download a character profile image, return True on success."""
    try:
        resp = requests.get(image_url, headers={'User-Agent': MAL_UA,
                                                'Referer': 'https://myanimelist.net/'},
                            timeout=20)
        resp.raise_for_status()
        with open(save_path, 'wb') as f:
            f.write(resp.content)
        return os.path.getsize(save_path) > 500
    except Exception as e:
        print(f"[MAL] Image download failed ({image_url}): {e}")
        return False


def import_characters_to_cast(characters: list, cast_name: str,
                              download_images: bool = True) -> dict:
    """
    Merge scraped characters into a cast in the Character Builder's DICT
    format: characters.json = {char_id: {...}} with images in <cast>/faces/.
    """
    from pipeline.character_builder import CAST_DATA_DIR, _ensure_dir

    cast_dir = _ensure_dir(os.path.join(CAST_DATA_DIR, cast_name))
    faces_dir = _ensure_dir(os.path.join(cast_dir, "faces"))
    chars_file = os.path.join(cast_dir, 'characters.json')

    existing = {}
    if os.path.exists(chars_file):
        with open(chars_file, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        if isinstance(existing, list):
            # Migrate a legacy list into the dict format
            existing = {c.get('id', f"char_{i:03d}"): c for i, c in enumerate(existing)}

    existing_names = {c.get('name', '').lower() for c in existing.values()}
    imported = []
    for char in characters:
        name_key = char['name'].lower()
        if name_key in existing_names:
            print(f"[MAL] '{char['name']}' already in cast — skipping")
            continue

        char_id = (f"mal_{char['mal_character_id']}" if char.get('mal_character_id')
                   else f"char_{len(existing) + 1:03d}")
        if char_id in existing:
            char_id = f"{char_id}_{len(existing)}"

        char_data = {
            'id': char_id,
            'name': char['name'],
            'gender': 'unknown',          # MAL has no gender field — editable in UI
            'role': char.get('role', 'Supporting'),
            'mal_url': char.get('mal_url', ''),
            'reference_images': [],
            'created_at': datetime.now().isoformat(),
            'source': 'myanimelist',
        }

        if download_images and char.get('image_url'):
            img_filename = f"{char_id}_0.jpg"
            if download_character_image(char['image_url'], os.path.join(faces_dir, img_filename)):
                char_data['reference_images'].append(img_filename)

        existing[char_id] = char_data
        existing_names.add(name_key)
        imported.append(char_data)

    with open(chars_file, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    return {'imported': len(imported), 'total': len(existing), 'characters': imported}


def add_bulk_characters(names: list, cast_name: str) -> dict:
    """Manually add character entries (name only) to a cast."""
    from pipeline.character_builder import CAST_DATA_DIR, _ensure_dir

    cast_dir = _ensure_dir(os.path.join(CAST_DATA_DIR, cast_name))
    chars_file = os.path.join(cast_dir, 'characters.json')
    existing = {}
    if os.path.exists(chars_file):
        with open(chars_file, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        if isinstance(existing, list):
            existing = {c.get('id', f"char_{i:03d}"): c for i, c in enumerate(existing)}

    existing_names = {c.get('name', '').lower() for c in existing.values()}
    added = []
    for name in names:
        name = (name or '').strip()
        if not name or name.lower() in existing_names:
            continue
        char_id = f"char_{len(existing) + 1:03d}"
        existing[char_id] = {
            'id': char_id, 'name': name, 'gender': 'unknown',
            'reference_images': [], 'created_at': datetime.now().isoformat(),
            'source': 'manual',
        }
        existing_names.add(name.lower())
        added.append(existing[char_id])

    with open(chars_file, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    return {'added': len(added), 'total': len(existing), 'characters': added}
