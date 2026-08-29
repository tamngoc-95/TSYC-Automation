"""Offline tests for src/services/facebook_history_parser.py.

Fixtures below are minimal but structurally faithful excerpts of the real
Facebook data-export markup shape (verified against the actual probe
export file at data/raw/facebook_export_probe/... during development) --
not full HTML documents, since parse_facebook_history_export() only ever
looks for '<section class="_a6-g"' markers and works directly on the raw
text between them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.services.facebook_history_parser import (
    SECTION_MARKER,
    load_facebook_history_export,
    parse_facebook_history_export,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_EXPORT_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "facebook_export_probe"
    / "your_facebook_activity"
    / "posts"
    / "your_posts__check_ins__photos_and_videos_1.html"
)


def _wrap(*sections: str) -> str:
    """Join raw <section> fragments the way they appear back-to-back in
    the real export (no wrapping <html>/<body> needed -- the parser only
    splits on SECTION_MARKER)."""
    return "<html><body>" + "".join(sections) + "</body></html>"


STATUS_UPDATE_SECTION = (
    SECTION_MARKER
    + ' aria-labelledby="u_0_kmn_ai">'
    '<h2 class="_2ph_ _a6-h _a6-i" id="u_0_kmn_ai">Tâm Võ đã cập nhật trạng thái của cô ấy.</h2>'
    '<div class="_2ph_ _a6-p"><div><div class="_2pin">'
    "<div>Oigioioi, nhiều lúc cảm thấy một số người chẳng bằng con chó😏</div>"
    "<div></div></div></div></div>"
    '<footer class="_3-94 _a6-o"><a target="_blank" href="https://www.facebook.com/dyi/l/?l=XYZ">'
    '<div class="_a72d">Tháng 10 01, 2025 10:21:24 ch</div></a></footer>'
    "</section>"
)

REPEATED_ALBUM_CAPTION_SECTION = (
    SECTION_MARKER
    + ' aria-labelledby="u_0_kag_1">'
    '<h2 class="_2ph_ _a6-h _a6-i" id="u_0_kag_1">Tâm Võ đã thêm 3 ảnh mới.</h2>'
    '<div class="_2ph_ _a6-p"><div><div class="_2pin"><div><div><div>'
    '<div class="_a7nf">'
    '<div class="_a7ng"><div><a target="_blank" href="your_facebook_activity/posts/media/SachCoSantaiDuc_111/1.jpg">'
    '<img src="your_facebook_activity/posts/media/SachCoSantaiDuc_111/1.jpg" class="_a6_o" /></a>'
    "<div>Sách Có Sẵn tại Đức</div></div></div>"
    '<div class="_a7ng"><div><a target="_blank" href="your_facebook_activity/posts/media/SachCoSantaiDuc_111/2.jpg">'
    '<img src="your_facebook_activity/posts/media/SachCoSantaiDuc_111/2.jpg" class="_a6_o" /></a>'
    "<div>Sách Có Sẵn tại Đức</div></div></div>"
    '<div class="_a7ng"><div><a target="_blank" href="your_facebook_activity/posts/media/SachCoSantaiDuc_111/3.jpg">'
    '<img src="your_facebook_activity/posts/media/SachCoSantaiDuc_111/3.jpg" class="_a6_o" /></a>'
    "<div>Sách Có Sẵn tại Đức</div></div></div>"
    "</div></div></div></div></div>"
    '<div class="_2pin"><div>Cập nhật Tháng 5 03, 2025 10:16:09 ch</div><div></div><div></div></div>'
    "</div></div>"
    '<footer class="_3-94 _a6-o"><a target="_blank" href="https://www.facebook.com/dyi/l/?l=XYZ">'
    '<div class="_a72d">Tháng 5 03, 2025 10:16:09 ch</div></a></footer>'
    "</section>"
)

FEEDBACK_MULTI_CAPTION_SECTION = (
    SECTION_MARKER
    + ' aria-labelledby="u_0_kag_2">'
    '<h2 class="_2ph_ _a6-h _a6-i" id="u_0_kag_2">Tâm Võ đã thêm 4 ảnh mới.</h2>'
    '<div class="_2ph_ _a6-p"><div><div class="_2pin"><div><div><div>'
    '<div class="_a7nf">'
    '<div class="_a7ng"><div><a target="_blank" href="your_facebook_activity/posts/media/FeedbackstuYeuConPageTiemsachvaThuviencongdong_222/1.jpg">'
    '<img src="your_facebook_activity/posts/media/FeedbackstuYeuConPageTiemsachvaThuviencongdong_222/1.jpg" class="_a6_o" /></a>'
    "<div>Feedbacks từ Yêu Con (Page/ Tiệm sách và Thư viện cộng đồng)</div></div></div>"
    '<div class="_a7ng"><div><a target="_blank" href="your_facebook_activity/posts/media/FeedbackstuYeuConPageTiemsachvaThuviencongdong_222/2.jpg">'
    '<img src="your_facebook_activity/posts/media/FeedbackstuYeuConPageTiemsachvaThuviencongdong_222/2.jpg" class="_a6_o" /></a>'
    "<div>Feedbacks từ Yêu Con (Page/ Tiệm sách và Thư viện cộng đồng)</div></div></div>"
    "</div></div></div></div></div>"
    '<div class="_2pin"><div>Đây là bộ sách hay, giàu xúc cảm hiếm có.<br /> <br /> Không chỉ các bé đâu.</div>'
    "<div></div></div>"
    "</div></div>"
    '<footer class="_3-94 _a6-o"><a target="_blank" href="https://www.facebook.com/dyi/l/?l=XYZ">'
    '<div class="_a72d">Tháng 12 02, 2024 10:15:35 sáng</div></a></footer>'
    "</section>"
)

SHARED_LINK_SECTION = (
    SECTION_MARKER
    + ' aria-labelledby="u_0_k22_4O">'
    '<h2 class="_2ph_ _a6-h _a6-i" id="u_0_k22_4O">Tâm Võ đã chia sẻ một liên kết.</h2>'
    '<div class="_2ph_ _a6-p"><div><div class="_2pin"><div><div><div><div><div><div>'
    '<a target="_blank" href="https://zalo.me/g/wztbnn909">https://zalo.me/g/wztbnn909</a>'
    "</div></div></div></div></div></div></div>"
    '<div class="_2pin"><div>Cập nhật Tháng 5 06, 2024 2:28:42 ch</div><div></div><div></div></div>'
    "</div></div>"
    '<footer class="_3-94 _a6-o"><a target="_blank" href="https://www.facebook.com/dyi/l/?l=XYZ">'
    '<div class="_a72d">Tháng 5 06, 2024 2:28:42 ch</div></a></footer>'
    "</section>"
)

VIDEO_SECTION = (
    SECTION_MARKER
    + ' aria-labelledby="u_0_kmw_Mr">'
    '<h2 class="_2ph_ _a6-h _a6-i" id="u_0_kmw_Mr">Tâm Võ đã thêm một video mới.</h2>'
    '<div class="_2ph_ _a6-p"><div><div class="_2pin"><div><div><div>'
    '<div class="_a7nf"><div class="_a7ng"><div>'
    '<video src="your_facebook_activity/posts/media/Putalamviecnha_444/1.mp4" controls="1" class="_a6_o">'
    '<a target="_blank" href="your_facebook_activity/posts/media/Putalamviecnha_444/1.mp4">'
    "<div>Nhấp để xem video:</div></a></video>"
    '<div></div><div class="_3-95">Bố k trải giường, mẹ không trải giường.</div>'
    "</div></div></div></div></div>"
    '<div class="_2pin"><div>Bố k trải giường, mẹ không trải giường.</div><div></div></div>'
    "</div></div>"
    '<footer class="_3-94 _a6-o"><a target="_blank" href="https://www.facebook.com/dyi/l/?l=XYZ">'
    '<div class="_a72d">Tháng 7 16, 2026 6:54:00 ch</div></a></footer>'
    "</section>"
)

MENTION_SECTION = (
    SECTION_MARKER
    + ' aria-labelledby="u_0_k9">'
    '<h2 class="_2ph_ _a6-h _a6-i" id="u_0_k9">Tâm Võ đã thêm một ảnh mới.</h2>'
    '<div class="_2ph_ _a6-p"><div><div class="_2pin">'
    "<div>Em vẫn bán và preorder sách mới tại &#064;[2415122391976246:69:Tiệm sách Yêu Con ở Đức], cho thuê nữa nhé</div>"
    "<div></div></div></div></div>"
    '<footer class="_3-94 _a6-o"><a target="_blank" href="https://www.facebook.com/dyi/l/?l=XYZ">'
    '<div class="_a72d">Tháng 11 19, 2025 8:26:07 ch</div></a></footer>'
    "</section>"
)

EMPTY_STICKER_SECTION = (
    SECTION_MARKER
    + ' aria-labelledby="u_0_k1x">'
    '<h2 class="_2ph_ _a6-h _a6-i" id="u_0_k1x">Tâm Võ đã chia sẻ một bài viết.</h2>'
    '<div class="_2ph_ _a6-p"><div><div class="_2pin"><div><div><div><div class="_a7nf">'
    '<div class="_a7ng"><div>'
    '<a target="_blank" href="your_facebook_activity/posts/media/stickers_used/1.webp">'
    '<img src="your_facebook_activity/posts/media/stickers_used/1.webp" class="_a6_o" /></a>'
    "<div></div></div></div></div></div></div>"
    '<div class="_2pin"><div>Cập nhật Tháng 11 06, 2023 2:41:17 ch</div><div></div><div></div></div>'
    "</div></div>"
    '<footer class="_3-94 _a6-o"><a target="_blank" href="https://www.facebook.com/dyi/l/?l=XYZ">'
    '<div class="_a72d">Tháng 11 06, 2023 2:41:17 ch</div></a></footer>'
    "</section>"
)


# --- basic status update --------------------------------------------------


def test_parses_basic_status_update_record():
    records = parse_facebook_history_export(_wrap(STATUS_UPDATE_SECTION))

    assert len(records) == 1
    record = records[0]
    assert record.record_index == 1
    assert record.heading == "Tâm Võ đã cập nhật trạng thái của cô ấy."
    assert record.full_text == "Oigioioi, nhiều lúc cảm thấy một số người chẳng bằng con chó😏"
    assert record.date_text == "Tháng 10 01, 2025 10:21:24 ch"
    assert record.media_count == 0


# --- repeated per-photo album caption dedupes to one ----------------------


def test_repeated_album_caption_collapses_to_a_single_copy():
    records = parse_facebook_history_export(_wrap(REPEATED_ALBUM_CAPTION_SECTION))

    record = records[0]
    assert record.full_text == "Sách Có Sẵn tại Đức"
    assert record.full_text.count("Sách Có Sẵn tại Đức") == 1
    assert record.media_count == 3
    assert record.folder_slugs == ("SachCoSantaiDuc",)


# --- multi-photo feedback post keeps both album caption and real text ----


def test_feedback_post_keeps_album_caption_and_distinct_real_caption():
    records = parse_facebook_history_export(_wrap(FEEDBACK_MULTI_CAPTION_SECTION))

    record = records[0]
    assert "Feedbacks từ Yêu Con (Page/ Tiệm sách và Thư viện cộng đồng)" in record.full_text
    assert "Đây là bộ sách hay, giàu xúc cảm hiếm có." in record.full_text
    # The per-photo caption appeared twice in the source markup (once per
    # photo) and must still collapse to exactly one copy.
    assert record.full_text.count("Feedbacks từ Yêu Con") == 1
    assert record.folder_slugs == ("FeedbackstuYeuConPageTiemsachvaThuviencongdong",)
    assert record.media_count == 2


# --- shared link -----------------------------------------------------------


def test_parses_shared_link_extracts_external_link():
    records = parse_facebook_history_export(_wrap(SHARED_LINK_SECTION))

    record = records[0]
    assert record.external_links == ("https://zalo.me/g/wztbnn909",)
    assert record.local_image_paths == ()
    assert record.media_count == 0
    # The "Cập nhật <date>" boilerplate line must not leak into full_text.
    assert "Cập nhật" not in record.full_text


# --- video -----------------------------------------------------------------


def test_parses_video_post_extracts_local_video_path_and_dedupes_caption():
    records = parse_facebook_history_export(_wrap(VIDEO_SECTION))

    record = records[0]
    assert record.local_video_paths == (
        "your_facebook_activity/posts/media/Putalamviecnha_444/1.mp4",
    )
    assert record.media_count == 1
    assert record.full_text == "Bố k trải giường, mẹ không trải giường."
    assert record.folder_slugs == ("Putalamviecnha",)


# --- structural @mention ----------------------------------------------------


def test_parses_structural_mention_id_and_name():
    records = parse_facebook_history_export(_wrap(MENTION_SECTION))

    record = records[0]
    assert record.mention_ids == ("2415122391976246",)
    assert record.mention_names == ("Tiệm sách Yêu Con ở Đức",)
    # The literal "&#064;[...]" markup must not leak into full_text as-is.
    assert "&#064;" not in record.full_text
    assert "2415122391976246" not in record.full_text


# --- empty sticker share -----------------------------------------------------


def test_empty_sticker_share_has_empty_full_text():
    records = parse_facebook_history_export(_wrap(EMPTY_STICKER_SECTION))

    record = records[0]
    assert record.full_text == ""
    assert record.media_count == 1
    assert record.text_preview == "Tâm Võ đã chia sẻ một bài viết."


# --- heading never leaks into full_text ------------------------------------


def test_heading_text_is_not_included_in_full_text():
    records = parse_facebook_history_export(_wrap(STATUS_UPDATE_SECTION))

    record = records[0]
    assert record.heading not in record.full_text


# --- record_index is stable, 1-based, document order -----------------------


def test_record_index_is_stable_1_based_document_order():
    combined = _wrap(STATUS_UPDATE_SECTION, SHARED_LINK_SECTION, VIDEO_SECTION)
    records = parse_facebook_history_export(combined)

    assert [record.record_index for record in records] == [1, 2, 3]
    assert records[0].heading.endswith("trạng thái của cô ấy.")
    assert records[1].heading.endswith("một liên kết.")
    assert records[2].heading.endswith("một video mới.")


# --- idempotency -------------------------------------------------------------


def test_parse_is_idempotent_across_repeated_calls():
    combined = _wrap(
        STATUS_UPDATE_SECTION,
        REPEATED_ALBUM_CAPTION_SECTION,
        FEEDBACK_MULTI_CAPTION_SECTION,
        MENTION_SECTION,
    )

    first = parse_facebook_history_export(combined)
    second = parse_facebook_history_export(combined)

    assert first == second


# --- real export smoke test (skipped if the local probe file is absent) ----


@pytest.mark.skipif(
    not REAL_EXPORT_PATH.is_file(),
    reason="Local Facebook export probe file is gitignored and not present in this checkout.",
)
def test_load_real_export_parses_without_error_and_finds_known_records():
    records = load_facebook_history_export(REAL_EXPORT_PATH)

    assert len(records) > 2000
    assert all(record.record_index == index for index, record in enumerate(records, start=1))

    # At least one record must resolve the manually verified strong
    # folder-slug/structural-mention evidence this classification layer
    # depends on.
    all_folder_slugs = {slug for record in records for slug in record.folder_slugs}
    assert "SachCoSantaiDuc" in all_folder_slugs
    assert "FeedbackstuYeuConPageTiemsachvaThuviencongdong" in all_folder_slugs

    all_mention_ids = {mention_id for record in records for mention_id in record.mention_ids}
    assert "2415122391976246" in all_mention_ids
