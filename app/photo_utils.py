"""Uploaded website photos are resized/recompressed before being embedded
as base64 in the generated site - GitHub's Contents API (used to deploy
the site, see app/deployer.py) caps file content at ~1MB, so three
uploaded photos at their original size would blow that budget instantly.
"""

import os
import io
import base64
import tempfile
from PIL import Image

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_EMBEDDED_BYTES = 250 * 1024
MAX_DIMENSION = 1000

def process_photo(raw_bytes: bytes) -> str:
    """Saves the raw upload to a scratch file in /tmp, resizes/recompresses
    it with Pillow, and returns a base64 data URI. The temp file is always
    deleted before returning."""
    fd, path = tempfile.mkstemp(dir="/tmp", suffix=".upload")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)

        img = Image.open(path)
        img = img.convert("RGB")
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))

        quality = 70
        while True:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            if len(data) <= MAX_EMBEDDED_BYTES or quality <= 35:
                break
            quality -= 10

        encoded = base64.b64encode(data).decode()
        return f"data:image/jpeg;base64,{encoded}"
    finally:
        os.remove(path)
