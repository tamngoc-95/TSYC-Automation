import argparse
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
UPLOADER_VERSION = "0.8.0"

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


def normalize_confirmation(
    value: str | None,
) -> str:
    """Normalize confirmation values across case, spaces, and hyphens."""
    if not value:
        return ""

    return (
        value.strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Upload selected Facebook images to Supabase Storage "
            "and create product_images records."
        )
    )

    parser.add_argument(
        "--candidate-code",
        help="Process one exact candidate code.",
    )

    parser.add_argument(
        "--candidate-id",
        help="Process one exact candidate UUID.",
    )

    parser.add_argument(
        "--raw-page-id",
        help="Process one exact Facebook raw page UUID.",
    )

    parser.add_argument(
        "--images",
        default=None,
        help=(
            "Image selection: ALL, one number, comma-separated numbers, "
            "or ranges such as 1,3-5."
        ),
    )

    parser.add_argument(
        "--confirm-upload",
        action="store_true",
        help="Confirm upload without an interactive prompt.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable input prompts. Requires a selector, --images, "
            "and --confirm-upload."
        ),
    )

    return parser.parse_args()


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



def build_metadata_item(
    metadata_path: Path,
) -> dict[str, Any]:
    """Load one metadata file and return a lightweight selection item."""
    metadata = load_json(
        metadata_path
    )

    raw_page_id = str(
        metadata.get("raw_page_id")
        or ""
    ).strip()

    if not raw_page_id:
        raise RuntimeError(
            "Metadata does not contain raw_page_id: "
            f"{metadata_path}"
        )

    return {
        "metadata_path": metadata_path,
        "metadata": metadata,
        "raw_page_id": raw_page_id,
        "source_url_id": str(
            metadata.get("source_url_id")
            or ""
        ).strip(),
        "facebook_post_url": str(
            metadata.get("facebook_post_url")
            or ""
        ).strip(),
        "image_hash": str(
            metadata.get("sha256")
            or ""
        ).strip().lower(),
        "width": metadata.get(
            "natural_width"
        ),
        "height": metadata.get(
            "natural_height"
        ),
    }


def build_metadata_items(
    metadata_files: list[Path],
) -> list[dict[str, Any]]:
    """Load all metadata files and skip invalid selection entries."""
    items: list[dict[str, Any]] = []

    for metadata_path in metadata_files:
        try:
            items.append(
                build_metadata_item(
                    metadata_path
                )
            )

        except Exception as error:
            print()
            print(
                "Skipping invalid metadata file:"
            )
            print(
                f"  File: {metadata_path}"
            )
            print(
                f"  Error: {error}"
            )

    return items


def group_metadata_items(
    items: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group metadata items by raw_page_id."""
    groups: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for item in items:
        raw_page_id = item[
            "raw_page_id"
        ]

        groups.setdefault(
            raw_page_id,
            [],
        ).append(
            item
        )

    for raw_page_id in groups:
        groups[raw_page_id] = sorted(
            groups[raw_page_id],
            key=lambda item: str(
                item["metadata_path"]
            ),
        )

    return groups


def get_candidate_for_raw_page(
    repository: SupabaseRepository,
    raw_page_id: str,
) -> dict[str, Any] | None:
    """Return one candidate linked to the selected raw page."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "candidate_code, "
            "extracted_title, "
            "workflow_status"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .order(
            "created_at",
            desc=True,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def get_raw_page_summary(
    repository: SupabaseRepository,
    raw_page_id: str,
) -> dict[str, Any] | None:
    """Return basic source details for one raw page."""
    response = (
        repository.client
        .table("raw_pages")
        .select(
            "raw_page_id, "
            "page_url, "
            "raw_title, "
            "collected_at"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def select_raw_page_group(
    repository: SupabaseRepository,
    groups: dict[str, list[dict[str, Any]]],
    candidate_code: str | None = None,
    candidate_id: str | None = None,
    raw_page_id: str | None = None,
    non_interactive: bool = False,
) -> tuple[
    str,
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    """
    Select exactly one raw-page image group.

    Explicit selectors are preferred for automation. Interactive selection
    remains available for manual operation.
    """
    if not groups:
        raise RuntimeError(
            "No valid image metadata groups were found."
        )

    group_entries: list[
        tuple[
            str,
            list[dict[str, Any]],
            dict[str, Any] | None,
        ]
    ] = []

    for current_raw_page_id in sorted(groups):
        items = groups[current_raw_page_id]

        candidate = get_candidate_for_raw_page(
            repository=repository,
            raw_page_id=current_raw_page_id,
        )

        if raw_page_id and current_raw_page_id != raw_page_id:
            continue

        if candidate_code:
            if not candidate or candidate.get("candidate_code") != candidate_code:
                continue

        if candidate_id:
            if not candidate or candidate.get("candidate_id") != candidate_id:
                continue

        group_entries.append(
            (
                current_raw_page_id,
                items,
                candidate,
            )
        )

    if raw_page_id or candidate_code or candidate_id:
        if not group_entries:
            raise RuntimeError(
                "No local Facebook image group matched the supplied selector."
            )

        if len(group_entries) > 1:
            raise RuntimeError(
                "The supplied selector matched multiple raw-page groups. "
                "Use --raw-page-id to select one exact post."
            )

        return group_entries[0]

    if non_interactive:
        raise RuntimeError(
            "--non-interactive requires --candidate-code, --candidate-id, "
            "or --raw-page-id."
        )

    print()
    print(
        "Facebook posts with local images:"
    )

    group_entries = []

    for index, current_raw_page_id in enumerate(
        sorted(groups),
        start=1,
    ):
        items = groups[current_raw_page_id]

        candidate = get_candidate_for_raw_page(
            repository=repository,
            raw_page_id=current_raw_page_id,
        )

        raw_page = get_raw_page_summary(
            repository=repository,
            raw_page_id=current_raw_page_id,
        )

        group_entries.append(
            (
                current_raw_page_id,
                items,
                candidate,
            )
        )

        candidate_code_value = (
            candidate.get("candidate_code")
            if candidate
            else "[not linked]"
        )

        candidate_title = (
            candidate.get("extracted_title")
            if candidate
            else "[candidate not found]"
        )

        page_url = (
            raw_page.get("page_url")
            if raw_page
            else (
                items[0].get("facebook_post_url")
                or "[URL not found]"
            )
        )

        print()
        print(
            f"[{index}] Raw page ID: {current_raw_page_id}"
        )
        print(
            f"    Candidate: {candidate_code_value}"
        )
        print(
            f"    Title: {candidate_title}"
        )
        print(
            f"    Local images: {len(items)}"
        )
        print(
            f"    URL: {page_url}"
        )

    print()

    selection = input(
        "Enter one post number to continue, "
        "or press Enter to cancel: "
    ).strip()

    if not selection:
        raise KeyboardInterrupt

    try:
        selected_index = int(selection) - 1

    except ValueError as error:
        raise ValueError(
            "The selected post number must be numeric."
        ) from error

    if (
        selected_index < 0
        or selected_index >= len(group_entries)
    ):
        raise ValueError(
            "The selected post number is outside the available range."
        )

    return group_entries[selected_index]


def filter_database_duplicates(
    repository: SupabaseRepository,
    items: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Split local metadata into uploadable and existing database images."""
    uploadable: list[
        dict[str, Any]
    ] = []

    duplicates: list[
        dict[str, Any]
    ] = []

    for item in items:
        existing_record = (
            find_existing_database_record(
                repository=repository,
                raw_page_id=item[
                    "raw_page_id"
                ],
                image_hash=item[
                    "image_hash"
                ],
            )
        )

        if existing_record is None:
            uploadable.append(
                item
            )

        else:
            duplicate_item = dict(
                item
            )

            duplicate_item[
                "existing_record"
            ] = existing_record

            duplicates.append(
                duplicate_item
            )

    return (
        uploadable,
        duplicates,
    )


def print_existing_duplicates(
    duplicates: list[dict[str, Any]],
) -> None:
    """Print local images already represented in product_images."""
    if not duplicates:
        return

    print()
    print(
        "Images already in the database "
        "and excluded from this upload:"
    )

    for duplicate in duplicates:
        existing = duplicate[
            "existing_record"
        ]

        print(
            "- "
            f"{duplicate['metadata_path'].name} "
            "-> image_id "
            f"{existing.get('image_id')} "
            f"({existing.get('image_status')})"
        )


def print_selectable_images(
    items: list[dict[str, Any]],
) -> None:
    """Print uploadable images from the selected raw page."""
    print()
    print(
        "Images available for selection:"
    )

    for index, item in enumerate(
        items,
        start=1,
    ):
        metadata_path = item[
            "metadata_path"
        ]

        try:
            image_path = resolve_local_image_path(
                metadata=item[
                    "metadata"
                ],
                metadata_path=metadata_path,
            )

            image_name = image_path.name

        except Exception:
            image_name = "[image file unresolved]"

        print(
            f"[{index}] {metadata_path.name}"
        )
        print(
            f"    Image: {image_name}"
        )
        print(
            "    Size: "
            f"{item.get('width')} x "
            f"{item.get('height')}"
        )
        print(
            "    SHA-256: "
            f"{item.get('image_hash')}"
        )


def parse_image_selection(
    selection: str,
    item_count: int,
) -> list[int]:
    """
    Parse ALL, one number, comma-separated numbers, or ranges.

    Examples: ALL, 1, 1,3, 1-3, 1,3-5.
    """
    normalized = selection.strip().upper()

    if normalized == "ALL":
        return list(
            range(item_count)
        )

    if not normalized:
        return []

    selected_indexes: set[
        int
    ] = set()

    for token in normalized.split(","):
        token = token.strip()

        if not token:
            continue

        if "-" in token:
            start_text, end_text = token.split(
                "-",
                1,
            )

            try:
                start_number = int(
                    start_text
                )

                end_number = int(
                    end_text
                )

            except ValueError as error:
                raise ValueError(
                    "Image ranges must contain numeric values."
                ) from error

            if start_number > end_number:
                raise ValueError(
                    "Image range start cannot exceed its end."
                )

            numbers = range(
                start_number,
                end_number + 1,
            )

        else:
            try:
                numbers = [
                    int(token)
                ]

            except ValueError as error:
                raise ValueError(
                    "Image selection must use numbers, "
                    "commas, ranges, or ALL."
                ) from error

        for number in numbers:
            if number < 1 or number > item_count:
                raise ValueError(
                    "Selected image number is outside "
                    "the available range: "
                    f"{number}"
                )

            selected_indexes.add(
                number - 1
            )

    return sorted(
        selected_indexes
    )


def select_images(
    items: list[dict[str, Any]],
    selection_value: str | None = None,
    non_interactive: bool = False,
) -> list[dict[str, Any]]:
    """Select images interactively or from a command-line selection."""
    if not items:
        return []

    print_selectable_images(
        items
    )

    print()

    if selection_value is not None:
        selection = selection_value.strip()

    elif non_interactive:
        raise RuntimeError(
            "--non-interactive requires --images."
        )

    else:
        selection = input(
            "Select images using ALL, one number, "
            "comma-separated numbers, or a range "
            "(example: 1,3-5). "
            "Press Enter to cancel: "
        ).strip()

    selected_indexes = parse_image_selection(
        selection=selection,
        item_count=len(items),
    )

    return [
        items[index]
        for index in selected_indexes
    ]


def validate_selected_group(
    selected_raw_page_id: str,
    selected_candidate: dict[str, Any] | None,
    selected_group_items: list[dict[str, Any]],
) -> None:
    """Validate that all selected metadata belongs to one raw page."""
    if not selected_group_items:
        raise RuntimeError(
            "Selected raw-page group contains no image metadata."
        )

    for item in selected_group_items:
        if item.get("raw_page_id") != selected_raw_page_id:
            raise RuntimeError(
                "Image metadata raw_page_id does not match the selected group."
            )

    source_url_ids = {
        item.get("source_url_id")
        for item in selected_group_items
        if item.get("source_url_id")
    }

    if len(source_url_ids) > 1:
        raise RuntimeError(
            "Selected image metadata contains multiple source_url_id values."
        )

    if selected_candidate is None:
        raise RuntimeError(
            "Selected raw page is not linked to a product candidate."
        )


def print_final_upload_plan(
    raw_page_id: str,
    candidate: dict[str, Any] | None,
    selected_items: list[dict[str, Any]],
) -> None:
    """Print the exact final upload plan before confirmation."""
    print()
    print("=" * 78)
    print(
        "FINAL SUPABASE UPLOAD PLAN"
    )
    print("=" * 78)
    print(
        f"Raw page ID: {raw_page_id}"
    )
    print(
        "Candidate: "
        + (
            str(
                candidate.get(
                    "candidate_code"
                )
            )
            if candidate
            else "[not linked]"
        )
    )
    print(
        "Candidate title: "
        + (
            str(
                candidate.get(
                    "extracted_title"
                )
            )
            if candidate
            else "[not found]"
        )
    )
    print(
        f"Images selected: {len(selected_items)}"
    )

    for index, item in enumerate(
        selected_items,
        start=1,
    ):
        print(
            f"[{index}] "
            f"{item['metadata_path'].name}"
        )
        print(
            "    SHA-256: "
            f"{item['image_hash']}"
        )

def main() -> None:
    """Upload selected Facebook images from one raw page to Supabase."""
    load_dotenv()

    args = parse_arguments()

    if args.candidate_code and args.candidate_id:
        raise RuntimeError(
            "Use either --candidate-code or --candidate-id, not both."
        )

    if args.non_interactive:
        if not (
            args.candidate_code
            or args.candidate_id
            or args.raw_page_id
        ):
            raise RuntimeError(
                "--non-interactive requires --candidate-code, "
                "--candidate-id, or --raw-page-id."
            )

        if args.images is None:
            raise RuntimeError(
                "--non-interactive requires --images."
            )

        if not args.confirm_upload:
            raise RuntimeError(
                "--non-interactive requires --confirm-upload."
            )

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

    metadata_items = build_metadata_items(
        metadata_files
    )

    groups = group_metadata_items(
        metadata_items
    )

    (
        selected_raw_page_id,
        selected_group_items,
        selected_candidate,
    ) = select_raw_page_group(
        repository=repository,
        groups=groups,
        candidate_code=args.candidate_code,
        candidate_id=args.candidate_id,
        raw_page_id=args.raw_page_id,
        non_interactive=args.non_interactive,
    )

    validate_selected_group(
        selected_raw_page_id=selected_raw_page_id,
        selected_candidate=selected_candidate,
        selected_group_items=selected_group_items,
    )

    (
        uploadable_items,
        duplicate_items,
    ) = filter_database_duplicates(
        repository=repository,
        items=selected_group_items,
    )

    print_existing_duplicates(
        duplicate_items
    )

    if not uploadable_items:
        print()
        print(
            "All local images for the selected post already "
            "exist in product_images. Nothing will be uploaded."
        )
        return

    selected_items = select_images(
        items=uploadable_items,
        selection_value=args.images,
        non_interactive=args.non_interactive,
    )

    if not selected_items:
        print(
            "No images were selected. "
            "Supabase upload cancelled."
        )
        return

    print_final_upload_plan(
        raw_page_id=selected_raw_page_id,
        candidate=selected_candidate,
        selected_items=selected_items,
    )

    print()

    if args.confirm_upload:
        confirmation = "UPLOAD"

    elif args.non_interactive:
        confirmation = ""

    else:
        confirmation = normalize_confirmation(
            input(
                "Type UPLOAD, CONFIRM, or UPLOAD_IMAGES to upload only "
                "the images listed above, or press Enter to cancel: "
            )
        )

    if confirmation not in {
        "UPLOAD",
        "CONFIRM",
        "UPLOAD_IMAGES",
    }:
        print()
        print(
            "Invalid confirmation. Use UPLOAD, CONFIRM, or UPLOAD_IMAGES."
        )
        print(
            f"Received value: {confirmation or '[empty]'}"
        )
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

    for item in selected_items:
        metadata_path = item[
            "metadata_path"
        ]

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
        "Database duplicates skipped during upload: "
        f"{results['DUPLICATE_DATABASE']}"
    )
    print(
        "Duplicates excluded before selection: "
        f"{len(duplicate_items)}"
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