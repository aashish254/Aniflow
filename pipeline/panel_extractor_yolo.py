"""YOLO-based Panel Extraction (port from YTV3).

Uses the trained YOLO model to split long vertical webtoon strips / pages
into individual panels, saved in reading order as panel_00001.jpg, panel_00002.jpg
in manhwa_crop/{manhwa_name}/Chapter_{num:05d}/.

Also writes panels_manifest.json mapping each panel to the raw source
page it was cropped from, with bbox coordinates and dimensions.
"""
import os
import json
import logging
from pathlib import Path
from typing import List

import cv2

import config

log = logging.getLogger("pipeline.panels_yolo")


class YoloPanelExtractor:
    """YOLO based panel splitter (port from YTV3 pipeline/panel_extractor.py)."""

    def __init__(self, model_path: str = None, conf: float = None):
        from ultralytics import YOLO
        import torch

        # torch 2.6+ defaults to weights_only=True which blocks YOLO .pt files
        _orig_load = torch.load
        def _safe_load(*args, **kwargs):
            kwargs.setdefault("weights_only", False)
            return _orig_load(*args, **kwargs)
        torch.load = _safe_load

        self.model_path = model_path or config.YOLO_MODEL_PATH
        if not Path(self.model_path).exists():
            raise FileNotFoundError(
                f"YOLO model not found at '{self.model_path}'. "
                "Download or symlink KAGGLE/best (6).pt from the YTV3 project."
            )
        log.info("Loading YOLO model: %s", self.model_path)
        self.model = YOLO(self.model_path)
        torch.load = _orig_load
        self.device = self._best_device()
        log.info("Panel detection running on device: %s", self.device)
        self.conf = conf if conf is not None else config.PANEL_CONF_THRESHOLD

    # ------------------------------------------------------------------
    def extract_chapter(self, raw_dir: str, state, progress_callback=None) -> List[str]:
        """
        Extract panels from raw chapter images using YOLO detection.
        Saves to: manhwa_crop/{manhwa_name}/Chapter_{num:05d}/panel_00001.jpg
        
        Args:
            raw_dir: Directory with raw page images
            state: PipelineState instance with crop_dir path
            progress_callback: Optional callback(current, total, message)
            
        Returns:
            List of panel file paths
        """
        raw_path = Path(raw_dir)
        panel_path = Path(state.crop_dir)
        os.makedirs(panel_path, exist_ok=True)
        
        manifest_path = panel_path / "panels_manifest.json"
        # Check for existing panels with new 5-digit naming
        existing = sorted(panel_path.glob("panel_*.jpg"), key=_panel_index)

        if manifest_path.exists() and existing:
            log.info("Found %d panels already in %s - skipping extraction",
                     len(existing), state.crop_dir)
            return [str(p) for p in existing]

        if existing:
            log.warning("Found %d panels from an interrupted run - re-extracting",
                        len(existing))
            for p in existing:
                p.unlink()

        pages = sorted(p for p in raw_path.iterdir() if p.is_file())
        if not pages:
            raise RuntimeError(f"No raw pages found in {raw_dir}. Download them first.")

        saved: List[Path] = []
        manifest = {}
        panel_id = 1
        
        total_pages = len(pages)
        for page_idx, page in enumerate(pages):
            if progress_callback:
                progress_callback(page_idx + 1, total_pages, 
                                f"Detecting panels in page {page_idx + 1}/{total_pages}")
            
            img = cv2.imread(str(page))
            if img is None:
                log.warning("Could not read %s - skipped", page.name)
                continue

            results = self.model.predict(
                source=img,
                classes=[0],  # panel class
                conf=self.conf,
                imgsz=config.PANEL_IMG_SIZE,
                device=self.device,
                verbose=False,
            )
            boxes = results[0].boxes.xyxy.cpu().numpy()

            # Reading order for vertical webtoons: top-to-bottom, left-to-right
            dets = sorted(
                (tuple(map(int, box)) for box in boxes),
                key=lambda b: (b[1], b[0]),
            )
            log.info("%s: %d panels detected", page.name, len(dets))

            for (x1, y1, x2, y2) in dets:
                if (x2 - x1) < config.MIN_PANEL_SIDE or (y2 - y1) < config.MIN_PANEL_SIDE:
                    continue
                crop = img[max(y1, 0):y2, max(x1, 0):x2]
                
                # Use 5-digit panel numbering: panel_00001.jpg
                out = panel_path / f"panel_{panel_id:05d}.jpg"
                cv2.imwrite(str(out), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                
                manifest[out.stem] = {
                    'source_page': page.name,
                    'bbox': [x1, y1, x2, y2],
                    'width': x2 - x1,
                    'height': y2 - y1,
                }
                saved.append(out)
                panel_id += 1

        if not saved:
            raise RuntimeError(
                "YOLO found no panels. Try lowering PANEL_CONF_THRESHOLD (e.g. 0.15)."
            )
        
        # Save manifest with enhanced metadata
        import json
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump({
                'total_panels': len(saved),
                'panels': manifest,
            }, f, indent=2, ensure_ascii=False)
        
        log.info("Extracted %d panels into %s", len(saved), state.crop_dir)
        return [str(p) for p in saved]

    @staticmethod
    def _suppress_overlaps(boxes: list, iou_threshold: float = 0.30) -> list:
        """Remove overlapping detection boxes using IoU suppression.
        
        When two boxes overlap by more than iou_threshold, the smaller one
        (by area) is discarded. This prevents duplicate panel crops.
        """
        if len(boxes) <= 1:
            return boxes
        
        # Sort by area descending — keep larger boxes first
        sorted_boxes = sorted(boxes, key=lambda b: b['w'] * b['h'], reverse=True)
        kept = []
        
        for box in sorted_boxes:
            bx1, by1 = box['x'], box['y']
            bx2, by2 = bx1 + box['w'], by1 + box['h']
            b_area = box['w'] * box['h']
            
            overlap_found = False
            for k in kept:
                kx1, ky1 = k['x'], k['y']
                kx2, ky2 = kx1 + k['w'], ky1 + k['h']
                
                # Calculate intersection
                ix1 = max(bx1, kx1)
                iy1 = max(by1, ky1)
                ix2 = min(bx2, kx2)
                iy2 = min(by2, ky2)
                
                if ix1 < ix2 and iy1 < iy2:
                    inter_area = (ix2 - ix1) * (iy2 - iy1)
                    # IoU = intersection / union
                    k_area = k['w'] * k['h']
                    union_area = b_area + k_area - inter_area
                    iou = inter_area / max(union_area, 1)
                    
                    if iou > iou_threshold:
                        overlap_found = True
                        break
            
            if not overlap_found:
                kept.append(box)
        
        # Re-sort by position (top-to-bottom, left-to-right)
        kept.sort(key=lambda b: (b['y'], b['x']))
        return kept

    def detect_image_boxes(self, img_path: str) -> list:
        img = cv2.imread(img_path)
        if img is None:
            raise RuntimeError(f"Could not read image: {img_path}")
            
        results = self.model.predict(
            source=img,
            classes=[0],
            conf=self.conf,
            imgsz=config.PANEL_IMG_SIZE,
            device=self.device,
            verbose=False,
        )
        boxes_data = results[0].boxes.xyxy.cpu().numpy()
        
        dets = sorted(
            (tuple(map(int, box)) for box in boxes_data),
            key=lambda b: (b[1], b[0]),
        )
        
        valid_dets = []
        for (x1, y1, x2, y2) in dets:
            if (x2 - x1) < config.MIN_PANEL_SIDE or (y2 - y1) < config.MIN_PANEL_SIDE:
                continue
            valid_dets.append({
                'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1,
            })
        
        # Remove overlapping boxes to prevent duplicate panels
        valid_dets = self._suppress_overlaps(valid_dets)
            
        return valid_dets

    def detect_chapter_boxes(self, raw_dir: str, state=None) -> dict:
        """
        Runs YOLO on raw pages and returns detection boxes.
        If state is provided, saves detection metadata to manhwa_detect/ folder.
        
        Returns: dict of {page_name: [[x1,y1,x2,y2], ...] }
        """
        raw_path = Path(raw_dir)
        pages = sorted(p for p in raw_path.iterdir() if p.is_file())
        if not pages:
            raise RuntimeError(f"No raw pages found in {raw_dir}.")
        
        all_dets = {}
        for page in pages:
            img = cv2.imread(str(page))
            if img is None: continue
            
            results = self.model.predict(
                source=img,
                classes=[0],
                conf=self.conf,
                imgsz=config.PANEL_IMG_SIZE,
                device=self.device,
                verbose=False,
            )
            boxes = results[0].boxes.xyxy.cpu().numpy()
            dets = sorted(
                (tuple(map(int, box)) for box in boxes),
                key=lambda b: (b[1], b[0]),
            )
            
            valid_dets = []
            for (x1, y1, x2, y2) in dets:
                if (x2 - x1) < config.MIN_PANEL_SIDE or (y2 - y1) < config.MIN_PANEL_SIDE:
                    continue
                valid_dets.append([x1, y1, x2, y2])
            all_dets[page.name] = valid_dets
            log.info("%s: %d panels detected", page.name, len(valid_dets))
        
        # Save detection metadata to new organized structure
        if state is not None:
            import json
            os.makedirs(state.detect_dir, exist_ok=True)
            metadata_path = os.path.join(state.detect_dir, 'detection_boxes.json')
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'total_pages': len(pages),
                    'total_panels': sum(len(dets) for dets in all_dets.values()),
                    'detections': all_dets,
                    'config': {
                        'conf_threshold': self.conf,
                        'min_panel_side': config.MIN_PANEL_SIDE,
                        'img_size': config.PANEL_IMG_SIZE,
                    }
                }, f, indent=2, ensure_ascii=False)
            log.info("Saved detection metadata to %s", metadata_path)
            
        return all_dets

    def preview_page(self, image_path: str) -> bytes:
        """Runs YOLO on a single page, draws green boxes, and returns JPEG bytes."""
        img = cv2.imread(image_path)
        if img is None:
            raise FileNotFoundError(f"Could not read {image_path}")

        results = self.model.predict(
            source=img,
            classes=[0],  # panel class
            conf=self.conf,
            imgsz=config.PANEL_IMG_SIZE,
            device=self.device,
            verbose=False,
        )
        boxes = results[0].boxes.xyxy.cpu().numpy()

        dets = sorted(
            (tuple(map(int, box)) for box in boxes),
            key=lambda b: (b[1], b[0]),
        )

        for (x1, y1, x2, y2) in dets:
            if (x2 - x1) < config.MIN_PANEL_SIDE or (y2 - y1) < config.MIN_PANEL_SIDE:
                continue
            # Draw a bright green box with thickness 4
            cv2.rectangle(img, (max(x1, 0), max(y1, 0)), (x2, y2), (0, 255, 0), 6)

        success, buffer = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not success:
            raise RuntimeError("Failed to encode preview image")
        return buffer.tobytes()

    # ------------------------------------------------------------------
    @staticmethod
    def _best_device() -> str:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"


def _panel_index(path: Path) -> int:
    """img12.png -> 12 (used to keep reading order everywhere)."""
    import re
    m = re.search(r"(\d+)", path.stem)
    return int(m.group(1)) if m else 0

_global_extractor = None

def get_global_extractor() -> YoloPanelExtractor:
    """Return a cached singleton instance of the YOLO extractor for quick previews."""
    global _global_extractor
    if _global_extractor is None:
        _global_extractor = YoloPanelExtractor()
    return _global_extractor


class BubbleTextExtractor(YoloPanelExtractor):
    """YOLO extractor using the panel_with_bubble_text model (Detect 1).
    
    This model has a single class: {0: 'panel_with_text'}.
    It detects manhwa panels that contain speech bubbles / text.
    """

    def __init__(self, model_path: str = None, conf: float = None):
        path = model_path or config.PANEL_WITH_BUBBLE_MODEL_PATH
        super().__init__(model_path=path, conf=conf)


_global_bubble_extractor = None

def get_global_bubble_extractor() -> BubbleTextExtractor:
    """Return a cached singleton instance of the BubbleText extractor."""
    global _global_bubble_extractor
    if _global_bubble_extractor is None:
        _global_bubble_extractor = BubbleTextExtractor()
    return _global_bubble_extractor
