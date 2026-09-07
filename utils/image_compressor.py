from PIL import Image
import os
import tempfile


def compress_image(image_path, quality=85, max_size=1600):
    img = Image.open(image_path)

    # Fix orientation from phone cameras
    img = img.convert("RGB")

    # Resize while maintaining aspect ratio
    img.thumbnail((max_size, max_size))

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".jpg",
        delete=False
    )

    img.save(
        temp_file.name,
        "JPEG",
        quality=quality,
        optimize=True
    )

    return temp_file.name