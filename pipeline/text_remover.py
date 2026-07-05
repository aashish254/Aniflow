"""
Text Box Remover — BubbleBlaster Approach
==========================================

Inspired by https://github.com/Aeonss/BubbleBlaster

Uses EasyOCR (already installed) to precisely locate text inside speech bubbles,
then fills ONLY those exact regions using OpenCV inpainting.

This is far more accurate than the previous contour-detection approach because:
  - EasyOCR finds the actual text strokes, not just white blob regions
  - Bounding boxes are tight around the text, not the entire bubble
  - Character faces and art are never touched

Two-layer approach:
  Layer 1 (BubbleBlaster-style): EasyOCR finds text bbox → expand to cover
           the whole bubble → inpaint with TELEA
  Layer 2 (Fallback): If EasyOCR is unavailable or finds nothing, fall back
           to the contour-whiteness detector from the previous version
           (which is already good for clear speech bubbles)
"""
import os
import cv2
import numpy as np
from PIL import Image

# EasyOCR is initialized once globally (model load is expensive)
_ocr_reader = None
_ocr_reader_attempted = False


def _get_ocr_reader():
    """Lazy-initialize EasyOCR reader (loads on first use, cached after)."""
    global _ocr_reader, _ocr_reader_attempted
    if _ocr_reader_attempted:
        return _ocr_reader
    _ocr_reader_attempted = True
    try:
        import easyocr
        print("[TextRemover] Loading EasyOCR (Korean + English)...")
        # gpu=False for CPU-only Macs — set True if CUDA available
        _ocr_reader = easyocr.Reader(['ko', 'en'], gpu=False, verbose=False)
        print("[TextRemover] EasyOCR ready ✓")
    except Exception as e:
        print(f"[TextRemover] EasyOCR not available ({e}) — using fallback detector")
        _ocr_reader = None
    return _ocr_reader


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def remove_text_boxes(panels: list[dict], project_dir: str,
                      progress_callback=None) -> list[dict]:
    """
    Detect and remove text from speech bubbles in each panel.
    Saves cleaned images to 'cropped_panels_no_text/' subdirectory.

    Uses EasyOCR (BubbleBlaster-style) for precise text detection.
    Falls back to whiteness-contour detection if OCR unavailable.

    Args:
        panels: List of panel dicts (must have 'path' key)
        project_dir: Project working directory
        progress_callback: Optional callback(current, total, message)

    Returns:
        Updated panels list with 'clean_path' field added
    """
    clean_dir = os.path.join(project_dir, "cropped_panels_no_text")
    os.makedirs(clean_dir, exist_ok=True)

    # Pre-load OCR reader once before the loop
    reader = _get_ocr_reader()

    total = len(panels)
    cleaned_count = 0

    for i, panel in enumerate(panels):
        if progress_callback:
            progress_callback(i + 1, total,
                              f"Removing text from panel {i + 1}/{total}...")

        panel_path = panel['path']
        panel_name = os.path.basename(panel_path)
        clean_path = os.path.join(clean_dir, panel_name)

        try:
            img = cv2.imread(panel_path)
            if img is None:
                print(f"[TextRemover] Could not read {panel_path}")
                panel['clean_path'] = panel_path
                continue

            if reader is not None:
                mask, n_regions = _detect_via_ocr(img, reader)
                method = f"OCR ({n_regions} regions)"
            else:
                mask = _detect_speech_bubbles_fallback(img)
                n_regions = int(np.any(mask > 0))
                method = "fallback contour"

            if np.any(mask > 0):
                cleaned = cv2.inpaint(img, mask, inpaintRadius=5,
                                      flags=cv2.INPAINT_TELEA)
                coverage = np.sum(mask > 0) / mask.size * 100
                print(f"[TextRemover] {panel_name}: {method}, "
                      f"removed {coverage:.1f}% area")
                cleaned_count += 1
            else:
                cleaned = img

            cv2.imwrite(clean_path, cleaned, [cv2.IMWRITE_JPEG_QUALITY, 95])
            panel['clean_path'] = clean_path

        except Exception as e:
            import traceback
            print(f"[TextRemover] Error on {panel_name}: {e}")
            traceback.print_exc()
            panel['clean_path'] = panel_path

    if progress_callback:
        progress_callback(total, total,
                          f"Cleaned {cleaned_count}/{total} panels")

    print(f"[TextRemover] Saved clean panels to {clean_dir}")
    return panels


# ═══════════════════════════════════════════════════════════════════
# Layer 1: BubbleBlaster-Style OCR Detection
# ═══════════════════════════════════════════════════════════════════

def _detect_via_ocr(img: np.ndarray, reader,
                    confidence_threshold: float = 0.35) -> tuple[np.ndarray, int]:
    """
    Use EasyOCR to find text bounding boxes, then expand each box
    to cover the full speech bubble containing that text, then build
    an inpaint mask.

    Returns (mask, num_regions_found).
    """
    h, w = img.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)

    # Downscale for faster OCR (EasyOCR is slow on tall panels)
    scale = 1.0
    ocr_img = img
    if w > 600:
        scale = 600 / w
        new_w = 600
        new_h = int(h * scale)
        # Cap height to avoid OOM on very tall stitched panels
        if new_h > 3000:
            scale = 3000 / h
            new_h = 3000
            new_w = int(w * scale)
        ocr_img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    try:
        results = reader.readtext(
            ocr_img,
            detail=1,
            paragraph=False,
            # Allow lower confidence for foreign-script text
            text_threshold=confidence_threshold,
        )
    except Exception as e:
        print(f"[TextRemover] OCR error: {e}")
        return mask, 0

    if not results:
        return mask, 0

    # Build grayscale for bubble expansion
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    n_found = 0
    for bbox, text, conf in results:
        if conf < confidence_threshold:
            continue
        if not text.strip():
            continue

        # Scale bbox back to original image coordinates
        pts = np.array(bbox, dtype=np.float32)
        pts /= scale
        pts = pts.astype(np.int32)

        # Get tight bounding rect of the OCR hit
        x, y, bw, bh = cv2.boundingRect(pts)

        # Clamp to image bounds
        x = max(0, x)
        y = max(0, y)
        bw = min(bw, w - x)
        bh = min(bh, h - y)

        if bw < 3 or bh < 3:
            continue

        # Expand the text box to the full speech bubble using flood-fill
        # from the centre of the detected text box outward on the white region
        bubble_mask = _expand_to_bubble(gray, x, y, bw, bh)
        mask = cv2.bitwise_or(mask, bubble_mask)
        n_found += 1

    return mask, n_found


def _expand_to_bubble(gray: np.ndarray,
                       tx: int, ty: int, tw: int, th: int) -> np.ndarray:
    """
    Given the tight bounding box of OCR-detected text, grow outward
    to capture the whole enclosing speech bubble using flood fill.

    Strategy:
    1. Sample the pixel at the text center — it should be white (bubble interior)
    2. Flood-fill white-ish pixels starting from that seed
    3. Limit the fill to a plausible bubble area (cap at 25% of image)

    Returns a binary mask (same size as gray) of the bubble area.
    """
    h, w = gray.shape[:2]
    panel_area = h * w

    # Seed point = centre of the OCR text box
    cx = min(tx + tw // 2, w - 1)
    cy = min(ty + th // 2, h - 1)

    # Check if seed is actually on a white-ish pixel (inside bubble)
    seed_val = int(gray[cy, cx])
    if seed_val < 160:
        # Text centre is not on white — just mask the tight text box + small pad
        pad = max(4, min(tw, th) // 4)
        mask = np.zeros((h, w), dtype=np.uint8)
        x0 = max(0, tx - pad)
        y0 = max(0, ty - pad)
        x1 = min(w, tx + tw + pad)
        y1 = min(h, ty + th + pad)
        mask[y0:y1, x0:x1] = 255
        return mask

    # Threshold white region for flood fill
    _, white = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

    # Flood fill from text center
    flood_mask = np.zeros((h + 2, w + 2), dtype=np.uint8)
    flags = (
        cv2.FLOODFILL_MASK_ONLY
        | (255 << 8)   # fill value = 255 in mask
        | cv2.FLOODFILL_FIXED_RANGE
    )
    cv2.floodFill(white, flood_mask, (cx, cy), 255,
                  loDiff=20, upDiff=20, flags=flags)

    # Extract the filled region (remove the 1px border flood_mask adds)
    region = flood_mask[1:-1, 1:-1]

    # Safety: cap at 25% of panel
    fill_ratio = np.sum(region > 0) / panel_area
    if fill_ratio > 0.25:
        # Too large — just use tight text box with small padding
        pad = max(6, min(tw, th) // 3)
        region = np.zeros((h, w), dtype=np.uint8)
        x0 = max(0, tx - pad)
        y0 = max(0, ty - pad)
        x1 = min(w, tx + tw + pad)
        y1 = min(h, ty + th + pad)
        region[y0:y1, x0:x1] = 255

    return region


# ═══════════════════════════════════════════════════════════════════
# Layer 2: Fallback — Contour + Whiteness (no OCR)
# ═══════════════════════════════════════════════════════════════════

def _detect_speech_bubbles_fallback(img: np.ndarray) -> np.ndarray:
    """
    Fallback: detect speech bubbles by finding white enclosed regions
    with high solidity (convex-ish shape) that contain text-like features.
    Used only if EasyOCR is unavailable.
    """
    h, w = img.shape[:2]
    panel_area = h * w
    mask = np.zeros((h, w), dtype=np.uint8)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    _, white_mask = cv2.threshold(gray, 210, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    white_mask = cv2.morphologyEx(white_mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)

    min_area = panel_area * 0.004
    max_area = panel_area * 0.25

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        aspect = bw / max(bh, 1)
        if aspect > 4.0 or aspect < 0.2:
            continue
        sub_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(sub_mask, [cnt], -1, 255, -1)
        roi_pixels = gray[sub_mask > 0]
        if roi_pixels.size == 0 or np.mean(roi_pixels) < 200:
            continue
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        if hull_area > 0 and (area / hull_area) < 0.60:
            continue
        roi_gray = gray[y:y + bh, x:x + bw]
        if _has_text_features(roi_gray):
            cv2.drawContours(mask, [cnt], -1, 255, -1)

    if np.any(mask > 0):
        erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4, 4))
        mask = cv2.erode(mask, erode_k, iterations=1)

    return mask


def _has_text_features(roi_gray: np.ndarray) -> bool:
    if roi_gray.size == 0:
        return False
    h, w = roi_gray.shape[:2]
    if h < 12 or w < 12:
        return False
    mean_val = float(np.mean(roi_gray))
    dark_threshold = max(100, mean_val - 50)
    dark_ratio = np.sum(roi_gray < dark_threshold) / roi_gray.size
    if 0.03 < dark_ratio < 0.55:
        return True
    return np.std(roi_gray) > 25 and mean_val > 180
