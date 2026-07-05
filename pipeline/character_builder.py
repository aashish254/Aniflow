"""
Character Builder - Extract character faces from manga panels and build character IDs.
Uses edge-density based detection to find face/character regions in manga/manhwa panels.
Stores character profiles with multiple reference poses for narration integration.
"""
import os
import json
import uuid
import re
from PIL import Image, ImageFilter
from datetime import datetime

CAST_DATA_DIR = os.path.expanduser("~/RECAP/Cast Data")


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def scan_faces(manga_path, chapter_from, chapter_to, session_id=None, max_per_image=3):
    """
    Scan chapter images and extract face/character crops.
    Returns session_id and list of face metadata.
    """
    if not session_id:
        session_id = uuid.uuid4().hex[:8]

    faces_dir = _ensure_dir(os.path.join(CAST_DATA_DIR, "_scans", session_id))

    # Collect all images from chapter range
    all_images = []
    for ch_name in sorted(os.listdir(manga_path)):
        ch_dir = os.path.join(manga_path, ch_name)
        if not os.path.isdir(ch_dir) or not ch_name.startswith('Chapter'):
            continue
        num_match = re.search(r'(\d+)', ch_name)
        if not num_match:
            continue
        ch_num = int(num_match.group(1))
        if ch_num < chapter_from or ch_num > chapter_to:
            continue
        for img_name in sorted(os.listdir(ch_dir)):
            if img_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                all_images.append({
                    'path': os.path.join(ch_dir, img_name),
                    'chapter': ch_name,
                    'filename': img_name,
                })

    faces = []
    face_idx = 0
    for img_info in all_images:
        try:
            crops = _detect_faces_in_image(img_info['path'], max_faces=max_per_image)
            for crop_img in crops:
                fname = f"face_{face_idx:04d}.jpg"
                save_path = os.path.join(faces_dir, fname)
                crop_img.save(save_path, "JPEG", quality=90)
                faces.append({
                    'index': face_idx,
                    'filename': fname,
                    'source': f"{img_info['chapter']}/{img_info['filename']}",
                    'session': session_id,
                })
                face_idx += 1
        except Exception as e:
            print(f"[CharBuilder] Error processing {img_info['path']}: {e}")

    # Save scan metadata
    meta = {'session_id': session_id, 'total_faces': len(faces),
            'manga_path': manga_path, 'chapters': f"{chapter_from}-{chapter_to}",
            'scanned_at': datetime.now().isoformat()}
    with open(os.path.join(faces_dir, 'scan_meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    return session_id, faces


def _detect_faces_in_image(img_path, face_size=180, max_faces=5):
    """
    Detect character faces in manga panels using OpenCV anime face cascade.
    Falls back to enhanced edge-density with skin-tone filtering.
    """
    import cv2
    import numpy as np

    # Load cascade classifier
    cascade_path = os.path.join(os.path.dirname(__file__), 'lbpcascade_animeface.xml')
    cascade = cv2.CascadeClassifier(cascade_path)

    # Read image
    img_cv = cv2.imread(img_path)
    if img_cv is None:
        # Try PIL for webp
        pil_img = Image.open(img_path).convert('RGB')
        img_cv = np.array(pil_img)[:, :, ::-1]  # RGB to BGR

    h, w = img_cv.shape[:2]
    if w < 50 or h < 50:
        return []

    # Convert to grayscale for detection
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    # Detect anime faces - stricter params for fewer false positives
    faces_rect = cascade.detectMultiScale(
        gray,
        scaleFactor=1.05,
        minNeighbors=5,
        minSize=(60, 60),
    )

    # Open original with PIL for cropping
    pil_img = Image.open(img_path).convert('RGB')

    results = []
    if len(faces_rect) > 0:
        # Sort by face size (largest first - likely main characters)
        faces_sorted = sorted(faces_rect, key=lambda f: f[2] * f[3], reverse=True)

        for (x, y, fw, fh) in faces_sorted[:max_faces]:
            # Expand crop slightly for better context (include hair/shoulders)
            pad_x = int(fw * 0.3)
            pad_y_top = int(fh * 0.4)  # More padding above for hair
            pad_y_bot = int(fh * 0.5)  # Padding below for shoulders

            cx = max(0, x - pad_x)
            cy = max(0, y - pad_y_top)
            cw = min(w - cx, fw + 2 * pad_x)
            ch = min(h - cy, fh + pad_y_top + pad_y_bot)

            face_crop = pil_img.crop((cx, cy, cx + cw, cy + ch))
            face_crop = face_crop.resize((face_size, face_size), Image.LANCZOS)
            results.append(face_crop)

    # If cascade found nothing, try fallback with skin-tone filtering
    if not results:
        results = _fallback_face_detect(pil_img, face_size, max_faces)

    return results


def _fallback_face_detect(pil_img, face_size=180, max_faces=3):
    """
    Fallback face detection using edge density + skin-tone color filtering.
    Only extracts regions that contain skin-like colors (not text/scenery).
    """
    w, h = pil_img.size
    if w < 100 or h < 100:
        return []

    # Resize for processing
    max_dim = 800
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        nw, nh = int(w * scale), int(h * scale)
        proc = pil_img.resize((nw, nh), Image.LANCZOS)
    else:
        proc = pil_img
        nw, nh = w, h

    gray = proc.convert('L')
    edges = gray.filter(ImageFilter.FIND_EDGES)

    win = max(80, min(nw // 2, 200))
    stride = win // 3

    regions = []
    for y in range(0, nh - win, stride):
        for x in range(0, nw - win, stride):
            box = (x, y, x + win, y + win)

            # Check edge density
            edge_crop = edges.crop(box)
            edge_density = sum(edge_crop.getdata()) / (win * win)

            # Check for skin-tone colors (filter out text/white regions)
            rgb_crop = proc.crop(box)
            pixels = list(rgb_crop.getdata())
            skin_count = 0
            white_count = 0
            for r, g, b in pixels:
                # Detect skin tones (various ethnicities + anime skin)
                if r > 150 and g > 100 and b > 70 and r > g and r > b:
                    skin_count += 1
                # Anime skin can also be very light
                if r > 200 and g > 180 and b > 160 and abs(r - g) < 40:
                    skin_count += 1
                # Count white pixels (text bubbles)
                if r > 230 and g > 230 and b > 230:
                    white_count += 1

            total = len(pixels)
            skin_ratio = skin_count / total
            white_ratio = white_count / total

            # Skip if too much white (text bubble) or no skin tones
            if white_ratio > 0.4 or skin_ratio < 0.05:
                continue
            if edge_density < 15:
                continue

            # Score: edge density + skin presence
            score = edge_density * (1 + skin_ratio * 3)
            regions.append((score, x, y))

    regions.sort(key=lambda r: r[0], reverse=True)

    # Non-max suppression
    selected = []
    for score, x, y in regions:
        overlap = False
        for _, sx, sy in selected:
            if abs(x - sx) < win * 0.7 and abs(y - sy) < win * 0.7:
                overlap = True
                break
        if not overlap:
            selected.append((score, x, y))
        if len(selected) >= max_faces:
            break

    inv = 1.0 / scale
    faces = []
    for _, x, y in selected:
        ox, oy = int(x * inv), int(y * inv)
        ow = int(win * inv)
        pad = int(ow * 0.15)
        ox = max(0, ox - pad)
        oy = max(0, oy - pad)
        ow_padded = min(w - ox, ow + 2 * pad)
        oh_padded = min(h - oy, ow + 2 * pad)
        face = pil_img.crop((ox, oy, ox + ow_padded, oy + oh_padded))
        face = face.resize((face_size, face_size), Image.LANCZOS)
        faces.append(face)

    return faces


def save_character(cast_name, name, gender, face_indices, session_id):
    """Save a character with selected face images."""
    cast_dir = _ensure_dir(os.path.join(CAST_DATA_DIR, cast_name))
    faces_dir = _ensure_dir(os.path.join(cast_dir, "faces"))
    chars_file = os.path.join(cast_dir, "characters.json")

    # Load existing
    characters = {}
    if os.path.exists(chars_file):
        with open(chars_file) as f:
            characters = json.load(f)

    # Generate unique ID
    char_id = f"CH-{uuid.uuid4().hex[:4]}"

    # Copy selected face images to cast directory
    scan_dir = os.path.join(CAST_DATA_DIR, "_scans", session_id)
    ref_images = []
    for idx in face_indices:
        src = os.path.join(scan_dir, f"face_{idx:04d}.jpg")
        if os.path.exists(src):
            dest_name = f"{char_id}_{len(ref_images)}.jpg"
            dest = os.path.join(faces_dir, dest_name)
            Image.open(src).save(dest, "JPEG", quality=90)
            ref_images.append(dest_name)

    characters[char_id] = {
        'id': char_id,
        'name': name,
        'gender': gender,
        'reference_images': ref_images,
        'created_at': datetime.now().isoformat(),
    }

    with open(chars_file, 'w') as f:
        json.dump(characters, f, indent=2)

    return characters[char_id]


def load_characters(cast_name):
    """Load saved characters for a cast."""
    chars_file = os.path.join(CAST_DATA_DIR, cast_name, "characters.json")
    if not os.path.exists(chars_file):
        return {}
    with open(chars_file) as f:
        return json.load(f)


def delete_character(cast_name, char_id):
    """Delete a character and its reference images."""
    cast_dir = os.path.join(CAST_DATA_DIR, cast_name)
    chars_file = os.path.join(cast_dir, "characters.json")
    if not os.path.exists(chars_file):
        return False
    with open(chars_file) as f:
        characters = json.load(f)
    if char_id not in characters:
        return False
    # Remove face files
    char = characters[char_id]
    faces_dir = os.path.join(cast_dir, "faces")
    for img in char.get('reference_images', []):
        p = os.path.join(faces_dir, img)
        if os.path.exists(p):
            os.remove(p)
    del characters[char_id]
    with open(chars_file, 'w') as f:
        json.dump(characters, f, indent=2)
    return True


def update_character(cast_name, char_id, name=None, gender=None):
    """Update character name or gender."""
    chars_file = os.path.join(CAST_DATA_DIR, cast_name, "characters.json")
    if not os.path.exists(chars_file):
        return None
    with open(chars_file) as f:
        characters = json.load(f)
    if char_id not in characters:
        return None
    if name is not None:
        characters[char_id]['name'] = name
    if gender is not None:
        characters[char_id]['gender'] = gender
    with open(chars_file, 'w') as f:
        json.dump(characters, f, indent=2)
    return characters[char_id]


def get_face_image_path(session_id, filename):
    """Get the full path to a scanned face image."""
    return os.path.join(CAST_DATA_DIR, "_scans", session_id, filename)


def get_char_face_path(cast_name, filename):
    """Get the full path to a saved character face image."""
    return os.path.join(CAST_DATA_DIR, cast_name, "faces", filename)


def find_similar_faces(session_id, reference_index, all_faces, threshold=0.72):
    """
    Find faces similar to the reference face using region-based comparison.
    Splits face into hair (top 40%) and face (bottom 60%) regions for
    more accurate matching. Uses perceptual hash for structural similarity.
    """
    import cv2
    import numpy as np

    scan_dir = os.path.join(CAST_DATA_DIR, "_scans", session_id)
    ref_path = os.path.join(scan_dir, f"face_{reference_index:04d}.jpg")

    if not os.path.exists(ref_path):
        return []

    ref_img = cv2.imread(ref_path)
    if ref_img is None:
        return []

    ref_img = cv2.resize(ref_img, (128, 128))
    
    # --- Compute reference features ---
    ref_features = _compute_face_features(ref_img)

    similar = []
    for face in all_faces:
        idx = face['index']
        if idx == reference_index:
            similar.append(idx)
            continue

        cand_path = os.path.join(scan_dir, face['filename'])
        if not os.path.exists(cand_path):
            continue

        cand_img = cv2.imread(cand_path)
        if cand_img is None:
            continue

        cand_img = cv2.resize(cand_img, (128, 128))
        cand_features = _compute_face_features(cand_img)

        # Compare each feature
        score_hair = cv2.compareHist(ref_features['hair_hist'], cand_features['hair_hist'], cv2.HISTCMP_CORREL)
        score_face = cv2.compareHist(ref_features['face_hist'], cand_features['face_hist'], cv2.HISTCMP_CORREL)
        score_hair_v = cv2.compareHist(ref_features['hair_val'], cand_features['hair_val'], cv2.HISTCMP_CORREL)
        
        # Perceptual hash distance (0-1 where 1 = identical)
        hash_dist = 1.0 - (bin(ref_features['phash'] ^ cand_features['phash']).count('1') / 64.0)

        # Hair color is the #1 distinguishing feature in manhwa
        # Dark hair vs blonde is the clearest differentiator
        combined = (score_hair * 0.35 +     # Hair hue (color)
                   score_hair_v * 0.20 +     # Hair brightness
                   score_face * 0.20 +       # Face/skin color
                   hash_dist * 0.25)         # Overall structure

        if combined >= threshold:
            similar.append(idx)

    return similar


def _compute_face_features(img):
    """Compute features for face comparison: hair region, face region, perceptual hash."""
    import cv2
    import numpy as np
    
    h, w = img.shape[:2]
    hair_region = img[0:int(h*0.4), :]        # Top 40% = hair
    face_region = img[int(h*0.4):, :]          # Bottom 60% = face/skin
    
    hsv_hair = cv2.cvtColor(hair_region, cv2.COLOR_BGR2HSV)
    hsv_face = cv2.cvtColor(face_region, cv2.COLOR_BGR2HSV)
    
    # Hair hue histogram (color)
    hair_hist = cv2.calcHist([hsv_hair], [0], None, [60], [0, 180])
    cv2.normalize(hair_hist, hair_hist)
    
    # Hair value histogram (brightness - distinguishes dark vs light hair)
    hair_val = cv2.calcHist([hsv_hair], [2], None, [40], [0, 256])
    cv2.normalize(hair_val, hair_val)
    
    # Face hue histogram
    face_hist = cv2.calcHist([hsv_face], [0, 1], None, [30, 30], [0, 180, 0, 256])
    cv2.normalize(face_hist, face_hist)
    
    # Perceptual hash (dHash - difference hash)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (9, 8))
    phash = 0
    for row in range(8):
        for col in range(8):
            phash = (phash << 1) | (1 if small[row, col] > small[row, col+1] else 0)
    
    return {
        'hair_hist': hair_hist,
        'hair_val': hair_val, 
        'face_hist': face_hist,
        'phash': phash
    }
