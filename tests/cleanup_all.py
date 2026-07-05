#!/usr/bin/env python3
import shutil
import os

# Folders to clean completely
folders_to_clean = [
    'manhwa_download',
    'manhwa_stitch', 
    'manhwa_detect',
    'manhwa_crop',
    'manhwa_text',
    'manhwa_audio',
    'manhwa_clips',
    'projects/_previews'
]

# Delete all subdirectories and files in these folders
for folder in folders_to_clean:
    if os.path.exists(folder):
        for item in os.listdir(folder):
            # Keep .gitkeep files
            if item == '.gitkeep':
                continue
            item_path = os.path.join(folder, item)
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    print(f"Deleted directory: {item_path}")
                else:
                    os.remove(item_path)
                    print(f"Deleted file: {item_path}")
            except Exception as e:
                print(f"Error deleting {item_path}: {e}")

# Delete .DS_Store files
for root, dirs, files in os.walk('.'):
    for file in files:
        if file == '.DS_Store':
            try:
                ds_path = os.path.join(root, file)
                os.remove(ds_path)
                print(f"Deleted: {ds_path}")
            except:
                pass

print("\n✅ All projects and temporary files cleaned!")
