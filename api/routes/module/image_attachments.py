import base64
import binascii
import json
import re


MAX_IMAGES = 5
MAX_IMAGE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
IMAGE_PATTERN = re.compile(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})\Z")


def validate_image_attachments(raw):
    if not raw:
        return []
    if len(raw) > 3_000_000:
        raise ValueError("첨부 이미지의 전체 크기가 너무 큽니다.")
    try:
        images = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("첨부 이미지 데이터를 읽을 수 없습니다.") from None
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError("이미지는 최대 5개까지 첨부할 수 있습니다.")

    validated = []
    total_bytes = 0
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("name"), str) or not isinstance(image.get("data"), str):
            raise ValueError("첨부 이미지 형식이 올바르지 않습니다.")
        name = image["name"].strip()
        if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
            raise ValueError("이미지 파일명은 1~120자로 입력해 주세요.")
        match = IMAGE_PATTERN.fullmatch(image["data"])
        if not match:
            raise ValueError("PNG, JPG, WebP, GIF 이미지만 첨부할 수 있습니다.")
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("이미지 데이터를 읽을 수 없습니다.") from None
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("이미지는 파일당 512KB 이하로 첨부해 주세요.")
        image_type = match.group(1)
        valid = {
            "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "jpeg": data.startswith(b"\xff\xd8\xff"),
            "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
            "gif": data.startswith((b"GIF87a", b"GIF89a")),
        }
        if not valid[image_type]:
            raise ValueError("이미지 형식과 내용이 일치하지 않습니다.")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("첨부 이미지의 총 용량은 2MB 이하로 설정해 주세요.")
        validated.append({"name": name, "data": image["data"]})
    return validated
