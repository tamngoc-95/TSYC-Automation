import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


BATCH_CODE = "FB-2026-001"

REGISTRAR_NAME = "candidate_reference_source_registrar"
REGISTRAR_VERSION = "1.0.0"

SOURCE_TYPES = (
    "PUBLISHER",
    "AUTHORIZED_SUPPLIER",
    "BOOKSTORE",
    "FAHASA",
    "FACEBOOK",
    "OTHER",
)

DISCOVERY_METHODS = (
    "MANUAL",
    "SEARCH_ENGINE",
    "SITE_SEARCH",
    "ISBN_LOOKUP",
    "AI_ASSISTED",
    "OTHER",
)

TRACKING_QUERY_PARAMETERS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_",
    "source",
}

TRACKING_QUERY_PREFIXES = (
    "utm_",
    "_ga",
)


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Register one candidate reference source for the TSYC pipeline."
        )
    )

    selector_group = parser.add_mutually_exclusive_group()

    selector_group.add_argument(
        "--candidate-code",
        help="Exact candidate code.",
    )

    selector_group.add_argument(
        "--candidate-id",
        help="Exact candidate UUID.",
    )

    parser.add_argument(
        "--source-type",
        choices=SOURCE_TYPES,
        help="Reference source type.",
    )

    parser.add_argument(
        "--source-name",
        help="Readable source name.",
    )

    parser.add_argument(
        "--source-url",
        help="Reference source URL.",
    )

    parser.add_argument(
        "--discovery-method",
        choices=DISCOVERY_METHODS,
        help="How the reference source was discovered.",
    )

    parser.add_argument(
        "--discovery-query",
        help="Optional discovery query.",
    )

    parser.add_argument(
        "--discovery-rank",
        type=int,
        help="Optional positive discovery rank.",
    )

    parser.add_argument(
        "--discovery-confidence",
        type=float,
        help="Optional discovery confidence from 0 to 1.",
    )

    parser.add_argument(
        "--selection-reason",
        help="Optional reason for selecting this source.",
    )

    parser.add_argument(
        "--authorized",
        action="store_true",
        help="Mark the source as authorized/approved.",
    )

    parser.add_argument(
        "--select-for-crawl",
        action="store_true",
        help="Select this source for metadata crawling.",
    )

    parser.add_argument(
        "--confirm-register",
        action="store_true",
        help="Confirm registration without interactive input.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable prompts. Requires an exact candidate selector, "
            "source type, source URL, and --confirm-register."
        ),
    )

    return parser.parse_args()


def normalize_whitespace(
    value: str | None,
) -> str:
    """Normalize whitespace in a text value."""
    if not value:
        return ""

    return " ".join(
        value.split()
    ).strip()


def normalize_url(
    raw_url: str,
) -> str:
    """Normalize an HTTP URL and remove common tracking parameters."""
    value = raw_url.strip()

    if not value:
        raise ValueError(
            "Source URL cannot be empty."
        )

    if not re.match(
        r"^https?://",
        value,
        flags=re.IGNORECASE,
    ):
        value = f"https://{value}"

    parsed = urlsplit(
        value
    )

    if parsed.scheme.lower() not in {
        "http",
        "https",
    }:
        raise ValueError(
            "Source URL must use HTTP or HTTPS."
        )

    if not parsed.netloc:
        raise ValueError(
            "Source URL does not contain a valid domain."
        )

    scheme = parsed.scheme.lower()

    hostname = (
        parsed.hostname
        or ""
    ).lower()

    if not hostname:
        raise ValueError(
            "Source URL does not contain a hostname."
        )

    port = parsed.port

    if (
        port
        and not (
            scheme == "http"
            and port == 80
        )
        and not (
            scheme == "https"
            and port == 443
        )
    ):
        netloc = f"{hostname}:{port}"

    else:
        netloc = hostname

    path = re.sub(
        r"/{2,}",
        "/",
        parsed.path or "/",
    )

    if path != "/":
        path = path.rstrip("/")

    retained_query_items: list[
        tuple[str, str]
    ] = []

    for key, item_value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        normalized_key = key.lower()

        if normalized_key in TRACKING_QUERY_PARAMETERS:
            continue

        if any(
            normalized_key.startswith(prefix)
            for prefix in TRACKING_QUERY_PREFIXES
        ):
            continue

        retained_query_items.append(
            (
                key,
                item_value,
            )
        )

    retained_query_items.sort(
        key=lambda item: (
            item[0].lower(),
            item[1],
        )
    )

    normalized_query = urlencode(
        retained_query_items,
        doseq=True,
    )

    return urlunsplit(
        (
            scheme,
            netloc,
            path,
            normalized_query,
            "",
        )
    )


def get_batch(
    repository: SupabaseRepository,
) -> dict[str, Any]:
    """Return the configured batch."""
    response = (
        repository.client
        .table("batches")
        .select(
            "batch_id, batch_code"
        )
        .eq(
            "batch_code",
            BATCH_CODE,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            f"Batch was not found: {BATCH_CODE}"
        )

    return records[0]


def get_candidates(
    repository: SupabaseRepository,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Return active candidates for reference discovery."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "candidate_code, "
            "candidate_type, "
            "extracted_title, "
            "extracted_author, "
            "possible_isbn, "
            "identity_status, "
            "workflow_status, "
            "review_required"
        )
        .eq(
            "batch_id",
            batch_id,
        )
        .neq(
            "workflow_status",
            "REJECTED",
        )
        .order(
            "candidate_code",
            desc=False,
        )
        .execute()
    )

    return response.data or []


def resolve_candidate(
    candidates: list[dict[str, Any]],
    candidate_code: str | None,
    candidate_id: str | None,
    non_interactive: bool,
) -> dict[str, Any]:
    """Resolve exactly one candidate or fall back to manual selection."""
    if candidate_code or candidate_id:
        matches = [
            candidate
            for candidate in candidates
            if (
                candidate_code
                and candidate.get("candidate_code") == candidate_code
            )
            or (
                candidate_id
                and str(candidate.get("candidate_id")) == str(candidate_id)
            )
        ]

        if len(matches) != 1:
            raise RuntimeError(
                "Candidate selector did not resolve to exactly one record."
            )

        return matches[0]

    if non_interactive:
        raise RuntimeError(
            "--non-interactive requires --candidate-code or --candidate-id."
        )

    return select_candidate(
        candidates
    )


def select_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allow the user to select one candidate."""
    if not candidates:
        raise RuntimeError(
            "No active product candidates were found."
        )

    print()
    print("Available product candidates:")

    for index, candidate in enumerate(
        candidates,
        start=1,
    ):
        print()
        print(
            f"[{index}] "
            f"{candidate.get('candidate_code')}"
        )
        print(
            "    Title: "
            f"{candidate.get('extracted_title')}"
        )
        print(
            "    Author: "
            f"{candidate.get('extracted_author') or '[not found]'}"
        )
        print(
            "    ISBN: "
            f"{candidate.get('possible_isbn') or '[not found]'}"
        )
        print(
            "    Identity status: "
            f"{candidate.get('identity_status')}"
        )

    while True:
        print()

        selection = input(
            "Enter the candidate number "
            "[default: 1]: "
        ).strip()

        if not selection:
            return candidates[0]

        try:
            selected_index = int(
                selection
            ) - 1

        except ValueError:
            print(
                "Please enter a numeric candidate number."
            )
            continue

        if (
            selected_index < 0
            or selected_index >= len(candidates)
        ):
            print(
                "The selected candidate number is "
                "outside the available range."
            )
            continue

        return candidates[selected_index]


def select_from_options(
    title: str,
    options: tuple[str, ...],
    default_number: int,
) -> str:
    """Select one value from a controlled list."""
    print()
    print(title)

    for index, option in enumerate(
        options,
        start=1,
    ):
        default_marker = (
            " [default]"
            if index == default_number
            else ""
        )

        print(
            f"[{index}] "
            f"{option}"
            f"{default_marker}"
        )

    while True:
        selection = input(
            "Enter the option number "
            f"[default: {default_number}]: "
        ).strip()

        if not selection:
            return options[
                default_number - 1
            ]

        try:
            selected_index = int(
                selection
            ) - 1

        except ValueError:
            print(
                "Please enter a numeric option."
            )
            continue

        if (
            selected_index < 0
            or selected_index >= len(options)
        ):
            print(
                "The selected option is outside "
                "the available range."
            )
            continue

        return options[selected_index]


def prompt_required_text(
    prompt: str,
) -> str:
    """Read a required text value."""
    while True:
        value = input(
            f"{prompt}: "
        ).strip()

        if value:
            return value

        print(
            f"{prompt} is required."
        )


def prompt_optional_text(
    prompt: str,
    default_value: str | None = None,
) -> str | None:
    """Read optional text with an optional default."""
    if default_value:
        display_prompt = (
            f"{prompt} "
            f"[default: {default_value}]: "
        )

    else:
        display_prompt = (
            f"{prompt} [optional]: "
        )

    value = input(
        display_prompt
    ).strip()

    if value:
        return value

    return default_value


def prompt_optional_positive_integer(
    prompt: str,
) -> int | None:
    """Read an optional positive integer."""
    while True:
        value = input(
            f"{prompt} [optional]: "
        ).strip()

        if not value:
            return None

        try:
            result = int(
                value
            )

        except ValueError:
            print(
                "Please enter a whole number "
                "or press Enter to skip."
            )
            continue

        if result <= 0:
            print(
                "The value must be greater than zero."
            )
            continue

        return result


def prompt_optional_confidence() -> float | None:
    """Read an optional confidence score between zero and one."""
    while True:
        value = input(
            "Discovery confidence from 0 to 1 "
            "[optional]: "
        ).strip()

        if not value:
            return None

        value = value.replace(
            ",",
            ".",
        )

        try:
            result = float(
                value
            )

        except ValueError:
            print(
                "Please enter a number from 0 to 1 "
                "or press Enter to skip."
            )
            continue

        if not 0 <= result <= 1:
            print(
                "Confidence must be between 0 and 1."
            )
            continue

        return round(
            result,
            4,
        )


def prompt_boolean(
    prompt: str,
    default_value: bool,
) -> bool:
    """Read a yes/no value."""
    default_label = (
        "Y"
        if default_value
        else "N"
    )

    while True:
        value = input(
            f"{prompt} [default: {default_label}]: "
        ).strip().upper()

        if not value:
            return default_value

        if value in {
            "Y",
            "YES",
        }:
            return True

        if value in {
            "N",
            "NO",
        }:
            return False

        print(
            "Please enter Y or N."
        )


def get_default_source_name(
    source_type: str,
) -> str:
    """Return a readable default source name."""
    defaults = {
        "PUBLISHER": "Publisher",
        "AUTHORIZED_SUPPLIER": "Authorized Supplier",
        "BOOKSTORE": "Bookstore",
        "FAHASA": "Fahasa",
        "FACEBOOK": "Facebook",
        "OTHER": "Other",
    }

    return defaults[source_type]


def build_default_discovery_query(
    candidate: dict[str, Any],
) -> str:
    """Build a reusable search query from candidate metadata."""
    parts = [
        normalize_whitespace(
            candidate.get("extracted_title")
        ),
        normalize_whitespace(
            candidate.get("extracted_author")
        ),
        normalize_whitespace(
            candidate.get("possible_isbn")
        ),
    ]

    return " ".join(
        part
        for part in parts
        if part
    )


def build_registration_from_arguments(
    args: argparse.Namespace,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Build a validated registration payload from CLI arguments."""
    if not args.source_type:
        raise RuntimeError(
            "--source-type is required in non-interactive mode."
        )

    if not args.source_url:
        raise RuntimeError(
            "--source-url is required in non-interactive mode."
        )

    if (
        args.discovery_rank is not None
        and args.discovery_rank <= 0
    ):
        raise RuntimeError(
            "--discovery-rank must be greater than zero."
        )

    if (
        args.discovery_confidence is not None
        and not 0 <= args.discovery_confidence <= 1
    ):
        raise RuntimeError(
            "--discovery-confidence must be between 0 and 1."
        )

    source_name = (
        normalize_whitespace(
            args.source_name
        )
        or get_default_source_name(
            args.source_type
        )
    )

    discovery_method = (
        args.discovery_method
        or "MANUAL"
    )

    discovery_query = (
        normalize_whitespace(
            args.discovery_query
        )
        or build_default_discovery_query(
            candidate
        )
        or None
    )

    # Authorization must always be explicit in automation.
    # Public Facebook/OTHER sources must never be auto-authorized.
    is_authorized = bool(
        args.authorized
    )

    return {
        "source_type": args.source_type,
        "source_name": source_name,
        "source_url": normalize_url(
            args.source_url
        ),
        "is_authorized": is_authorized,
        "discovery_method": discovery_method,
        "discovery_query": discovery_query,
        "discovery_rank": args.discovery_rank,
        "discovery_confidence": (
            round(
                args.discovery_confidence,
                4,
            )
            if args.discovery_confidence is not None
            else None
        ),
        "discovery_status": (
            "SELECTED"
            if args.select_for_crawl
            else "DISCOVERED"
        ),
        "selection_reason": normalize_whitespace(
            args.selection_reason
        ) or None,
        "is_selected_for_crawl": bool(
            args.select_for_crawl
        ),
    }


def collect_registration_input(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Collect one candidate-reference URL relationship."""
    print()
    print("=" * 78)
    print("REFERENCE SOURCE REGISTRATION")
    print("=" * 78)
    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )
    print(
        "Title: "
        f"{candidate.get('extracted_title')}"
    )
    print(
        "Author: "
        f"{candidate.get('extracted_author') or '[not found]'}"
    )

    source_type = select_from_options(
        title="Reference source types:",
        options=SOURCE_TYPES,
        default_number=4,
    )

    source_name = prompt_optional_text(
        prompt="Source name",
        default_value=get_default_source_name(
            source_type
        ),
    )

    raw_source_url = prompt_required_text(
        "Source URL"
    )

    source_url = normalize_url(
        raw_source_url
    )

    discovery_method = select_from_options(
        title="Discovery methods:",
        options=DISCOVERY_METHODS,
        default_number=1,
    )

    default_query = build_default_discovery_query(
        candidate
    )

    discovery_query = prompt_optional_text(
        prompt="Discovery query",
        default_value=default_query or None,
    )

    discovery_rank = prompt_optional_positive_integer(
        "Discovery rank"
    )

    discovery_confidence = (
        prompt_optional_confidence()
    )

    selection_reason = prompt_optional_text(
        "Selection reason"
    )

    is_authorized = prompt_boolean(
        "Is this an authorized or approved source?",
        default_value=(
            source_type
            in {
                "PUBLISHER",
                "AUTHORIZED_SUPPLIER",
                "BOOKSTORE",
                "FAHASA",
            }
        ),
    )

    is_selected_for_crawl = prompt_boolean(
        "Select this URL for metadata crawling?",
        default_value=True,
    )

    discovery_status = (
        "SELECTED"
        if is_selected_for_crawl
        else "DISCOVERED"
    )

    return {
        "source_type": source_type,
        "source_name": source_name,
        "source_url": source_url,
        "is_authorized": is_authorized,
        "discovery_method": discovery_method,
        "discovery_query": discovery_query,
        "discovery_rank": discovery_rank,
        "discovery_confidence": discovery_confidence,
        "discovery_status": discovery_status,
        "selection_reason": selection_reason,
        "is_selected_for_crawl": (
            is_selected_for_crawl
        ),
    }


def find_existing_source_url(
    repository: SupabaseRepository,
    batch_id: str,
    source_type: str,
    source_url: str,
) -> dict[str, Any] | None:
    """Find an existing URL in the same batch."""
    response = (
        repository.client
        .table("source_urls")
        .select(
            "source_url_id, "
            "batch_id, "
            "source_type, "
            "source_url, "
            "source_name, "
            "is_authorized, "
            "crawl_status"
        )
        .eq(
            "batch_id",
            batch_id,
        )
        .eq(
            "source_type",
            source_type,
        )
        .eq(
            "source_url",
            source_url,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def insert_source_url(
    repository: SupabaseRepository,
    batch_id: str,
    registration: dict[str, Any],
) -> dict[str, Any]:
    """Insert one source URL."""
    payload = {
        "batch_id": batch_id,
        "source_type": registration[
            "source_type"
        ],
        "source_url": registration[
            "source_url"
        ],
        "source_name": registration.get(
            "source_name"
        ),
        "is_authorized": registration[
            "is_authorized"
        ],
        "crawl_status": "PENDING",
        "last_error": None,
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    response = (
        repository.client
        .table("source_urls")
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Source URL insert returned no record."
        )

    return records[0]


def get_or_create_source_url(
    repository: SupabaseRepository,
    batch_id: str,
    registration: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return an existing URL or create a new one."""
    existing_source = find_existing_source_url(
        repository=repository,
        batch_id=batch_id,
        source_type=registration[
            "source_type"
        ],
        source_url=registration[
            "source_url"
        ],
    )

    if existing_source is not None:
        return existing_source, False

    created_source = insert_source_url(
        repository=repository,
        batch_id=batch_id,
        registration=registration,
    )

    return created_source, True


def find_existing_discovery(
    repository: SupabaseRepository,
    candidate_id: str,
    source_url_id: str,
) -> dict[str, Any] | None:
    """Find an existing candidate-to-URL relationship."""
    response = (
        repository.client
        .table(
            "candidate_reference_sources"
        )
        .select(
            "discovery_id, "
            "candidate_id, "
            "source_url_id, "
            "discovery_method, "
            "discovery_query, "
            "discovery_rank, "
            "discovery_confidence, "
            "discovery_status, "
            "selection_reason, "
            "is_selected_for_crawl"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "source_url_id",
            source_url_id,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def insert_discovery(
    repository: SupabaseRepository,
    candidate_id: str,
    source_url_id: str,
    registration: dict[str, Any],
) -> dict[str, Any]:
    """Insert one candidate reference-source discovery record."""
    now = datetime.now(
        timezone.utc
    ).isoformat()

    payload = {
        "candidate_id": candidate_id,
        "source_url_id": source_url_id,
        "discovery_method": registration[
            "discovery_method"
        ],
        "discovery_query": registration.get(
            "discovery_query"
        ),
        "discovery_rank": registration.get(
            "discovery_rank"
        ),
        "discovery_confidence": registration.get(
            "discovery_confidence"
        ),
        "discovery_status": registration[
            "discovery_status"
        ],
        "selection_reason": registration.get(
            "selection_reason"
        ),
        "is_selected_for_crawl": registration[
            "is_selected_for_crawl"
        ],
        "reviewed_at": (
            now
            if registration[
                "is_selected_for_crawl"
            ]
            else None
        ),
        "updated_at": now,
    }

    response = (
        repository.client
        .table(
            "candidate_reference_sources"
        )
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Discovery insert returned no record."
        )

    return records[0]


def delete_source_url(
    repository: SupabaseRepository,
    source_url_id: str,
) -> None:
    """Remove a source URL created during a failed registration."""
    (
        repository.client
        .table("source_urls")
        .delete()
        .eq(
            "source_url_id",
            source_url_id,
        )
        .execute()
    )


def verify_registration(
    repository: SupabaseRepository,
    discovery_id: str,
) -> dict[str, Any]:
    """Read the discovery and its source URL back."""
    discovery_response = (
        repository.client
        .table(
            "candidate_reference_sources"
        )
        .select(
            "discovery_id, "
            "candidate_id, "
            "source_url_id, "
            "discovery_method, "
            "discovery_query, "
            "discovery_rank, "
            "discovery_confidence, "
            "discovery_status, "
            "selection_reason, "
            "is_selected_for_crawl"
        )
        .eq(
            "discovery_id",
            discovery_id,
        )
        .limit(1)
        .execute()
    )

    discovery_records = (
        discovery_response.data
        or []
    )

    if not discovery_records:
        raise RuntimeError(
            "Discovery verification failed."
        )

    discovery = discovery_records[0]

    source_response = (
        repository.client
        .table("source_urls")
        .select(
            "source_url_id, "
            "batch_id, "
            "source_type, "
            "source_url, "
            "source_name, "
            "is_authorized, "
            "crawl_status"
        )
        .eq(
            "source_url_id",
            discovery["source_url_id"],
        )
        .limit(1)
        .execute()
    )

    source_records = (
        source_response.data
        or []
    )

    if not source_records:
        raise RuntimeError(
            "Source URL verification failed."
        )

    return {
        "discovery": discovery,
        "source": source_records[0],
    }


def print_registration_preview(
    candidate: dict[str, Any],
    registration: dict[str, Any],
) -> None:
    """Print the proposed source registration."""
    print()
    print("=" * 78)
    print("REFERENCE SOURCE PREVIEW")
    print("=" * 78)
    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )
    print(
        "Source type: "
        f"{registration.get('source_type')}"
    )
    print(
        "Source name: "
        f"{registration.get('source_name')}"
    )
    print(
        "Normalized URL: "
        f"{registration.get('source_url')}"
    )
    print(
        "Authorized: "
        f"{registration.get('is_authorized')}"
    )
    print(
        "Discovery method: "
        f"{registration.get('discovery_method')}"
    )
    print(
        "Discovery query: "
        f"{registration.get('discovery_query') or '[none]'}"
    )
    print(
        "Discovery rank: "
        f"{registration.get('discovery_rank') or '[none]'}"
    )
    print(
        "Discovery confidence: "
        f"{registration.get('discovery_confidence')}"
    )
    print(
        "Discovery status: "
        f"{registration.get('discovery_status')}"
    )
    print(
        "Selected for crawl: "
        f"{registration.get('is_selected_for_crawl')}"
    )


def main() -> None:
    """Register one reference URL for one product candidate."""
    load_dotenv()
    args = parse_arguments()

    if args.non_interactive:
        if not args.confirm_register:
            raise RuntimeError(
                "--non-interactive requires --confirm-register."
            )

        if not (
            args.candidate_code
            or args.candidate_id
        ):
            raise RuntimeError(
                "--non-interactive requires an exact candidate selector."
            )

    print(
        "Candidate reference source registrar started."
    )
    print(
        f"Version: {REGISTRAR_VERSION}"
    )
    print(
        f"Batch: {BATCH_CODE}"
    )

    repository = SupabaseRepository()

    batch = get_batch(
        repository
    )

    candidates = get_candidates(
        repository=repository,
        batch_id=batch["batch_id"],
    )

    print(
        "Candidates found: "
        f"{len(candidates)}"
    )

    candidate = resolve_candidate(
        candidates=candidates,
        candidate_code=args.candidate_code,
        candidate_id=args.candidate_id,
        non_interactive=args.non_interactive,
    )

    if args.non_interactive:
        registration = build_registration_from_arguments(
            args=args,
            candidate=candidate,
        )

    else:
        registration = collect_registration_input(
            candidate
        )

    print_registration_preview(
        candidate=candidate,
        registration=registration,
    )

    print()

    if args.confirm_register:
        confirmation = "REGISTER"

    elif args.non_interactive:
        confirmation = ""

    else:
        confirmation = input(
            "Type REGISTER to save this reference source, "
            "or press Enter to cancel: "
        ).strip().upper()

    if confirmation != "REGISTER":
        print(
            "Reference source registration cancelled."
        )
        return

    source_record, source_created = (
        get_or_create_source_url(
            repository=repository,
            batch_id=batch["batch_id"],
            registration=registration,
        )
    )

    source_url_id = str(
        source_record["source_url_id"]
    )

    print()
    print(
        "Source URL: "
        + (
            "CREATED"
            if source_created
            else "REUSED"
        )
    )
    print(
        f"Source URL ID: {source_url_id}"
    )

    existing_discovery = (
        find_existing_discovery(
            repository=repository,
            candidate_id=candidate[
                "candidate_id"
            ],
            source_url_id=source_url_id,
        )
    )

    if existing_discovery is not None:
        print()
        print("=" * 78)
        print("DUPLICATE DISCOVERY")
        print("=" * 78)
        print(
            "Discovery ID: "
            f"{existing_discovery.get('discovery_id')}"
        )
        print(
            "Discovery status: "
            f"{existing_discovery.get('discovery_status')}"
        )
        print(
            "Selected for crawl: "
            f"{existing_discovery.get('is_selected_for_crawl')}"
        )
        return

    try:
        discovery_record = insert_discovery(
            repository=repository,
            candidate_id=candidate[
                "candidate_id"
            ],
            source_url_id=source_url_id,
            registration=registration,
        )

        verified = verify_registration(
            repository=repository,
            discovery_id=discovery_record[
                "discovery_id"
            ],
        )

    except Exception:
        if source_created:
            print()
            print(
                "Discovery creation failed. "
                "Removing the newly created source URL..."
            )

            try:
                delete_source_url(
                    repository=repository,
                    source_url_id=source_url_id,
                )

                print(
                    "Source URL rollback completed."
                )

            except Exception as rollback_error:
                print(
                    "Warning: source URL rollback failed."
                )
                print(
                    "Rollback details: "
                    f"{rollback_error}"
                )

        raise

    verified_source = verified[
        "source"
    ]

    verified_discovery = verified[
        "discovery"
    ]

    print()
    print("=" * 78)
    print("REFERENCE SOURCE RESULT")
    print("=" * 78)
    print(
        "Source URL ID: "
        f"{verified_source.get('source_url_id')}"
    )
    print(
        "Source type: "
        f"{verified_source.get('source_type')}"
    )
    print(
        "Source URL: "
        f"{verified_source.get('source_url')}"
    )
    print(
        "Crawl status: "
        f"{verified_source.get('crawl_status')}"
    )
    print(
        "Discovery ID: "
        f"{verified_discovery.get('discovery_id')}"
    )
    print(
        "Discovery method: "
        f"{verified_discovery.get('discovery_method')}"
    )
    print(
        "Discovery status: "
        f"{verified_discovery.get('discovery_status')}"
    )
    print(
        "Selected for crawl: "
        f"{verified_discovery.get('is_selected_for_crawl')}"
    )

    print()
    print(
        "Candidate reference source registrar finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Reference source registration was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Candidate reference source registration failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)