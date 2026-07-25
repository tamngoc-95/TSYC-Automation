import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


BATCH_CODE = "FB-2026-001"
STORAGE_BUCKET = "product-images"

UPLOADER_NAME = "facebook_image_supabase_uploader"
UPLOADER_VERSION = "0.6.3"

LOCAL_IMAGE_ROOT = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "facebook-images"
    / BATCH_CODE
)

ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}

SOURCE_TYPE = "FACEBOOK"
USAGE_RIGHTS_STATUS = "RIGHTS_UNKNOWN"

# image_role is intentionally NULL because the image
# has not yet been classified.
IMAGE_ROLE = None

# image_status is intentionally omitted from insert payload.
# The database assigns the valid default value: PENDING.


def find_metadata_files() -> list[Path]:
    """Find local Facebook image metadata files."""
    if not LOCAL_IMAGE_ROOT.exists():
        raise RuntimeError(
            "Local Facebook image directory does not exist: "
            f"{LOCAL_IMAGE_ROOT}"
        )

    metadata_files = [
        file_path
        for file_path in LOCAL_IMAGE_ROOT.rglob("*.json")
        if file_path.name != "download_summary.json"
    ]

    return sorted(metadata_files)


def load_json(
    file_path: Path,
) -> dict[str, Any]:
    """Read one UTF-8 JSON file."""
    try:
        raw_text = file_path.read_text(
            encoding="utf-8"
        )

        payload = json.loads(
            raw_text
        )

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid JSON file: {file_path}"
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Metadata JSON must contain an object: "
            f"{file_path}"
        )

    return payload


def resolve_local_image_path(
    metadata: dict[str, Any],
    metadata_path: Path,
) -> Path:
    """Resolve the image file referenced by metadata."""
    local_path_value = str(
        metadata.get("local_image_path")
        or ""
    ).strip()

    if local_path_value:
        local_path = Path(
            local_path_value
        )

        if not local_path.is_absolute():
            local_path = (
                PROJECT_ROOT
                / local_path
            )

        if local_path.exists():
            return local_path

    image_hash = str(
        metadata.get("sha256")
        or ""
    ).strip()

    if image_hash:
        hash_prefix = image_hash[:16]

        matching_files = [
            file_path
            for file_path in metadata_path.parent.glob(
                f"image_*_{hash_prefix}.*"
            )
            if file_path.suffix.lower() != ".json"
        ]

        if matching_files:
            return matching_files[0]

    raise RuntimeError(
        "The local image file referenced by metadata "
        f"could not be found: {metadata_path}"
    )


def validate_metadata(
    metadata: dict[str, Any],
    image_path: Path,
) -> None:
    """Validate metadata and local image before upload."""
    required_fields = (
        "raw_page_id",
        "source_url_id",
        "facebook_post_url",
        "facebook_image_url",
        "content_type",
        "sha256",
        "natural_width",
        "natural_height",
    )

    missing_fields = [
        field_name
        for field_name in required_fields
        if metadata.get(field_name) in {
            None,
            "",
        }
    ]

    if missing_fields:
        raise RuntimeError(
            "Required metadata fields are missing: "
            + ", ".join(missing_fields)
        )

    if not image_path.exists():
        raise RuntimeError(
            f"Image file does not exist: {image_path}"
        )

    if not image_path.is_file():
        raise RuntimeError(
            f"Image path is not a file: {image_path}"
        )

    file_size = image_path.stat().st_size

    if file_size <= 0:
        raise RuntimeError(
            f"Image file is empty: {image_path}"
        )

    content_type = str(
        metadata.get("content_type")
        or ""
    ).strip().lower()

    if content_type not in ALLOWED_MIME_TYPES:
        raise RuntimeError(
            "Unsupported image MIME type: "
            f"{content_type or '[missing]'}"
        )

    image_hash = str(
        metadata.get("sha256")
        or ""
    ).strip().lower()

    if len(image_hash) != 64:
        raise RuntimeError(
            "SHA-256 value must contain exactly 64 characters."
        )

    if not all(
        character in "0123456789abcdef"
        for character in image_hash
    ):
        raise RuntimeError(
            "SHA-256 value contains invalid characters."
        )

    try:
        width = int(
            metadata["natural_width"]
        )

        height = int(
            metadata["natural_height"]
        )

    except (
        TypeError,
        ValueError,
    ) as error:
        raise RuntimeError(
            "Image width and height must be numeric."
        ) from error

    if width <= 0 or height <= 0:
        raise RuntimeError(
            "Image width and height must be positive."
        )


def build_storage_path(
    metadata: dict[str, Any],
    image_path: Path,
) -> str:
    """Build a deterministic Storage path using SHA-256."""
    raw_page_id = str(
        metadata["raw_page_id"]
    ).strip()

    image_hash = str(
        metadata["sha256"]
    ).strip().lower()

    extension = image_path.suffix.lower()

    if extension == ".jpeg":
        extension = ".jpg"

    if not extension:
        extension = ".bin"

    return (
        f"facebook/"
        f"{BATCH_CODE}/"
        f"{raw_page_id}/"
        f"{image_hash}{extension}"
    )


def find_existing_database_record(
    repository: SupabaseRepository,
    raw_page_id: str,
    image_hash: str,
) -> dict[str, Any] | None:
    """Find an existing image record for the same post and hash."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id, "
            "raw_page_id, "
            "image_hash, "
            "storage_bucket, "
            "storage_path, "
            "image_status"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .eq(
            "image_hash",
            image_hash,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def storage_file_exists(
    repository: SupabaseRepository,
    storage_path: str,
) -> bool:
    """Check whether an image already exists in Supabase Storage."""
    storage_path_object = Path(
        storage_path
    )

    folder_path = (
        storage_path_object
        .parent
        .as_posix()
    )

    file_name = (
        storage_path_object.name
    )

    storage_items = (
        repository.client.storage
        .from_(STORAGE_BUCKET)
        .list(
            folder_path,
            {
                "limit": 100,
                "offset": 0,
                "search": file_name,
            },
        )
    )

    return any(
        storage_item.get("name") == file_name
        for storage_item in storage_items
    )


def upload_storage_file(
    repository: SupabaseRepository,
    image_path: Path,
    storage_path: str,
    content_type: str,
) -> None:
    """Upload one local image to Supabase Storage."""
    image_content = image_path.read_bytes()

    if not image_content:
        raise RuntimeError(
            f"Image file is empty: {image_path}"
        )

    repository.client.storage.from_(
        STORAGE_BUCKET
    ).upload(
        path=storage_path,
        file=image_content,
        file_options={
            "content-type": content_type,
            "cache-control": "3600",
            "upsert": False,
        },
    )


def build_database_payload(
    metadata: dict[str, Any],
    image_path: Path,
    storage_path: str,
) -> dict[str, Any]:
    """Build a valid product_images insert payload."""
    return {
        "candidate_id": None,
        "reference_id": None,

        "raw_page_id": metadata["raw_page_id"],
        "source_url_id": metadata["source_url_id"],

        # Valid value from product_images_source_type_check.
        "source_type": SOURCE_TYPE,

        # Temporary Facebook CDN image URL.
        "source_url": metadata.get(
            "facebook_image_url"
        ),

        # Stable Facebook photo page URL.
        "source_photo_url": metadata.get(
            "parent_photo_url"
        ),

        "storage_bucket": STORAGE_BUCKET,
        "storage_path": storage_path,

        "original_file_name": image_path.name,

        "mime_type": str(
            metadata["content_type"]
        ).strip().lower(),

        "width_pixels": int(
            metadata["natural_width"]
        ),

        "height_pixels": int(
            metadata["natural_height"]
        ),

        "file_size_bytes": (
            image_path.stat().st_size
        ),

        "image_hash": str(
            metadata["sha256"]
        ).strip().lower(),

        # NULL is valid according to
        # product_images_image_role_check.
        "image_role": IMAGE_ROLE,

        # Valid value according to
        # product_images_usage_rights_status_check.
        "usage_rights_status": (
            USAGE_RIGHTS_STATUS
        ),

        "is_main_image_candidate": False,
        "is_selected_main_image": False,
        "is_publish_eligible": False,

        # image_status is not included.
        # The database assigns PENDING automatically.

        "collector_name": UPLOADER_NAME,
        "collector_version": UPLOADER_VERSION,

        "uploaded_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


def insert_database_record(
    repository: SupabaseRepository,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert one image record into product_images."""
    response = (
        repository.client
        .table("product_images")
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Supabase returned no product_images record "
            "after insert."
        )

    return records[0]


def remove_storage_file(
    repository: SupabaseRepository,
    storage_path: str,
) -> None:
    """Remove a Storage file after a failed database insert."""
    repository.client.storage.from_(
        STORAGE_BUCKET
    ).remove(
        [
            storage_path
        ]
    )


def upload_one_image(
    repository: SupabaseRepository,
    metadata_path: Path,
) -> str:
    """Upload one image and create its database record."""
    metadata = load_json(
        metadata_path
    )

    image_path = resolve_local_image_path(
        metadata=metadata,
        metadata_path=metadata_path,
    )

    validate_metadata(
        metadata=metadata,
        image_path=image_path,
    )

    raw_page_id = str(
        metadata["raw_page_id"]
    ).strip()

    image_hash = str(
        metadata["sha256"]
    ).strip().lower()

    content_type = str(
        metadata["content_type"]
    ).strip().lower()

    storage_path = build_storage_path(
        metadata=metadata,
        image_path=image_path,
    )

    print()
    print("-" * 78)
    print(
        f"Metadata: {metadata_path.name}"
    )
    print(
        f"Image: {image_path.name}"
    )
    print(
        f"Raw page ID: {raw_page_id}"
    )
    print(
        f"SHA-256: {image_hash}"
    )
    print(
        f"Storage path: {storage_path}"
    )

    existing_record = (
        find_existing_database_record(
            repository=repository,
            raw_page_id=raw_page_id,
            image_hash=image_hash,
        )
    )

    if existing_record is not None:
        print(
            "Result: DUPLICATE_DATABASE"
        )
        print(
            "Existing image ID: "
            f"{existing_record.get('image_id')}"
        )
        print(
            "Existing image status: "
            f"{existing_record.get('image_status')}"
        )
        print(
            "Existing Storage path: "
            f"{existing_record.get('storage_path')}"
        )

        return "DUPLICATE_DATABASE"

    already_in_storage = storage_file_exists(
        repository=repository,
        storage_path=storage_path,
    )

    uploaded_in_this_run = False

    if already_in_storage:
        print(
            "Storage file already exists."
        )

    else:
        print(
            "Uploading image to Supabase Storage..."
        )

        upload_storage_file(
            repository=repository,
            image_path=image_path,
            storage_path=storage_path,
            content_type=content_type,
        )

        uploaded_in_this_run = True

        print(
            "Storage upload completed."
        )

    payload = build_database_payload(
        metadata=metadata,
        image_path=image_path,
        storage_path=storage_path,
    )

    try:
        database_record = insert_database_record(
            repository=repository,
            payload=payload,
        )

    except Exception:
        if uploaded_in_this_run:
            print(
                "Database insert failed. "
                "Removing the newly uploaded Storage file..."
            )

            try:
                remove_storage_file(
                    repository=repository,
                    storage_path=storage_path,
                )

                print(
                    "Storage rollback completed."
                )

            except Exception as rollback_error:
                print(
                    "Warning: Storage rollback failed."
                )
                print(
                    "Rollback error type: "
                    f"{type(rollback_error).__name__}"
                )
                print(
                    "Rollback details: "
                    f"{rollback_error}"
                )

        raise

    print(
        "Database record created."
    )
    print(
        "Image ID: "
        f"{database_record.get('image_id')}"
    )
    print(
        "Image status: "
        f"{database_record.get('image_status')}"
    )
    print(
        "Source type: "
        f"{database_record.get('source_type')}"
    )

    if already_in_storage:
        return "DATABASE_RECORD_CREATED"

    return "UPLOADED"


def print_files_ready_for_upload(
    metadata_files: list[Path],
) -> None:
    """Print local metadata files ready for upload."""
    print()
    print(
        "Files ready for upload:"
    )

    for index, metadata_path in enumerate(
        metadata_files,
        start=1,
    ):
        try:
            display_path = (
                metadata_path.relative_to(
                    PROJECT_ROOT
                )
            )

        except ValueError:
            display_path = metadata_path

        print(
            f"[{index}] {display_path}"
        )


def main() -> None:
    """Upload downloaded Facebook images to Supabase."""
    load_dotenv()

    print(
        "Facebook image Supabase uploader started."
    )
    print(
        f"Version: {UPLOADER_VERSION}"
    )
    print(
        f"Bucket: {STORAGE_BUCKET}"
    )
    print(
        f"Batch: {BATCH_CODE}"
    )

    repository = SupabaseRepository()

    metadata_files = find_metadata_files()

    print(
        "Image metadata files found: "
        f"{len(metadata_files)}"
    )

    if not metadata_files:
        print(
            "No local Facebook image metadata files were found."
        )
        return

    print_files_ready_for_upload(
        metadata_files
    )

    print()

    confirmation = input(
        "Type UPLOAD to send these images to Supabase, "
        "or press Enter to cancel: "
    ).strip().upper()

    if confirmation != "UPLOAD":
        print(
            "Supabase upload cancelled."
        )
        return

    results = {
        "UPLOADED": 0,
        "DATABASE_RECORD_CREATED": 0,
        "DUPLICATE_DATABASE": 0,
        "FAILED": 0,
    }

    for metadata_path in metadata_files:
        try:
            status = upload_one_image(
                repository=repository,
                metadata_path=metadata_path,
            )

            results[status] += 1

        except Exception as error:
            results["FAILED"] += 1

            print()
            print(
                "Image upload failed."
            )
            print(
                f"Metadata file: {metadata_path}"
            )
            print(
                f"Error type: {type(error).__name__}"
            )
            print(
                f"Error details: {error}"
            )

    print()
    print("=" * 78)
    print(
        "SUPABASE UPLOAD RESULT"
    )
    print("=" * 78)
    print(
        "Uploaded with DB record: "
        f"{results['UPLOADED']}"
    )
    print(
        "Existing Storage file, DB record created: "
        f"{results['DATABASE_RECORD_CREATED']}"
    )
    print(
        "Database duplicates skipped: "
        f"{results['DUPLICATE_DATABASE']}"
    )
    print(
        f"Failed: {results['FAILED']}"
    )

    print()
    print(
        "Facebook image Supabase uploader finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Supabase image upload was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Facebook image Supabase uploader failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)