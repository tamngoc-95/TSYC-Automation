import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from src.repositories.supabase_repository import SupabaseRepository


def main() -> None:
    """List authorized Facebook URLs waiting to be collected."""

    repository = SupabaseRepository()

    batch = repository.get_batch_by_code("FB-2026-001")

    if batch is None:
        print("Batch FB-2026-001 was not found.")
        sys.exit(1)

    source_urls = repository.get_pending_source_urls(
        batch_id=batch["batch_id"],
        source_type="FACEBOOK_POST",
    )

    print(f"Pending authorized Facebook URLs: {len(source_urls)}")

    for index, source in enumerate(source_urls, start=1):
        print()
        print(f"Source {index}")
        print(f"  Source URL ID: {source['source_url_id']}")
        print(f"  URL: {source['source_url']}")
        print(f"  Selection reason: {source['source_name']}")
        print(f"  Crawl status: {source['crawl_status']}")


if __name__ == "__main__":
    main()