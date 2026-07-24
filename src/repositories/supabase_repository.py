import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client


class SupabaseRepository:
    """Provide reusable Supabase database operations for the TSYC pipeline."""

    def __init__(self) -> None:
        """Initialize the Supabase client from environment variables."""
        load_dotenv()

        supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        supabase_key = os.getenv("SUPABASE_KEY", "").strip()

        if not supabase_url:
            raise RuntimeError(
                "Required environment variable is missing: SUPABASE_URL"
            )

        if not supabase_key:
            raise RuntimeError(
                "Required environment variable is missing: SUPABASE_KEY"
            )

        self.client: Client = create_client(
            supabase_url,
            supabase_key,
        )

    def get_batch_by_code(
        self,
        batch_code: str,
    ) -> dict[str, Any] | None:
        """Return one batch by its business code."""
        response = (
            self.client.table("batches")
            .select("*")
            .eq("batch_code", batch_code)
            .limit(1)
            .execute()
        )

        if not response.data:
            return None

        return response.data[0]

    def create_batch(
        self,
        batch_code: str,
        batch_name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a new batch and return the inserted record."""
        existing_batch = self.get_batch_by_code(batch_code)

        if existing_batch:
            raise ValueError(
                f"Batch already exists: {batch_code}"
            )

        payload = {
            "batch_code": batch_code,
            "batch_name": batch_name,
            "batch_status": "CREATED",
            "description": description,
        }

        response = (
            self.client.table("batches")
            .insert(payload)
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                f"Batch creation returned no data: {batch_code}"
            )

        return response.data[0]

    def save_source_url(
        self,
        batch_id: str,
        source_url: str,
        selection_reason: str | None = None,
        active: bool = True,
        source_type: str = "FACEBOOK_POST",
    ) -> dict[str, Any]:
        """Insert or update an authorized source URL."""
        payload = {
            "batch_id": batch_id,
            "source_type": source_type,
            "source_url": source_url,
            "source_name": selection_reason,
            "is_authorized": active,
            "crawl_status": "PENDING" if active else "SKIPPED",
        }

        response = (
            self.client.table("source_urls")
            .upsert(
                payload,
                on_conflict="batch_id,source_type,source_url",
            )
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                f"Source URL upsert returned no data: {source_url}"
            )

        return response.data[0]

    def get_pending_source_urls(
        self,
        batch_id: str,
        source_type: str = "FACEBOOK_POST",
    ) -> list[dict[str, Any]]:
        """Return authorized source URLs waiting to be collected."""
        response = (
            self.client.table("source_urls")
            .select(
                "source_url_id, batch_id, source_type, source_url, "
                "source_name, is_authorized, crawl_status"
            )
            .eq("batch_id", batch_id)
            .eq("source_type", source_type)
            .eq("is_authorized", True)
            .eq("crawl_status", "PENDING")
            .order("created_at")
            .execute()
        )

        return response.data or []

    def update_source_url_status(
        self,
        source_url_id: str,
        crawl_status: str,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        """Update the crawl status of one source URL."""
        allowed_statuses = {
            "PENDING",
            "IN_PROGRESS",
            "COLLECTED",
            "SKIPPED",
            "FAILED",
        }

        if crawl_status not in allowed_statuses:
            raise ValueError(
                f"Unsupported crawl status: {crawl_status}"
            )

        payload: dict[str, Any] = {
            "crawl_status": crawl_status,
            "last_error": last_error,
        }

        if crawl_status in {"COLLECTED", "FAILED"}:
            payload["last_crawled_at"] = (
                datetime.now(timezone.utc).isoformat()
            )

        response = (
            self.client.table("source_urls")
            .update(payload)
            .eq("source_url_id", source_url_id)
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                "Source URL status update returned no data: "
                f"{source_url_id}"
            )

        return response.data[0]

    def save_raw_page(
        self,
        batch_id: str,
        page_type: str,
        raw_text: str | None = None,
        source_url_id: str | None = None,
        page_url: str | None = None,
        raw_title: str | None = None,
        raw_html: str | None = None,
        content_hash: str | None = None,
        collector_name: str | None = None,
        collector_version: str | None = None,
    ) -> dict[str, Any]:
        """Save raw collected page content before AI processing."""
        payload = {
            "batch_id": batch_id,
            "source_url_id": source_url_id,
            "page_type": page_type,
            "page_url": page_url,
            "raw_title": raw_title,
            "raw_text": raw_text,
            "raw_html": raw_html,
            "content_hash": content_hash,
            "collector_name": collector_name,
            "collector_version": collector_version,
        }

        response = (
            self.client.table("raw_pages")
            .insert(payload)
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                "Raw page insertion returned no data."
            )

        return response.data[0]

    def save_candidate(
        self,
        batch_id: str,
        candidate_code: str,
        extracted_title: str,
        candidate_type: str = "SINGLE_BOOK",
        possible_isbn: str | None = None,
        combo_group_code: str | None = None,
        extraction_confidence: float | None = None,
        source_evidence: dict[str, Any] | None = None,
        review_required: bool = False,
        review_reason: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update one product candidate."""
        payload = {
            "batch_id": batch_id,
            "candidate_code": candidate_code,
            "candidate_type": candidate_type,
            "combo_group_code": combo_group_code,
            "extracted_title": extracted_title,
            "possible_isbn": possible_isbn,
            "identity_status": "IDENTITY_PENDING",
            "workflow_status": "EXTRACTED",
            "extraction_confidence": extraction_confidence,
            "source_evidence": source_evidence or {},
            "review_required": review_required,
            "review_reason": review_reason,
        }

        response = (
            self.client.table("product_candidates")
            .upsert(
                payload,
                on_conflict="candidate_code",
            )
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                f"Candidate upsert returned no data: {candidate_code}"
            )

        return response.data[0]

    def write_process_log(
        self,
        message: str,
        process_name: str,
        batch_id: str | None = None,
        candidate_id: str | None = None,
        process_step: str | None = None,
        log_level: str = "INFO",
        status: str | None = None,
        error_code: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one technical or business process log."""
        payload = {
            "batch_id": batch_id,
            "candidate_id": candidate_id,
            "process_name": process_name,
            "process_step": process_step,
            "log_level": log_level,
            "status": status,
            "message": message,
            "error_code": error_code,
            "error_details": error_details,
        }

        response = (
            self.client.table("process_logs")
            .insert(payload)
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                "Process log insertion returned no data."
            )

        return response.data[0]