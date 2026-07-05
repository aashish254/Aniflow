#!/usr/bin/env python3
"""Clean all manhwa processing directories"""
import shutil
import os

dirs = [
    'manhwa_download',
    'manhwa_stitch',
    'manhwa_detect',
    'manhwa_crop',
    'manhwa_text',
    'manhwa_audio',
    'manhwa_clips'
]

print("🗑️  Deleting all previous projects and data...")
for d in dirs:
    if os.path.exists(d):
        for item in os.listdir(d):
            if item != '.gitkeep':
                path = os.path.join(d, item)
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                        print(f"   Deleted: {path}/")
                    else:
                        os.remove(path)
                        print(f"   Deleted: {path}")
                except Exception as e:
                    print(f"   Error deleting {path}: {e}")

print("\n✅ All projects and data deleted!")
print("📂 Directories are now empty and ready for fresh start\n")
