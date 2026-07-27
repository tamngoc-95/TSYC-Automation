import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from src.repositories.supabase_repository import SupabaseRepository


def main() -> None:
    """Test raw page and candidate repository operations."""

    repository = SupabaseRepository()

    batch = repository.get_batch_by_code("FB-2026-001")

    if batch is None:
        print("Batch FB-2026-001 was not found.")
        sys.exit(1)

    raw_page = repository.save_raw_page(
        batch_id=batch["batch_id"],
        page_type="FACEBOOK_POST",
        page_url="https://example.com/test-facebook-post",
        raw_title="Test Facebook Post",
        raw_text="Temporary raw post content for repository testing.",
        collector_name="manual_test",
        collector_version="1.0",
    )

    print("Raw page created successfully.")
    print(f"Raw page ID: {raw_page['raw_page_id']}")

    candidate = repository.save_candidate(
        batch_id=batch["batch_id"],
        candidate_code="FB-2026-001-CAN-TEST-0001",
        extracted_title="Temporary Test Book",
        candidate_type="SINGLE_BOOK",
        extraction_confidence=0.90,
        source_evidence={
            "raw_page_id": raw_page["raw_page_id"],
            "source_type": "FACEBOOK_POST",
        },
        review_required=False,
    )

    print("Candidate created successfully.")
    print(f"Candidate ID: {candidate['candidate_id']}")
    print(f"Candidate code: {candidate['candidate_code']}")
    print(f"Title: {candidate['extracted_title']}")

    repository.write_process_log(
        batch_id=batch["batch_id"],
        candidate_id=candidate["candidate_id"],
        process_name="TEST_CANDIDATE_AND_RAW_PAGE",
        process_step="SAVE_TEST_RECORDS",
        log_level="INFO",
        status="SUCCESS",
        message="Raw page and candidate test completed successfully.",
    )

    print("Test process log created successfully.")


if __name__ == "__main__":
    main()