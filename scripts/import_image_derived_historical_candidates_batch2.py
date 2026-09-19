"""
SECOND BOUNDED image-derived historical Facebook candidate import into
Supabase -- see import_image_derived_historical_candidates.py (batch 1,
record #1018) for the full methodology explanation. This script mirrors
that one's exact repository patterns and FK chain (batches -> source_urls
-> raw_pages -> product_candidates -> process_logs), targeting a
different historical record.

Scope of this run (explicit, hardcoded, bounded):
    historical record #1399 only. Record #1399 alone supplied 50
    SAFE_NEW candidates in deterministic inventory order (the
    requested cap for this batch), so record #1136 -- next in this
    batch's source priority -- contributes zero to this specific run.

Six items from record #1399's raw inventory were excluded before this
list was built:
    - "Bí Mật Của Một Trí Nhớ Siêu Phàm" (Eran Katz) -- already exists
      as FB-2026-001-CAN-0001
    - "Muôn Kiếp Nhân Sinh" (Nguyên Phong) -- already exists as
      FB-HIST-2026-002-CAN-0004; appeared twice in this post's own
      images, both instances excluded
    - "Bạn Đắt Giá Bao Nhiêu?" (Văn Tình) -- already exists as
      FB-HIST-2026-002-CAN-0012
    - "Chữa Lành Đứa Trẻ Bên Trong Bạn" (Charles Whitfield) -- already
      exists as FB-HIST-2026-001-CAN-0009
    - "Luật Tâm Thức" (Ngô Sa Thạch) appeared twice in this post's own
      images; the first occurrence is kept as the one candidate, the
      second is a within-post duplicate photo and is excluded

Never fabricates ISBN, publisher, weight, dimensions, or reference
matches (CLAUDE.md section 2.2) -- extracted_author is populated only
where an author name was actually legible on the image.

Idempotency: identical to batch 1 -- product_candidates is deduped by
(raw_page_id, normalized title, candidate_type) before any insert;
raw_pages by (batch_id, content_hash); source_urls by
upsert-on-conflict.

Usage:
    .venv/Scripts/python.exe scripts/import_image_derived_historical_candidates_batch2.py
    .venv/Scripts/python.exe scripts/import_image_derived_historical_candidates_batch2.py --execute
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.rules.historical_text_cleaner import clean_historical_facebook_text
from src.repositories.supabase_repository import SupabaseRepository
from src.services.facebook_history_parser import load_facebook_history_export

configure_utf8_console()

SCRIPT_VERSION = "1.0.0"

BATCH_CODE = "FB-HIST-2026-IMG-002"
BATCH_NAME = "Historical Facebook export -- image-derived candidates, second bounded import"
BATCH_DESCRIPTION = (
    "Second bounded import of NEW_HIGH_CONFIDENCE historical candidates "
    "found by direct visual review of source images from historical "
    "Facebook export records not covered by the original three "
    "FB-HIST-2026-001/002/AUTOIMPORT batches, nor by IMG-001. Source "
    "record: #1399 (record #1136 was next in this batch's source "
    "priority but was not needed -- #1399 alone reached the 50-candidate "
    "cap)."
)
IMPORT_TYPE = "HISTORICAL_FACEBOOK_EXPORT_IMAGE_DERIVED"
IMPORTER_NAME = "image_derived_historical_candidate_importer"

DEFAULT_SOURCE_EXPORT_LABEL = (
    "your_facebook_activity/posts/"
    "your_posts__check_ins__photos_and_videos_1.html"
)
DEFAULT_SOURCE_EXPORT = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "facebook_export_probe"
    / "your_facebook_activity"
    / "posts"
    / "your_posts__check_ins__photos_and_videos_1.html"
)
MEDIA_PATH_PREFIX = (
    "your_facebook_activity/posts/media/Tailentudidong_1391983851115144/"
)

CANDIDATE_CODE_PATTERN = re.compile(rf"^{re.escape(BATCH_CODE)}-CAN-(\d+)$")

EXTRACTION_SOURCE = "MANUAL_VISUAL_REVIEW"
EXTRACTOR_NAME = "claude_sonnet_5_vision_manual_review"
EXTRACTOR_VERSION = "2026-09-19-session"

REVIEW_REASON = (
    "Candidate was found by direct visual review of one historical "
    "Facebook export record's images (image-derived recovery batch; "
    "see source_evidence.historical_import_key and local_media_paths). "
    "Book identity, ISBN, publisher, weight, dimensions, and reference "
    "matches still require verification -- none were fabricated at "
    "import time."
)

HISTORICAL_RECORD_ID = "1399"
HISTORICAL_POST_DATE = "Tháng 4 05, 2025 8:40:42 ch"

# --- the exact, hardcoded, bounded selection for this second image-derived
# batch: 50 SAFE_NEW items from record #1399, in the deterministic order
# they were encountered during this session's review (original
# individually-inspected images first, then sheet-by-sheet contact-sheet
# order, sorted-filename order within each). Excluded items (see module
# docstring) are simply absent from this list, not represented as skipped
# entries -- consistent with batch 1's convention.
SELECTED_ITEMS: list[dict[str, Any]] = [
    {"title": "Tư Duy Ngược", "type": "SINGLE_BOOK", "author": "Nguyễn Anh Dũng", "image": "3893721830941321.jpg", "evidence": "15,99€ visible, NXB Dân Trí/Sbooks"},
    {"title": "Người Mẹ Tốt Hơn Là Người Thầy Tốt", "type": "SINGLE_BOOK", "author": "Doãn Kiến Lợi", "image": "3893721847607986.jpg", "evidence": "23,99€ visible, NXB Văn Học"},
    {"title": "Người Bà Tài Giỏi Vùng Saga", "type": "SINGLE_BOOK", "author": "Yoshichi Shimada; Bảo Lam Anh (dịch)", "image": "3893721897607981.jpg", "evidence": "14,99€ visible, NXB Thanh Niên"},
    {"title": "Mặt Dày Tâm Đen", "type": "SINGLE_BOOK", "author": "Chin-Ning Chu; Vũ Thái Hà, Trần Lan Anh, Trần Thị Thùy Trang (dịch)", "image": "3893722047607966.jpg", "evidence": "19,99€ visible, ZenBooks/NXB Hồng Đức"},
    {"title": "Đàn Ông Sao Hỏa Đàn Bà Sao Kim", "type": "SINGLE_BOOK", "author": "John Gray", "image": "3893722447607926.jpg", "evidence": "21€ visible"},
    {"title": "Phụ Nữ (The Book of Women)", "type": "SINGLE_BOOK", "author": "Osho; Thanh Huyền (dịch)", "image": "3893723000941204.jpg", "evidence": "14,99€ visible, NXB Hà Nội/ThaiHaBooks"},
    {"title": "Khủng Hoảng Tuổi Chập Chững", "type": "SINGLE_BOOK", "author": "Harvey Karp, Paula Spencer; Thanh Minh (dịch)", "image": "3893723514274486.jpg", "evidence": "19,99€ visible"},
    {"title": "Sợ Hãi (Fear)", "type": "SINGLE_BOOK", "author": "Thích Nhất Hạnh; Chân Đạt (dịch)", "image": "3893724284274409.jpg", "evidence": "14,99€ visible"},
    {"title": "Bước Đệm Vững Chắc Vào Đời / Chào Con! Ba Mẹ Đã Sẵn Sàng!", "type": "BOOK_COMBO", "author": "BS. Trần Thị Huyên Thảo", "image": "3893724707607700.jpg", "evidence": "2-volume combo, 14,99€/cuốn, 28€/combo visible, NXB Trẻ"},
    {"title": "Bác Sĩ Của Con - Chỉ Dẫn Sức Khỏe Từ A-Z", "type": "SINGLE_BOOK", "author": None, "image": "3893721924274645.jpg", "evidence": "19,99€ visible, American Academy of Pediatrics"},
    {"title": "Gia Đình Tỉnh Thức", "type": "SINGLE_BOOK", "author": "Shefali Tsabary", "image": "3893721974274640.jpg", "evidence": "cover photographed"},
    {"title": "8 Loại Hình Thông Minh", "type": "SINGLE_BOOK", "author": "Kathy Koch PhD; Trương Quốc Vượng (dịch)", "image": "3893721997607971.jpg", "evidence": "17,99€ visible"},
    {"title": "Không Diệt Không Sinh Đừng Sợ Hãi", "type": "SINGLE_BOOK", "author": "Thích Nhất Hạnh; Chân Huyền (dịch)", "image": "3893722084274629.jpg", "evidence": "cover photographed"},
    {"title": "Tuổi Trẻ Đáng Giá Bao Nhiêu?", "type": "SINGLE_BOOK", "author": "Rosie Nguyễn", "image": "3893722140941290.jpg", "evidence": "cover photographed"},
    {"title": "Miễn Dịch", "type": "SINGLE_BOOK", "author": "Philipp Dettmer; Vân Nguyên, Quý Tiến (dịch)", "image": "3893722190941285.jpg", "evidence": "cover photographed, nhà sáng lập kênh Kurzgesagt"},
    {"title": "Luật Tâm Thức", "type": "SINGLE_BOOK", "author": "Ngô Sa Thạch", "image": "3893722230941281.jpg", "evidence": "27,79€ visible"},
    {"title": "Thay Đổi Cuộc Sống Với Thần Số Học", "type": "SINGLE_BOOK", "author": None, "image": "3893722254274612.jpg", "evidence": "cover photographed"},
    {"title": "Tiền Đẻ Ra Tiền", "type": "SINGLE_BOOK", "author": "Duncan Bannatyne", "image": "3893722324274605.jpg", "evidence": "cover photographed"},
    {"title": "Phương Pháp Học Tập Không Giới Hạn", "type": "SINGLE_BOOK", "author": "Jim Kwik", "image": "3893722364274601.jpg", "evidence": "cover photographed"},
    {"title": "Seneca - Những Bức Thư Đạo Đức", "type": "BOOK_COMBO", "author": "Seneca", "image": "3893722387607932.jpg", "evidence": "2-volume combo, 36€/Combo visible"},
    {"title": "Vẻ Đẹp Của Cảnh Sắc Tầm Thường", "type": "SINGLE_BOOK", "author": "Đặng Hoàng Giang", "image": "3893722467607924.jpg", "evidence": "cover photographed"},
    {"title": "Hãy Chăm Sóc Mẹ / Hãy Vẽ Với Cha", "type": "BOOK_COMBO", "author": "Shin Kyung-sook", "image": "3893722510941253.jpg", "evidence": "2-volume combo, 33€/combo visible"},
    {"title": "Trò Chuyện Với Vĩ Nhân (Meetings With Remarkable People)", "type": "SINGLE_BOOK", "author": "Osho", "image": "3893722534274584.jpg", "evidence": "20,99€ visible, First News"},
    {"title": "Ăn Dặm Không Nước Mắt", "type": "SINGLE_BOOK", "author": "Nguyễn Thị Ninh (mẹ Xoài)", "image": "3893722584274579.jpg", "evidence": "13,99€ visible"},
    {"title": "Giúp Con Nói \"Không\" Với Đường", "type": "SINGLE_BOOK", "author": None, "image": "3893722617607909.jpg", "evidence": "cover photographed"},
    {"title": "The Magic - Phép Màu", "type": "SINGLE_BOOK", "author": None, "image": "3893722697607901.jpg", "evidence": "22,99€ visible"},
    {"title": "The Power of Habit / Sức Mạnh Của Thói Quen", "type": "SINGLE_BOOK", "author": "Charles Duhigg; Lê Thảo Uyên (dịch)", "image": "3893722717607899.jpg", "evidence": "18,99€ visible, Alpha Books"},
    {"title": "Gieo Thói Quen Nhỏ Gặt Thành Công Lớn", "type": "SINGLE_BOOK", "author": "Stephen Guise; Trần Quang Vinh (dịch)", "image": "3893722784274559.jpg", "evidence": "13,99€ visible"},
    {"title": "Khi Lỗi Thuộc Về Những Vì Sao (The Fault in Our Stars)", "type": "SINGLE_BOOK", "author": None, "image": "3893722834274554.jpg", "evidence": "cover photographed"},
    {"title": "The Power of Your Subconscious Mind / Sức Mạnh Tiềm Thức", "type": "SINGLE_BOOK", "author": None, "image": "3893722854274552.jpg", "evidence": "16,99€ visible"},
    {"title": "Ăn Dặm Kiểu Nhật", "type": "SINGLE_BOOK", "author": "Tsutsumi Chiharu (chủ biên); Nguyễn Thị Hoa (dịch)", "image": "3893722910941213.jpg", "evidence": "17,99€ visible"},
    {"title": "Đánh Thức Con Người Phi Thường Trong Bạn", "type": "SINGLE_BOOK", "author": "Anthony Robbins; TriBookers (dịch)", "image": "3893722947607876.jpg", "evidence": "18,99€ visible"},
    {"title": "Việc 12 Tháng Làm Trong 12 Tuần", "type": "SINGLE_BOOK", "author": "Brian P. Moran, Michael Lennington; Quế Chi (dịch)", "image": "3893723014274536.jpg", "evidence": "cover photographed"},
    {"title": "Con Đường Chuyển Hóa", "type": "SINGLE_BOOK", "author": "Thích Nhất Hạnh", "image": "3893723064274531.jpg", "evidence": "13,99€ visible"},
    {"title": "Nuôi Dạy Em Bé Có Chính Kiến", "type": "SINGLE_BOOK", "author": None, "image": "3893723084274529.jpg", "evidence": "14,99€ visible"},
    {"title": "Phương Pháp Dạy Con Không Đòn Roi", "type": "SINGLE_BOOK", "author": "Daniel J. Siegel, Tina Payne Bryson; Linh Vũ (dịch)", "image": "3893723124274525.jpg", "evidence": "cover photographed"},
    {"title": "Compassion / Từ Bi", "type": "SINGLE_BOOK", "author": "Hồ Thị Việt Hà (dịch)", "image": "3893723154274522.jpg", "evidence": "14,99€ visible"},
    {"title": "Những Quy Luật Tự Nhiên Của Trẻ", "type": "SINGLE_BOOK", "author": "Céline Alvarez; Nguyễn Thúy Hường, Trần Thị Khánh Vân (dịch)", "image": "3893723234274514.jpg", "evidence": "21,99€ visible"},
    {"title": "Thầy Cô Giáo Hạnh Phúc Sẽ Thay Đổi Thế Giới", "type": "BOOK_COMBO", "author": "Thích Nhất Hạnh, Katherine Weare", "image": "3893723317607839.jpg", "evidence": "2-volume combo, 29€/Combo visible"},
    {"title": "Làm Cha Mẹ Tỉnh Thức", "type": "SINGLE_BOOK", "author": "Shefali Tsabary; Khánh Thủy (dịch)", "image": "3893723330941171.jpg", "evidence": "cover photographed"},
    {"title": "Phương Pháp Giáo Dục Montessori - Sức Thẩm Thấu Của Tâm Hồn", "type": "SINGLE_BOOK", "author": "Maria Montessori", "image": "3893723404274497.jpg", "evidence": "16,99€ visible"},
    {"title": "Đắc Nhân Tâm", "type": "SINGLE_BOOK", "author": "Dale Carnegie", "image": "3893723420941162.jpg", "evidence": "14,99€ visible"},
    {"title": "Sao Chúng Ta Lại Ngủ (Why We Sleep)", "type": "SINGLE_BOOK", "author": "Matthew Walker", "image": "3893723474274490.jpg", "evidence": "22€ visible"},
    {"title": "The Secret - Bí Mật", "type": "SINGLE_BOOK", "author": None, "image": "3893723590941145.jpg", "evidence": "25,99€ visible"},
    {"title": "Trộm Cơ Lấy May Từ Vận", "type": "SINGLE_BOOK", "author": None, "image": "3893723674274470.jpg", "evidence": "cover photographed"},
    {"title": "Chia Sẻ Từ Trái Tim", "type": "SINGLE_BOOK", "author": "Thích Pháp Hòa", "image": "3893723714274466.jpg", "evidence": "17,99€ visible"},
    {"title": "Thế Bây Giờ Mẹ Muốn Cái Gì?", "type": "SINGLE_BOOK", "author": "Dr. Gary Vũ", "image": "3893723780941126.jpg", "evidence": "16,99€ visible"},
    {"title": "Osho - Đàn Ông", "type": "SINGLE_BOOK", "author": "Osho", "image": "3893723787607792.jpg", "evidence": "cover photographed"},
    {"title": "Joy: The Happiness That Comes From Within / Hạnh Phúc Tại Tâm", "type": "SINGLE_BOOK", "author": "Lê Thị Thanh Tâm (dịch)", "image": "3893723864274451.jpg", "evidence": "cover photographed"},
    {"title": "Hiểu (The Book of Understanding)", "type": "SINGLE_BOOK", "author": None, "image": "3893723924274445.jpg", "evidence": "19,99€ visible"},
]


def _normalize_for_dedupe(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def get_or_create_batch(repository: SupabaseRepository, dry_run: bool) -> tuple[dict[str, Any], bool]:
    existing = repository.get_batch_by_code(BATCH_CODE)
    if existing is not None:
        return existing, False
    if dry_run:
        return ({"batch_id": "<would-create>", "batch_code": BATCH_CODE}, True)
    created = repository.create_batch(
        batch_code=BATCH_CODE, batch_name=BATCH_NAME, description=BATCH_DESCRIPTION
    )
    return created, True


def historical_source_url(record_id: str) -> str:
    return f"facebook-export://{DEFAULT_SOURCE_EXPORT_LABEL}#record={record_id}"


def get_or_create_source_url(
    repository: SupabaseRepository, batch_id: str, batch_exists: bool,
    record_id: str, date_text: str, dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    source_url = historical_source_url(record_id)
    if batch_exists:
        existing_response = (
            repository.client.table("source_urls")
            .select("source_url_id, batch_id, source_type, source_url, source_name, crawl_status")
            .eq("batch_id", batch_id).eq("source_type", "FACEBOOK_POST").eq("source_url", source_url)
            .limit(1).execute()
        )
        existing_rows = existing_response.data or []
        if existing_rows:
            return existing_rows[0], False
    if dry_run:
        return ({"source_url_id": "<would-create>", "source_url": source_url}, True)
    created = repository.save_source_url(
        batch_id=batch_id, source_url=source_url,
        selection_reason=f"Historical Facebook export record #{record_id} ({date_text})",
        active=True, source_type="FACEBOOK_POST",
    )
    repository.update_source_url_status(source_url_id=created["source_url_id"], crawl_status="COLLECTED")
    created["crawl_status"] = "COLLECTED"
    return created, True


def get_or_create_raw_page(
    repository: SupabaseRepository, batch_id: str, batch_exists: bool,
    source_url_row: dict[str, Any], record_id: str, cleaned_text: str,
    content_hash_full: str, dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    if batch_exists:
        existing_response = (
            repository.client.table("raw_pages")
            .select("raw_page_id, batch_id, source_url_id, page_type, page_url, content_hash, cleaning_status")
            .eq("batch_id", batch_id).eq("content_hash", content_hash_full).limit(1).execute()
        )
        existing_rows = existing_response.data or []
        if existing_rows:
            return existing_rows[0], False
    if dry_run:
        return ({"raw_page_id": "<would-create>", "content_hash": content_hash_full}, True)
    payload = {
        "batch_id": batch_id,
        "source_url_id": source_url_row["source_url_id"],
        "page_type": "FACEBOOK_POST",
        "page_url": source_url_row["source_url"],
        "raw_title": f"Historical Facebook export record #{record_id}",
        "raw_text": cleaned_text,
        "raw_html": None,
        "content_hash": content_hash_full,
        "collector_name": "image_derived_historical_facebook_export_importer",
        "collector_version": SCRIPT_VERSION,
    }
    response = repository.client.table("raw_pages").insert(payload).execute()
    records = response.data or []
    if not records:
        raise RuntimeError(f"raw_pages insert returned no data for record #{record_id}.")
    raw_page = records[0]
    update_response = (
        repository.client.table("raw_pages")
        .update({
            "cleaned_text": cleaned_text,
            "cleaning_status": "CLEANED",
            "cleaning_method": "historical_text_cleaner.clean_historical_facebook_text",
            "cleaned_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("raw_page_id", raw_page["raw_page_id"]).execute()
    )
    updated_records = update_response.data or []
    if updated_records:
        raw_page = updated_records[0]
    return raw_page, True


def find_existing_candidate(
    repository: SupabaseRepository, raw_page_id: str, title_normalized: str, candidate_type: str,
) -> dict[str, Any] | None:
    response = (
        repository.client.table("product_candidates")
        .select("candidate_id, candidate_code, raw_page_id, extracted_title, candidate_type, workflow_status, review_required, source_evidence")
        .eq("raw_page_id", raw_page_id).eq("candidate_type", candidate_type).execute()
    )
    target = _normalize_for_dedupe(title_normalized)
    for record in response.data or []:
        if _normalize_for_dedupe(record.get("extracted_title") or "") == target:
            return record
    return None


def get_next_candidate_code(repository: SupabaseRepository, batch_id: str) -> str:
    response = repository.client.table("product_candidates").select("candidate_code").eq("batch_id", batch_id).execute()
    highest = 0
    for record in response.data or []:
        match = CANDIDATE_CODE_PATTERN.match(str(record.get("candidate_code") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{BATCH_CODE}-CAN-{highest + 1:04d}"


def build_source_evidence(item: dict[str, Any], raw_page: dict[str, Any], source_url_row: dict[str, Any]) -> dict[str, Any]:
    title_normalized = _normalize_for_dedupe(item["title"])
    return {
        "import_type": IMPORT_TYPE,
        "importer_name": IMPORTER_NAME,
        "importer_version": SCRIPT_VERSION,
        "batch_code": BATCH_CODE,
        "source_type": "FACEBOOK",
        "page_type": "FACEBOOK_POST",
        "raw_page_id": raw_page.get("raw_page_id"),
        "source_url_id": source_url_row.get("source_url_id"),
        "source_url": source_url_row.get("source_url"),
        "historical_record_id": HISTORICAL_RECORD_ID,
        "historical_post_date": HISTORICAL_POST_DATE,
        "historical_export_file": DEFAULT_SOURCE_EXPORT_LABEL,
        "extraction_source": EXTRACTION_SOURCE,
        "title_raw": item["title"],
        "title_normalized": title_normalized,
        "evidence_text": item["evidence"],
        "confidence": 0.9,
        "completeness_status": "VISUALLY_CONFIRMED",
        "local_media_count": 1,
        "local_media_paths": [MEDIA_PATH_PREFIX + item["image"]],
        "historical_import_key": f"{HISTORICAL_RECORD_ID}:{title_normalized}:{item['type']}",
        "extraction_method": "AI_ASSISTED",
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def build_candidate_payload(batch: dict[str, Any], raw_page: dict[str, Any], source_url_row: dict[str, Any], item: dict[str, Any], candidate_code: str) -> dict[str, Any]:
    evidence = build_source_evidence(item, raw_page, source_url_row)
    return {
        "batch_id": batch["batch_id"],
        "candidate_code": candidate_code,
        "candidate_type": item["type"],
        "combo_group_code": None,
        "raw_page_id": raw_page["raw_page_id"],
        "source_url_id": source_url_row["source_url_id"],
        "extracted_title": item["title"],
        "extracted_author": item["author"],
        "possible_isbn": None,
        "workflow_status": "EXTRACTED",
        "extraction_confidence": 0.9,
        "source_evidence": evidence,
        "conflict_fields": [],
        "review_required": True,
        "review_reason": REVIEW_REASON,
        "extraction_method": evidence["extraction_method"],
        "extractor_name": evidence["extractor_name"],
        "extractor_version": evidence["extractor_version"],
    }


def insert_candidate(repository: SupabaseRepository, payload: dict[str, Any]) -> dict[str, Any]:
    response = repository.client.table("product_candidates").insert(payload).execute()
    records = response.data or []
    if not records:
        raise RuntimeError("product_candidates insert returned no data.")
    return records[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Perform real Supabase writes. Without this flag, runs as a dry run.")
    args = parser.parse_args()
    dry_run = not args.execute

    print(f"TSYC image-derived historical candidate import (batch 2) -- v{SCRIPT_VERSION}")
    print(f"MODE: {'DRY RUN (no writes)' if dry_run else 'EXECUTE (real Supabase writes)'}")
    print(f"BATCH_CODE: {BATCH_CODE}")
    print(f"selected items: {len(SELECTED_ITEMS)}")
    print()

    export_records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {r.record_index: r.full_text for r in export_records}
    full_text = text_by_id.get(int(HISTORICAL_RECORD_ID), "")
    if not full_text:
        raise RuntimeError(f"Historical record #{HISTORICAL_RECORD_ID} not found in export.")
    cleaned_text = clean_historical_facebook_text(full_text)
    content_hash_full = hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest()

    repository = SupabaseRepository()

    batch, batch_created = get_or_create_batch(repository, dry_run)
    batch_exists = not (batch_created and dry_run)
    print(f"BATCH: {batch.get('batch_code', BATCH_CODE)} "
          f"({'would create' if (batch_created and dry_run) else 'created' if batch_created else 'already existed'})")

    source_url_row, _ = get_or_create_source_url(repository, batch["batch_id"], batch_exists, HISTORICAL_RECORD_ID, HISTORICAL_POST_DATE, dry_run)
    raw_page, _ = get_or_create_raw_page(repository, batch["batch_id"], batch_exists, source_url_row, HISTORICAL_RECORD_ID, cleaned_text, content_hash_full, dry_run)
    print(f"RAW_PAGE: {raw_page.get('raw_page_id')}")
    print()

    results: list[dict[str, Any]] = []

    for idx, item in enumerate(SELECTED_ITEMS, start=1):
        try:
            title_normalized = _normalize_for_dedupe(item["title"])
            if not batch_exists or raw_page.get("raw_page_id") == "<would-create>":
                existing = None
            else:
                existing = find_existing_candidate(repository, raw_page["raw_page_id"], title_normalized, item["type"])

            if existing is not None:
                status = "ALREADY_EXISTED"
                candidate_code = existing["candidate_code"]
                candidate_id = existing["candidate_id"]
            elif dry_run:
                status = "WOULD_CREATE"
                candidate_code = "<would-assign>"
                candidate_id = "<would-create>"
            else:
                candidate_code = get_next_candidate_code(repository, batch["batch_id"])
                payload = build_candidate_payload(batch, raw_page, source_url_row, item, candidate_code)
                created = insert_candidate(repository, payload)
                status = "CREATED"
                candidate_code = created["candidate_code"]
                candidate_id = created["candidate_id"]
                repository.write_process_log(
                    message=f"Image-derived historical import: candidate {candidate_code} created from record #{HISTORICAL_RECORD_ID}, image {item['image']}.",
                    process_name="image_derived_historical_candidate_import",
                    batch_id=batch["batch_id"], candidate_id=candidate_id,
                    process_step="IMPORT_CANDIDATE", log_level="INFO", status="SUCCESS",
                )

            results.append({"index": idx, "title": item["title"], "type": item["type"], "status": status, "candidate_code": candidate_code, "candidate_id": candidate_id})
            print(f"[{idx}/{len(SELECTED_ITEMS)}] '{item['title']}' -> {status} ({candidate_code})")

        except Exception as exc:  # noqa: BLE001
            print()
            print(f"STOP: unexpected failure on item {idx} ('{item['title']}'): {exc}")
            if not dry_run:
                repository.write_process_log(
                    message=f"Image-derived historical import STOPPED on item {idx}: {exc}",
                    process_name="image_derived_historical_candidate_import",
                    batch_id=batch["batch_id"], process_step="IMPORT_CANDIDATE",
                    log_level="CRITICAL", status="FAILED",
                    error_details={"index": idx, "title": item["title"], "error": str(exc)},
                )
            return 1

    print()
    print("=== SUMMARY ===")
    created_count = sum(1 for r in results if r["status"] in {"CREATED", "WOULD_CREATE"})
    existed_count = sum(1 for r in results if r["status"] == "ALREADY_EXISTED")
    print(f"selected: {len(results)}")
    print(f"created_or_would_create: {created_count}")
    print(f"already_existed: {existed_count}")
    print()
    print(f"MODE: {'DRY_RUN' if dry_run else 'EXECUTE'}")
    print("IMAGE_DERIVED_HISTORICAL_IMPORT_RUN_COMPLETE: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
