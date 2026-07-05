"""
Panel Cropper for Manhwa/Webtoon
=================================

Redesigned for dark-background manhwa (like "Between Your Letter and My Reply").

Now includes AI-powered panel filtering via qwen2.5vl:3b to automatically skip:
  - Promotional pages and recruitment ads
  - Watermark / scanlation group pages
  - Chapter cover / title pages
  - Publisher credit pages
  - "End of chapter" cards
"""
import os
import cv2
import numpy as np
from PIL import Image
from pathlib import Path
import config


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def crop_panels(image_paths: list[str], state,
                progress_callback=None,
                use_ai_filter: bool = True) -> list[dict]:
    """
    Detect and crop individual panels from manhwa pages / stitched strips.
    Saves to: manhwa_crop/{manhwa_name}/Chapter_{num:05d}/panel_00001.jpg

    Args:
        image_paths:    Paths to source images (raw pages or stitched strip)
        state:          PipelineState instance with crop_dir path
        progress_callback: Optional callback(current, total, message)
        use_ai_filter:  Whether to use qwen2.5vl:3b to skip promo/watermark panels

    Returns:
        List of dicts with panel metadata (only story panels)
    """
    # Lazy import to avoid circular deps
    from pipeline.panel_filter import should_skip_panel

    panels_dir = state.crop_dir
    os.makedirs(panels_dir, exist_ok=True)

    all_panels = []
    panel_counter = 0
    skipped_count = 0
    total_pages = len(image_paths)

    for page_idx, page_path in enumerate(image_paths):
        if progress_callback:
            progress_callback(page_idx + 1, total_pages,
                              f"Cropping page {page_idx + 1}/{total_pages}...")

        try:
            img = _load_image(page_path)
            if img is None:
                print(f"[Cropper] Could not load: {page_path}")
                continue

            h, w = img.shape[:2]
            print(f"[Cropper] Processing {os.path.basename(page_path)}: {w}x{h}px")

            # Geometric panel detection
            page_panels = _detect_panels(img)
            print(f"[Cropper] Found {len(page_panels)} segments — running AI filter...")

            total_this_page = len(page_panels)

            for seg_idx, panel_img in enumerate(page_panels):
                ph, pw = panel_img.shape[:2]
                if ph < config.MIN_PANEL_HEIGHT or pw < 50:
                    continue  # Truly tiny — skip without AI

                # AI / heuristic content filter
                if use_ai_filter:
                    skip, reason = should_skip_panel(
                        panel_img,
                        panel_index=seg_idx,
                        total_panels=total_this_page,
                    )
                    if skip:
                        skipped_count += 1
                        print(f"[Cropper]   ✗ Skipped segment {seg_idx + 1}/{total_this_page}: {reason}")
                        continue

                panel_counter += 1
                # Use 5-digit panel numbering: panel_00001.jpg
                panel_filename = f"panel_{panel_counter:05d}.jpg"
                panel_path = os.path.join(panels_dir, panel_filename)

                pil_panel = Image.fromarray(cv2.cvtColor(panel_img, cv2.COLOR_BGR2RGB))
                pil_panel.save(panel_path, 'JPEG', quality=95)

                all_panels.append({
                    'panel_id': panel_counter,
                    'path': panel_path,
                    'source_page': os.path.basename(page_path),
                    'width': pw,
                    'height': ph,
                })

        except Exception as e:
            import traceback
            print(f"[Cropper] Error on {page_path}: {e}")
            traceback.print_exc()

    if progress_callback:
        progress_callback(total_pages, total_pages,
                          f"Cropped {panel_counter} story panels "
                          f"({skipped_count} non-story skipped)")
    
    # Save panels metadata to crop directory
    import json
    metadata_path = os.path.join(panels_dir, 'panels_metadata.json')
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump({
            'total_panels': len(all_panels),
            'skipped_panels': skipped_count,
            'panels': all_panels,
        }, f, indent=2, ensure_ascii=False)
    
    print(f"[Cropper] Saved panels metadata to {metadata_path}")

    return all_panels


# ═══════════════════════════════════════════════════════════════════
# Image Loading
# ═══════════════════════════════════════════════════════════════════

from typing import Optional

def _load_image(path: str) -> Optional[np.ndarray]:
    """Load an image with PIL fallback for large/unusual formats."""
    from PIL import Image as PILImage
    PILImage.MAX_IMAGE_PIXELS = None  # Allow arbitrarily large images

    try:
        pil = PILImage.open(path).convert('RGB')
        arr = np.array(pil)
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception as e:
        print(f"[Cropper] PIL load failed: {e}")
        img = cv2.imread(path)
        return img


# ═══════════════════════════════════════════════════════════════════
# Main Detection Router
# ═══════════════════════════════════════════════════════════════════

def _detect_panels(img: np.ndarray) -> list[np.ndarray]:
    """
    Route to the best detection strategy based on image characteristics.
    """
    h, w = img.shape[:2]

    # Analyze the image to pick the right strategy
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mean_brightness = np.mean(gray)
    is_dark_manhwa = mean_brightness < 100  # Dark-background content

    # --- Strategy 1: True separator detection (color gap rows) ---
    panels = _detect_by_separator_rows(img, gray, is_dark_manhwa)

    if len(panels) >= 2:
        return panels

    # --- Strategy 2: Edge valley detection ---
    panels = _detect_by_edge_valleys(img, gray)
    if len(panels) >= 2:
        return panels

    # --- Fallback: Split into equal reasonable chunks ---
    print("[Cropper] No panel boundaries found, splitting into chunks")
    return _split_into_chunks(img, target_height=1000)


# ═══════════════════════════════════════════════════════════════════
# Strategy 1: True Separator Row Detection
# ═══════════════════════════════════════════════════════════════════

def _detect_by_separator_rows(img: np.ndarray, gray: np.ndarray,
                               is_dark: bool) -> list[np.ndarray]:
    """
    Find rows that are TRUE separators: solid/near-solid color lines
    between panels. Works for both dark and light manhwa.

    Key insight: separator rows have VERY low standard deviation across
    ALL channels, regardless of their brightness. Only actual gap rows
    between panels have this property — dark artwork content has variation.
    """
    h, w = img.shape[:2]

    # Per-channel std for each row (use BGR channels)
    b, g, r = cv2.split(img.astype(np.float32))
    std_b = np.std(b, axis=1)
    std_g = np.std(g, axis=1)
    std_r = np.std(r, axis=1)
    max_std = np.maximum(np.maximum(std_b, std_g), std_r)

    row_means = np.mean(gray, axis=1).astype(np.float32)

    # True separator: very uniform row (regardless of brightness)
    # Threshold: std < 8 means less than ~3% variation — truly solid
    is_solid = max_std < 8.0

    # Additional pass: rows that are very bright (white separators)
    is_white_sep = (row_means > 240) & (max_std < 20)

    # Very dark uniform rows (black borders between pages in the stitch)
    is_black_sep = (row_means < 8) & (max_std < 5)

    separator_mask = is_solid | is_white_sep | is_black_sep

    # Find contiguous separator regions
    min_gap_px = max(3, config.GAP_THRESHOLD // 3)
    gap_regions = _find_gap_regions(separator_mask, min_gap_px)

    if not gap_regions:
        return []

    print(f"[Cropper] Found {len(gap_regions)} separator regions")

    # Build split points at the center of each gap
    split_points = [0]
    for gap_start, gap_end in gap_regions:
        split_points.append((gap_start + gap_end) // 2)
    split_points.append(h)

    # Extract panels between split points
    panels = []
    for i in range(len(split_points) - 1):
        y0, y1 = split_points[i], split_points[i + 1]
        ph = y1 - y0

        if ph < config.MIN_PANEL_HEIGHT:
            continue

        if ph > config.MAX_PANEL_HEIGHT:
            # Sub-split oversized segment
            sub = img[y0:y1, :]
            sub_panels = _smart_split_large(sub)
            panels.extend(sub_panels)
        else:
            panels.append(img[y0:y1, :])

    # Remove blank page-transition artifacts, then merge tiny content panels
    panels = _filter_blank_panels(panels)
    panels = _merge_tiny_panels(panels)
    return panels


# ═══════════════════════════════════════════════════════════════════
# Strategy 2: Edge Density Valley Detection
# ═══════════════════════════════════════════════════════════════════

def _detect_by_edge_valleys(img: np.ndarray, gray: np.ndarray) -> list[np.ndarray]:
    """
    Find panel boundaries by looking for valleys in horizontal edge density.
    Works when there are no clean separator rows.
    """
    h, w = img.shape[:2]

    # Apply bilateral filter to preserve edges while smoothing noise
    smoothed = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(smoothed, 30, 100)

    # Sum edge pixels per row
    edge_density = np.sum(edges, axis=1).astype(np.float32)

    # Smooth the density profile
    kernel_size = max(21, h // 80)
    if kernel_size % 2 == 0:
        kernel_size += 1
    edge_density_smooth = cv2.GaussianBlur(
        edge_density.reshape(-1, 1), (1, kernel_size), 0
    ).flatten()

    # Find valleys (low edge density = likely between panels)
    p10 = np.percentile(edge_density_smooth, 10)
    p90 = np.percentile(edge_density_smooth, 90)
    valley_threshold = p10 + (p90 - p10) * 0.12

    is_valley = edge_density_smooth < valley_threshold
    gap_regions = _find_gap_regions(is_valley, config.GAP_THRESHOLD)

    if not gap_regions:
        return []

    print(f"[Cropper] Edge-valley found {len(gap_regions)} boundaries")

    split_points = [0]
    for gs, ge in gap_regions:
        split_points.append((gs + ge) // 2)
    split_points.append(h)

    panels = []
    for i in range(len(split_points) - 1):
        y0, y1 = split_points[i], split_points[i + 1]
        ph = y1 - y0
        if ph < config.MIN_PANEL_HEIGHT:
            continue
        if ph > config.MAX_PANEL_HEIGHT:
            panels.extend(_smart_split_large(img[y0:y1, :]))
        else:
            panels.append(img[y0:y1, :])

    panels = _filter_blank_panels(panels)
    panels = _merge_tiny_panels(panels)
    return panels


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _smart_split_large(img: np.ndarray) -> list[np.ndarray]:
    """
    Split an oversized image segment. Tries edge valleys first,
    falls back to equal chunks.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    edges = cv2.Canny(gray, 30, 100)
    edge_density = np.sum(edges, axis=1).astype(np.float32)

    kernel_size = max(11, h // 60)
    if kernel_size % 2 == 0:
        kernel_size += 1
    smoothed = cv2.GaussianBlur(
        edge_density.reshape(-1, 1), (1, kernel_size), 0
    ).flatten()

    p10 = np.percentile(smoothed, 10)
    p90 = np.percentile(smoothed, 90)
    threshold = p10 + (p90 - p10) * 0.08
    is_valley = smoothed < threshold

    gap_regions = _find_gap_regions(is_valley, 10)
    valid_gaps = [(gs, ge) for gs, ge in gap_regions
                  if (gs + ge) // 2 > config.MIN_PANEL_HEIGHT]

    if valid_gaps:
        split_points = [0]
        for gs, ge in valid_gaps:
            mid = (gs + ge) // 2
            if mid - split_points[-1] >= config.MIN_PANEL_HEIGHT:
                split_points.append(mid)
        split_points.append(h)
        panels = []
        for i in range(len(split_points) - 1):
            y0, y1 = split_points[i], split_points[i + 1]
            if y1 - y0 >= config.MIN_PANEL_HEIGHT:
                panels.append(img[y0:y1, :])
        if panels:
            return panels

    return _split_into_chunks(img, target_height=900)


def _split_into_chunks(img: np.ndarray, target_height: int = 1000) -> list[np.ndarray]:
    """Split image into equal-height chunks."""
    h = img.shape[0]
    n = max(1, round(h / target_height))
    chunk_h = h // n
    chunks = []
    for i in range(n):
        y0 = i * chunk_h
        y1 = (i + 1) * chunk_h if i < n - 1 else h
        if y1 - y0 >= config.MIN_PANEL_HEIGHT:
            chunks.append(img[y0:y1, :])
    return chunks if chunks else [img]


def _filter_blank_panels(panels: list[np.ndarray],
                          blank_threshold: float = 0.88) -> list[np.ndarray]:
    """
    Remove panels that are nearly entirely white or black with no real content.
    These are page-boundary artifacts left over from stitching pages together.
    """
    result = []
    for panel in panels:
        gray = cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY).astype(np.float32)
        total = gray.size
        if total == 0:
            continue
        white_ratio = np.sum(gray > 245) / total
        black_ratio = np.sum(gray < 10) / total
        blank_ratio = white_ratio + black_ratio
        if blank_ratio > blank_threshold:
            print(f"[Cropper] Dropped blank panel ({panel.shape[0]}px, {blank_ratio:.0%} blank)")
            continue
        result.append(panel)
    return result if result else panels  # never return empty


def _merge_tiny_panels(panels: list[np.ndarray],
                        min_h: int = None) -> list[np.ndarray]:
    """
    Merge panels that are continuations of the same scene.

    Two-pass approach:
      Pass 1 — Scene continuity: compare color histograms between adjacent panels.
               If the bottom edge of panel[i] matches the top edge of panel[i+1],
               they are the same scene (e.g. character head + body cut) → merge.
      Pass 2 — Size safety: any remaining panel below min_h gets merged with
               its best-matching neighbor (by histogram similarity).

    This avoids merging genuinely different short scenes while correctly joining
    cut-up character panels like the shower head+body split.
    """
    if len(panels) <= 1:
        return panels

    # --- Pass 1: Scene continuity merge ---
    panels = _merge_by_scene_continuity(panels)

    # --- Pass 2: Size-based safety merge for truly tiny remainders ---
    if min_h is None:
        if len(panels) > 1:
            heights = [p.shape[0] for p in panels]
            avg_h = sum(heights) / len(heights)
            # Only merge panels smaller than 20% of average (very conservative)
            min_h = max(config.MIN_PANEL_HEIGHT, int(avg_h * 0.20))
        else:
            min_h = config.MIN_PANEL_HEIGHT

    if len(panels) <= 1:
        return panels

    max_iterations = 5
    for _ in range(max_iterations):
        any_merged = False
        result = []
        i = 0
        while i < len(panels):
            panel = panels[i]
            if panel.shape[0] < min_h:
                any_merged = True
                # Merge with whichever neighbor has higher histogram similarity
                prev_sim = _hist_similarity(result[-1], panel) if result else 0
                next_sim = _hist_similarity(panel, panels[i + 1]) if i + 1 < len(panels) else 0

                if next_sim >= prev_sim and i + 1 < len(panels):
                    combined = np.vstack([panel, panels[i + 1]])
                    result.append(combined)
                    i += 2
                elif result:
                    prev = result.pop()
                    result.append(np.vstack([prev, panel]))
                    i += 1
                else:
                    result.append(panel)
                    i += 1
            else:
                result.append(panel)
                i += 1
        panels = result
        if not any_merged:
            break

    return panels


def _merge_by_scene_continuity(panels: list[np.ndarray],
                                similarity_threshold: float = 0.82) -> list[np.ndarray]:
    """
    Merge adjacent panels that belong to the same scene by comparing
    the color histogram of the bottom edge of panel[i] with the top
    edge of panel[i+1]. High similarity = same scene = merge.

    The comparison zone is the middle 40% of panel width (avoids black borders)
    and uses the bottom/top 15% of panel height for comparison.
    """
    if len(panels) <= 1:
        return panels

    merged = True
    while merged:
        merged = False
        result = []
        i = 0
        while i < len(panels):
            if i + 1 < len(panels):
                sim = _hist_similarity(panels[i], panels[i + 1])
                # Only merge if both panels are short OR similarity is very high
                p_short = panels[i].shape[0] < 600 or panels[i + 1].shape[0] < 600
                if sim > similarity_threshold and p_short:
                    combined = np.vstack([panels[i], panels[i + 1]])
                    result.append(combined)
                    i += 2
                    merged = True
                    print(f"[Cropper] Merged scene-continuous panels "
                          f"({panels[i-2].shape[0]}px + {panels[i-1].shape[0]}px, "
                          f"sim={sim:.2f})")
                else:
                    result.append(panels[i])
                    i += 1
            else:
                result.append(panels[i])
                i += 1
        panels = result

    return panels


def _hist_similarity(panel_a: np.ndarray, panel_b: np.ndarray) -> float:
    """
    Compare color histograms of the bottom edge of panel_a
    and the top edge of panel_b.

    Returns correlation coefficient in [0, 1] where 1 = identical.
    """
    try:
        h_a, w_a = panel_a.shape[:2]
        h_b, w_b = panel_b.shape[:2]

        # Use middle 60% of width to avoid left/right border artifacts
        x0 = int(w_a * 0.20)
        x1 = int(w_a * 0.80)

        # Compare bottom 15% of panel_a vs top 15% of panel_b
        zone_a = panel_a[max(0, h_a - int(h_a * 0.15)):, x0:x1]
        zone_b = panel_b[:int(h_b * 0.15), x0:x1]

        if zone_a.size == 0 or zone_b.size == 0:
            return 0.0

        # Build per-channel histograms (32 bins each)
        hists_a, hists_b = [], []
        for ch in range(3):
            h_a_ch = cv2.calcHist([zone_a], [ch], None, [32], [0, 256])
            h_b_ch = cv2.calcHist([zone_b], [ch], None, [32], [0, 256])
            cv2.normalize(h_a_ch, h_a_ch)
            cv2.normalize(h_b_ch, h_b_ch)
            hists_a.append(h_a_ch)
            hists_b.append(h_b_ch)

        # Average correlation across channels
        corrs = [
            cv2.compareHist(hists_a[c], hists_b[c], cv2.HISTCMP_CORREL)
            for c in range(3)
        ]
        return float(np.mean(corrs))

    except Exception:
        return 0.0


def _find_gap_regions(gap_mask: np.ndarray, min_gap: int) -> list[tuple[int, int]]:
    """Find contiguous runs of True in gap_mask with length >= min_gap."""
    regions = []
    in_gap = False
    gap_start = 0
    n = len(gap_mask)

    for i in range(n):
        if gap_mask[i] and not in_gap:
            in_gap = True
            gap_start = i
        elif not gap_mask[i] and in_gap:
            in_gap = False
            if i - gap_start >= min_gap:
                regions.append((gap_start, i))

    if in_gap and n - gap_start >= min_gap:
        regions.append((gap_start, n))

    return regions
