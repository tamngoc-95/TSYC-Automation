import os
import sys

from dotenv import load_dotenv
from supabase import Client, create_client


def get_required_environment_variable(variable_name: str) -> str:
    """Return a required environment variable or raise a clear error."""
    value = os.getenv(variable_name)

    if not value:
        raise RuntimeError(
            f"Required environment variable is missing: {variable_name}"
        )

    return value


def main() -> None:
    """Connect to Supabase and read the pilot batch."""
    load_dotenv()

    try:
        supabase_url = get_required_environment_variable("SUPABASE_URL")
        supabase_key = get_required_environment_variable("SUPABASE_KEY")

        supabase: Client = create_client(
            supabase_url,
            supabase_key,
        )

        response = (
            supabase.table("batches")
            .select(
                "batch_id, batch_code, batch_name, "
                "batch_status, created_at"
            )
            .eq("batch_code", "FB-2026-001")
            .execute()
        )

        if not response.data:
            print(
                "Connection succeeded, but batch FB-2026-001 "
                "was not found."
            )
            sys.exit(1)

        print("Supabase connection succeeded.")
        print("Pilot batch data:")

        for batch in response.data:
            print(f"  Batch code: {batch.get('batch_code')}")
            print(f"  Batch name: {batch.get('batch_name')}")
            print(f"  Status: {batch.get('batch_status')}")
            print(f"  Created at: {batch.get('created_at')}")

    except Exception as error:
        print("Supabase connection failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()