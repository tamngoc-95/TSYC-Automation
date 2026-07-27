import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from src.repositories.supabase_repository import SupabaseRepository


def main() -> None:
    """Test the initial Supabase repository methods."""

    repository = SupabaseRepository()

    batch = repository.get_batch_by_code("FB-2026-001")

    if batch is None:
        print("Batch FB-2026-001 was not found.")
        sys.exit(1)

    print("Batch found successfully.")
    print(f"Batch ID: {batch['batch_id']}")
    print(f"Batch code: {batch['batch_code']}")
    print(f"Batch status: {batch['batch_status']}")

    log_record = repository.write_process_log(
        batch_id=batch["batch_id"],
        process_name="TEST_SUPABASE_REPOSITORY",
        process_step="READ_BATCH",
        log_level="INFO",
        status="SUCCESS",
        message=(
            "Supabase repository test completed successfully."
        ),
    )

    print("Process log created successfully.")
    print(f"Process log ID: {log_record['process_log_id']}")


if __name__ == "__main__":
    main()