"""
Manhwa Chapter Image Downloader
Supports: Direct folder loading, URL-based downloading
"""
import os
import re
import shutil
import requests
from PIL import Image
from urllib.parse import urlparse


def load_from_folder(folder_path: str, project_dir: str) -> list[str]:
    """
    Load manhwa chapter images from a local folder.
    Copies images to the project directory and returns sorted list of paths.
    """
    images_dir = os.path.join(project_dir, "raw_pages")
    os.makedirs(images_dir, exist_ok=True)

    valid_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
    image_files = []

    for f in sorted(os.listdir(folder_path)):
        ext = os.path.splitext(f)[1].lower()
        if ext in valid_extensions:
            src = os.path.join(folder_path, f)
            dst = os.path.join(images_dir, f)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            image_files.append(dst)

    # Sort naturally (page1, page2, ..., page10, page11)
    image_files.sort(key=_natural_sort_key)
    return image_files


def download_from_urls(urls: list[str], project_dir: str, headers: dict = None) -> list[str]:
    """
    Download manhwa chapter images from a list of URLs.
    Returns sorted list of downloaded file paths.
    """
    images_dir = os.path.join(project_dir, "raw_pages")
    os.makedirs(images_dir, exist_ok=True)

    if headers is None:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Referer': _get_referer(urls[0]) if urls else '',
        }

    downloaded = []
    for i, url in enumerate(urls):
        ext = _get_extension(url)
        filename = f"page_{i+1:04d}{ext}"
        filepath = os.path.join(images_dir, filename)

        if os.path.exists(filepath):
            downloaded.append(filepath)
            continue

        try:
            resp = requests.get(url, headers=headers, timeout=30, stream=True)
            resp.raise_for_status()
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            downloaded.append(filepath)
        except Exception as e:
            print(f"[Downloader] Failed to download {url}: {e}")

    downloaded.sort(key=_natural_sort_key)
    return downloaded


def validate_images(image_paths: list[str]) -> list[str]:
    """Validate that all image files are readable and return valid ones."""
    valid = []
    for path in image_paths:
        try:
            with Image.open(path) as img:
                img.verify()
            valid.append(path)
        except Exception as e:
            print(f"[Downloader] Invalid image {path}: {e}")
    return valid


def _natural_sort_key(s):
    """Sort strings with embedded numbers naturally."""
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(r'(\d+)', str(s))]


def _get_extension(url: str) -> str:
    """Extract file extension from URL."""
    parsed = urlparse(url)
    path = parsed.path
    ext = os.path.splitext(path)[1].lower()
    if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}:
        return ext
    return '.jpg'  # default


def _get_referer(url: str) -> str:
    """Extract base domain for referer header."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
