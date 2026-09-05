"""
Regression tests for create_woocommerce_draft.py's image signature
validation (CLAUDE.md section 17.1 media-identifier contract; the
recovery fix for FB-HIST-2026-AUTOIMPORT-CAN-0016's INVALID_IMAGE_
SIGNATURE failure).

Content sniffing is authoritative over a declared/guessed Content-Type:
a source server or CDN can mislabel a genuinely valid image (real JPEG
bytes served as "image/webp"). validate_image_signature() must accept
such a file -- using the *sniffed* type from here on, never the wrong
declared one -- while still rejecting anything that is not a real,
supported image (an HTML error page, a truncated/malformed payload).
"""
from __future__ import annotations

import pytest

from create_woocommerce_draft import (
    WordPressMediaError,
    sniff_image_content_type,
    validate_image_signature,
)


JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 32
HTML_ERROR_BODY = b"<html><head><title>404 Not Found</title></head><body>Not Found</body></html>"
MALFORMED_BYTES = b"this is not an image at all, just some random bytes"


def test_jpeg_bytes_with_wrong_webp_content_type_is_accepted_as_jpeg():
    """JPEG bytes + wrong webp Content-Type => accepted if valid JPEG."""
    authoritative_type = validate_image_signature(
        image_bytes=JPEG_BYTES,
        content_type="image/webp",
    )

    assert authoritative_type == "image/jpeg"


def test_html_body_with_image_content_type_is_rejected():
    """HTML body + image Content-Type => rejected."""
    with pytest.raises(WordPressMediaError) as excinfo:
        validate_image_signature(
            image_bytes=HTML_ERROR_BODY,
            content_type="image/jpeg",
        )

    assert excinfo.value.error_code == "INVALID_IMAGE_SIGNATURE"


def test_malformed_bytes_are_rejected():
    """Malformed/garbage bytes => rejected, regardless of declared type."""
    with pytest.raises(WordPressMediaError) as excinfo:
        validate_image_signature(
            image_bytes=MALFORMED_BYTES,
            content_type="image/png",
        )

    assert excinfo.value.error_code == "INVALID_IMAGE_SIGNATURE"


def test_correctly_labeled_jpeg_still_accepted():
    """A correctly-labeled file still validates normally -- the sniffing
    fix must not regress the already-working matching-label case."""
    assert validate_image_signature(
        image_bytes=JPEG_BYTES,
        content_type="image/jpeg",
    ) == "image/jpeg"


def test_correctly_labeled_png_still_accepted():
    assert validate_image_signature(
        image_bytes=PNG_BYTES,
        content_type="image/png",
    ) == "image/png"


def test_correctly_labeled_webp_still_accepted():
    assert validate_image_signature(
        image_bytes=WEBP_BYTES,
        content_type="image/webp",
    ) == "image/webp"


def test_png_bytes_with_wrong_jpeg_content_type_is_accepted_as_png():
    """The mislabeling fix is symmetric across supported types, not
    special-cased to the one JPEG/webp combination that failed in
    production."""
    assert validate_image_signature(
        image_bytes=PNG_BYTES,
        content_type="image/jpeg",
    ) == "image/png"


def test_sniff_image_content_type_returns_none_for_unrecognized_bytes():
    assert sniff_image_content_type(MALFORMED_BYTES) is None
    assert sniff_image_content_type(HTML_ERROR_BODY) is None


def test_sniff_image_content_type_detects_each_supported_signature():
    assert sniff_image_content_type(JPEG_BYTES) == "image/jpeg"
    assert sniff_image_content_type(PNG_BYTES) == "image/png"
    assert sniff_image_content_type(WEBP_BYTES) == "image/webp"
