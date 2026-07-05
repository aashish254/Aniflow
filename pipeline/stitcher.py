"""
Image Stitcher for Manhwa/Webtoon
Vertically concatenates all chapter page images into a single long strip
for easier panel detection and cropping.

Uses PIL (Pillow) for saving since it handles arbitrarily large images,
unlike OpenCV's JPEG encoder which has a 65535 pixel dimension limit.
"""
import os
import re
import cv2
import numpy as np
from PIL import Image

# Increase PIL's decompression bomb limit to handle large stitched manhwa images
# Default is ~178MP, we increase to 500MP to handle long vertical strips
Image.MAX_IMAGE_PIXELS = 500000000  # 500 megapixels

# ─── Dataset constraints (Roboflow / standard training platforms) ─────────────
DATASET_MAX_MB       = 20          # hard limit per file
DATASET_MAX_WIDTH    = 16400       # px
DATASET_MAX_HEIGHT   = 10900       # px


def stitch_images(image_paths: list[str], state,
                  progress_callback=None) -> str:
    """
    Stitch all chapter page images into a single vertical strip.
    Saves to: manhwa_stitch/{manhwa_name}/Chapter_{num:05d}/stitched_full.jpg

    Args:
        image_paths: List of page image paths (already sorted)
        state: PipelineState instance with stitch_dir path
        progress_callback: Optional callback(current, total, message)

    Returns:
        Path to the stitched output image
    """
    if not image_paths:
        raise ValueError("No images to stitch")

    total = len(image_paths)
    if progress_callback:
        progress_callback(0, total, "Reading images for stitching...")

    # First pass: read all images with PIL and find the maximum width
    pil_images = []
    max_width = 0

    for i, path in enumerate(image_paths):
        if progress_callback:
            progress_callback(i + 1, total, f"Reading page {i + 1}/{total}...")

        try:
            img = Image.open(path).convert('RGB')
        except Exception as e:
            print(f"[Stitcher] Warning: Could not read {path}: {e}, skipping")
            continue

        pil_images.append(img)
        w, h = img.size
        if w > max_width:
            max_width = w

    if not pil_images:
        raise ValueError("No valid images could be read")

    # Second pass: resize all images to max_width (maintain aspect ratio)
    # and calculate total height
    total_height = 0
    resized = []

    for img in pil_images:
        w, h = img.size
        if w != max_width:
            scale = max_width / w
            new_h = int(h * scale)
            img = img.resize((max_width, new_h), Image.LANCZOS)

        resized.append(img)
        total_height += img.size[1]

    if progress_callback:
        progress_callback(total, total,
                          f"Stitching {len(resized)} pages ({max_width}x{total_height})...")

    # Create the stitched canvas and paste all images
    stitched = Image.new('RGB', (max_width, total_height))
    y_offset = 0
    for img in resized:
        stitched.paste(img, (0, y_offset))
        y_offset += img.size[1]

    # Save to new organized structure: manhwa_stitch/{name}/Chapter_{num:05d}/
    os.makedirs(state.stitch_dir, exist_ok=True)
    
    # Use PNG for lossless, or JPEG for smaller files
    if total_height > 65000:
        # Use PNG for very tall images (JPEG max dimension is 65535)
        output_path = os.path.join(state.stitch_dir, "stitched_full.png")
        stitched.save(output_path, 'PNG', optimize=True)
    else:
        output_path = os.path.join(state.stitch_dir, "stitched_full.jpg")
        stitched.save(output_path, 'JPEG', quality=95)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)

    if progress_callback:
        progress_callback(total, total,
                          f"Stitched {len(resized)} pages → {max_width}x{total_height} ({size_mb:.1f}MB)")

    print(f"[Stitcher] Created stitched image: {output_path} "
          f"({max_width}x{total_height}, {size_mb:.1f}MB)")

    # Free memory
    del resized, pil_images

    return output_path


# ─── Dataset Export ───────────────────────────────────────────────────────────

def export_dataset_stitched(stitched_path: str, state,
                             progress_callback=None) -> list[str]:
    """
    Export dataset-compliant stitched image(s) into the manhwa stitch folder.
    
    New structure:
        manhwa_stitch/{manhwa_name}/Chapter_{num:05d}/
            stitched_full.jpg              (main file)
            stitched_part001.jpg           (if split needed)
            stitched_part002.jpg

    Constraints enforced (Roboflow / common dataset platforms):
        • File size  ≤ 20 MB
        • Dimensions ≤ 16,400 × 10,900 px

    Args:
        stitched_path:     Full path to the existing stitched image.
        state:            PipelineState instance with stitch_dir path
        progress_callback: Optional callback(current, total, message).

    Returns:
        List of absolute paths to the saved dataset image(s).
    """
    if not os.path.exists(stitched_path):
        print(f"[Stitcher] Dataset export skipped — stitched image not found: {stitched_path}")
        return []

    # Use state's stitch directory
    stitched_dir = state.stitch_dir
    os.makedirs(stitched_dir, exist_ok=True)

    prefix = f"stitched_part"

    if progress_callback:
        progress_callback(0, 1, f"Exporting dataset copy → {stitched_dir} ...")

    # Load the stitched image
    img = Image.open(stitched_path).convert('RGB')
    orig_w, orig_h = img.size

    # ── Step 1: Scale down width if wider than dataset limit ──────────
    if orig_w > DATASET_MAX_WIDTH:
        scale = DATASET_MAX_WIDTH / orig_w
        new_h = int(orig_h * scale)
        print(f"[Stitcher] Dataset export: scaling width {orig_w}→{DATASET_MAX_WIDTH} (height {orig_h}→{new_h})")
        img = img.resize((DATASET_MAX_WIDTH, new_h), Image.LANCZOS)
        orig_w, orig_h = img.size

    # ── Step 2: Decide whether to split (height > dataset max) ────────
    slices = _split_image_vertically(img, DATASET_MAX_HEIGHT)
    img.close()

    saved_paths = []
    total_slices = len(slices)

    for part_idx, slice_img in enumerate(slices):
        # Build filename with 3-digit part numbering
        if total_slices == 1:
            filename = "stitched_dataset.jpg"
        else:
            filename = f"{prefix}{part_idx + 1:03d}.jpg"

        out_path = os.path.join(stitched_dir, filename)

        # ── Step 3: Reduce quality until file fits under 20 MB ────────
        _save_under_size_limit(slice_img, out_path, DATASET_MAX_MB)

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        w, h = slice_img.size
        print(f"[Stitcher] Dataset export saved: {out_path} ({w}x{h}, {size_mb:.1f}MB)")
        saved_paths.append(out_path)

        if progress_callback:
            progress_callback(part_idx + 1, total_slices,
                              f"Saved {filename} ({size_mb:.1f}MB)")

        slice_img.close()

    return saved_paths


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _split_image_vertically(img: Image.Image, max_height: int) -> list:
    """
    Split a PIL image into vertical slices, each at most max_height pixels tall.
    Returns a list of PIL Image objects (caller is responsible for closing them).
    """
    w, h = img.size
    if h <= max_height:
        # Return a copy so the caller can close it independently
        return [img.copy()]

    slices = []
    y = 0
    while y < h:
        slice_h = min(max_height, h - y)
        sliced = img.crop((0, y, w, y + slice_h))
        slices.append(sliced)
        y += slice_h

    return slices


def _save_under_size_limit(img: Image.Image, out_path: str, max_mb: float,
                            quality_start: int = 92, quality_min: int = 60):
    """
    Save a PIL image as JPEG under max_mb. Reduces JPEG quality step by step
    until the file fits, then falls back to downscaling if quality is exhausted.
    """
    max_bytes = int(max_mb * 1024 * 1024)
    quality = quality_start

    while quality >= quality_min:
        img.save(out_path, 'JPEG', quality=quality, optimize=True)
        if os.path.getsize(out_path) <= max_bytes:
            return
        quality -= 5

    # Quality reduction wasn't enough — downscale the image
    scale = 0.9
    current = img
    while scale > 0.3:
        w, h = img.size
        new_w, new_h = int(w * scale), int(h * scale)
        downscaled = img.resize((new_w, new_h), Image.LANCZOS)
        downscaled.save(out_path, 'JPEG', quality=quality_min, optimize=True)
        if os.path.getsize(out_path) <= max_bytes:
            if current is not img:
                current.close()
            return
        if current is not img:
            current.close()
        current = downscaled
        scale -= 0.1

    # Last resort: save whatever we have
    current.save(out_path, 'JPEG', quality=quality_min, optimize=True)
    if current is not img:
        current.close()

    final_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[Stitcher] Warning: Could not get {out_path} under {max_mb}MB "
          f"(final size: {final_mb:.1f}MB)")
