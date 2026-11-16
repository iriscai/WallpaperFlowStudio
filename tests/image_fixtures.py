"""Small, valid PNG responses for image generation tests."""

import io

from PIL import Image


def png_bytes(width=1080, height=1920, color=(60, 80, 120)):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()
