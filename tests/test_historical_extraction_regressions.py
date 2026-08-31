"""Phase 5 targeted regression tests -- one per historical record whose
AUTO_PASS output was found garbled during the initial candidate-
extraction-preview run, before the historical_text_cleaner.py /
historical_title_quality_guard.py fix.

Every fixture's full_text is copied verbatim (line-for-line, including
the export's own stray-space and whole-block-duplication artifacts)
from HistoryRecord.full_text for that real record id, captured via
direct inspection of the historical export during this fix. These are
not synthetic examples -- they are the actual failing inputs.
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.rules.historical_candidate_extraction import (
    HistoricalExtractionInput,
    extract_historical_candidates,
)


def _make_input(record_id: int, full_text: str, **kwargs) -> HistoricalExtractionInput:
    defaults = dict(
        date_text="Tháng 1 01, 2025 12:00:00 ch",
        deterministic_post_type="PROMOTION",
        deterministic_candidate_eligible=True,
        semantic_post_type="PRODUCT_POST",
        decision_source="SEMANTIC",
        semantic_extracted_product_hints=(),
        local_image_paths=(),
        local_video_paths=(),
    )
    defaults.update(kwargs)
    return HistoricalExtractionInput(record_id=record_id, full_text=full_text, **defaults)


# --- #1038: mobile-upload chrome must not lead the title -------------------


def test_1038_title_never_starts_with_mobile_upload_chrome():
    text = (
        "Tải lên từ di động\n"
        "#Preorder tuyển tập truyện trinh thám 5🌟 của Agatha Christie\n"
        " \n"
        " Tui rất sợ mấy truyện ntn, nhưng ma cũng rất mê😱😆😅"
    )

    result = extract_historical_candidates(_make_input(1038, text))

    for candidate in result.candidates:
        assert not candidate.title_raw.startswith("Tải lên từ di động")
        assert "Tải lên từ di động" not in candidate.title_raw


# --- #1104: "Tải lên từ di động Đây" must never become a candidate --------


def test_1104_bare_pronoun_capture_never_becomes_a_candidate():
    text = (
        "Tải lên từ di động\n"
        "Đây là cuốn sách giúp mình vượt ra khỏi mọi nỗi đau kể cả về thể xác🙏 "
        "Biết ơn thiền sư Thích Nhất Hạnh đã viết nên cuốn sách tuyệt vời và giá trị này cho chúng ta♥️\n"
        "Mình có cả sách giấy cho ai yêu thích, giá chỉ 13,99€ và bản PDF miễn phí để dưới phần bình luận❤️\n"
        "Đây là cuốn sách giúp mình vượt ra khỏi mọi nỗi đau kể cả về thể xác🙏 "
        "Biết ơn thiền sư Thích Nhất Hạnh đã viết nên cuốn sách tuyệt vời và giá trị này cho chúng ta♥️\n"
        " \n"
        " Mình có cả sách giấy cho ai yêu thích, giá chỉ 13,99€ và bản PDF miễn phí để dưới phần bình luận❤️"
    )

    result = extract_historical_candidates(_make_input(1104, text))

    for candidate in result.candidates:
        assert candidate.title_raw.strip().casefold() != "đây"
    assert not any("tải lên từ di động" in c.title_raw.casefold() for c in result.candidates)


# --- #1155/#1156: long prose before "của" must not become a title ---------


def test_1156_long_prose_fragment_before_cua_is_not_a_candidate():
    text = (
        "Review sách hay🥰\n"
        "Mua mới 36€ mà tui đọc rồi thanh lý có 20€ thoi, ai hốt lẹ có 1 cuốn thui😁\n"
        "Review sách: Hoả Ngục – Dan Brown\n"
        "Đọc Hoả Ngục giống như bước vào một mê cung nơi nghệ thuật, lịch sử, tôn giáo và "
        "khoa học đan xen, vừa huyền bí, cuốn hút vừa khiến mình phải suy ngẫm. Đây là một "
        "trong những cuốn tiểu thuyết mình thấy rất hấp dẫn của Dan Brown, bởi không chỉ là "
        "một cuộc rượt đuổi nghẹt thở, mà còn đặt ra những câu hỏi gai góc về nhân loại và "
        "tương lai của loài người.\n"
        "Cuốn này 4,5/5 🌟 nha\n"
        "Mua mới 36€ mà tui đọc rồi thanh lý có 20€ thoi, ai hốt lẹ có 1 cuốn thui😁\n"
        " \n"
        " Review sách: Hoả Ngục – Dan Brown\n"
        " \n"
        " Đọc Hoả Ngục giống như bước vào một mê cung nơi nghệ thuật, lịch sử, tôn giáo và "
        "khoa học đan xen, vừa huyền bí, cuốn hút vừa khiến mình phải suy ngẫm. Đây là một "
        "trong những cuốn tiểu thuyết mình thấy rất hấp dẫn của Dan Brown, bởi không chỉ là "
        "một cuộc rượt đuổi nghẹt thở, mà còn đặt ra những câu hỏi gai góc về nhân loại và "
        "tương lai của loài người.\n"
        " \n"
        " Cuốn này 4,5/5 🌟 nha"
    )

    result = extract_historical_candidates(_make_input(1156, text))

    for candidate in result.candidates:
        assert "nghệ thuật, lịch sử" not in candidate.title_raw
        assert not candidate.title_raw[:1].islower()


def test_1155_prose_fragment_is_not_a_candidate():
    text = (
        "Review sách hay🥰\n"
        "Cuốn này em đọc từ lâu rồi, em viết lại review và pass sách giá rẻ thôi nha 😁\n"
        "Review sách: Thiên Thần và Ác Quỷ – Dan Brown\n"
        "Đọc Thiên Thần và Ác Quỷ giống như lao vào một cơn bão của bí mật, lịch sử và niềm "
        "tin. Đây là cuộc phiêu lưu đầu tiên của giáo sư Robert Langdon, mở màn cho loạt "
        "truyện nổi tiếng về biểu tượng học."
    )

    result = extract_historical_candidates(_make_input(1155, text))

    for candidate in result.candidates:
        assert "lao vào một cơn bão" not in candidate.title_raw


# --- #1180: real title must survive without the UI-chrome prefix ----------


def test_1180_preserves_real_title_without_chrome_prefix():
    text = (
        "Tải lên từ di động\n"
        "Thức tỉnh mục đích sống (A New Earth) của Eckhart Tolle là một trong những cuốn "
        "sách truyền cảm hứng mạnh mẽ nhất về sự tỉnh thức, bản ngã và hành trình tìm lại "
        "ý nghĩa cuộc sống đích thực."
    )

    result = extract_historical_candidates(_make_input(1180, text))

    titles = [c.title_raw for c in result.candidates]
    assert any("Thức tỉnh mục đích sống (A New Earth)" in title for title in titles)
    assert not any(title.startswith("Tải lên từ di động") for title in titles)


# --- #1189: prose kept as candidate while the real combo title is discarded --


def test_1189_prose_is_never_kept_as_the_candidate():
    text = (
        "Tải lên từ di động\n"
        "Ới hôm trước chị nào nhắm mua lẻ mà em bảo em nhập đủ bộ về r cho chị tha hồ chọn, "
        "mà giờ em nhớ mãi k ra là ai. Sorry chị nhiều, chị nhắn lại giúp em nhá🙏🙏🙏 Em cảm ơn chị❤️\n"
        "#Có Sẵn – Combo “Diary of a Wimpy Kid – Nhật Ký Chú Bé Nhút Nhát” (Song ngữ Việt – Anh, 18 tập)\n"
        "Nếu bạn từng là một đứa trẻ vụng về, hay mơ mộng và thấy mình hơi “lạc loài” ở trường "
        "học, bạn sẽ thấy Greg Heffley thật quen. Bộ sách song ngữ 18 tập này là cuốn nhật ký "
        "hài hước, tinh nghịch nhưng cũng đầy cảm xúc của một cậu bé đang lớn – với đủ trò dở "
        "khóc dở cười, những bài học nhỏ nhưng sâu sắc về tình bạn, gia đình và chính bản thân.\n"
        "Trẻ em sẽ mê mẩn nét vẽ vui nhộn, giọng văn gần gũi, còn bố mẹ thì yên tâm vì mỗi "
        "trang sách là một cơ hội giúp con học tiếng Anh thật tự nhiên.\n"
        "Một combo lý tưởng để cả nhà cùng đọc, cùng cười, cùng trưởng thành.\n"
        "Đặt trước ngay để không bỏ lỡ hành trình “nhút nhát mà vui nhộn” này!\n"
        "Ới hôm trước chị nào nhắm mua lẻ mà em bảo em nhập đủ bộ về r cho chị tha hồ chọn, "
        "mà giờ em nhớ mãi k ra là ai. Sorry chị nhiều, chị nhắn lại giúp em nhá🙏🙏🙏 Em cảm ơn chị❤️\n"
        " \n"
        " #Có Sẵn – Combo “Diary of a Wimpy Kid – Nhật Ký Chú Bé Nhút Nhát” (Song ngữ Việt – Anh, 18 tập)\n"
        " \n"
        " Nếu bạn từng là một đứa trẻ vụng về, hay mơ mộng và thấy mình hơi “lạc loài” ở trường "
        "học, bạn sẽ thấy Greg Heffley thật quen. Bộ sách song ngữ 18 tập này là cuốn nhật ký "
        "hài hước, tinh nghịch nhưng cũng đầy cảm xúc của một cậu bé đang lớn – với đủ trò dở "
        "khóc dở cười, những bài học nhỏ nhưng sâu sắc về tình bạn, gia đình và chính bản thân.\n"
        " \n"
        " Trẻ em sẽ mê mẩn nét vẽ vui nhộn, giọng văn gần gũi, còn bố mẹ thì yên tâm vì mỗi "
        "trang sách là một cơ hội giúp con học tiếng Anh thật tự nhiên.\n"
        " \n"
        " Một combo lý tưởng để cả nhà cùng đọc, cùng cười, cùng trưởng thành.\n"
        " \n"
        " Đặt trước ngay để không bỏ lỡ hành trình “nhút nhát mà vui nhộn” này!"
    )

    result = extract_historical_candidates(_make_input(1189, text))

    for candidate in result.candidates:
        assert not candidate.title_raw.startswith("y mình hơi")
        assert not candidate.title_raw[:1].islower()


# --- #1268: mobile-upload/preorder boilerplate must not become a combo title --


def test_1268_boilerplate_never_becomes_a_combo_title():
    text = (
        "Tải lên từ di động\n"
        "Em nhận preorder sách mới 70€/ Combo 4 cuốn truyện của Thomas Harris\n"
        " \n"
        " Thanh lý truyện cũ 40€/ combo 4 cuốn (Có sẵn)\n"
        " \n"
        " Có bán lẻ tập ạ👍🏻"
    )

    result = extract_historical_candidates(_make_input(1268, text))

    for candidate in result.candidates:
        assert not candidate.title_raw.startswith("Tải lên từ di động")


# --- #1482: truncated fragments must not be accepted -----------------------


def test_1482_truncated_fragments_are_not_accepted():
    text = (
        "Ảnh\n"
        "1. Nghệ Thuật Bán Hàng Cho Người Giàu 8€\n"
        " 2. Phương Pháp Đầu Tư Warren Buffett 10€\n"
        " 3. Người Nam Châm 6€\n"
        " 4. Tư Duy Phản Biện 6€\n"
        " 5. Nghệ Thuật Đầu Tư Dhandho 10€\n"
        " 6. Thuật Xử Thế Của Người Xưa 4€\n"
        "Ảnh\n"
        "1. Nỗi Buồn Chiến Tranh - Bảo Ninh\n"
        " 2. Xứ Cát - Frank Herbert\n"
        " 3. Phía Sau Nghi Can X - Higashino Keigo\n"
        " 4. Thương Nhớ Mười Hai - Vũ Bằng\n"
        " 5. Bí Mật Của Naoko - Keigo Higashino\n"
        " 6. Lặng Yên Dưới Vực Sâu - Đỗ Bích Thúy"
    )

    result = extract_historical_candidates(_make_input(1482, text))

    titles = [c.title_raw for c in result.candidates]
    assert "Thuật Xử Thế" not in titles
    assert "Bí Mật" not in titles


# --- #1560: prose must not become a combo title -----------------------------


def test_1560_prose_never_becomes_the_combo_title():
    text = (
        "Tải lên từ di động\n"
        "Sale sập sàn nhân dịp Giáng Sinh và Năm mới 2026 ♥️🥰🎅🏻🤩♥️\n"
        " \n"
        " Combo 14 cuốn Gieo hạt cùng vĩ nhân dành cho bé trên 6 tuổi, giá 42€, giảm 50% "
        "chỉ còn 21€ freeship 😱😱😱\n"
        " \n"
        " Giới thiệu bộ sách “Gieo Hạt Cùng Vĩ Nhân”\n"
        " \n"
        " Bộ sách “Gieo Hạt Cùng Vĩ Nhân” gồm 14 cuốn là một tuyển tập truyện tranh ý nghĩa "
        "và đầy giá trị, được biên soạn nhằm nuôi dưỡng đạo đức, trau dồi trí tuệ và rèn "
        "luyện nghị lực cho trẻ em. Bộ sách mang đến các câu chuyện sâu sắc, giúp các em học "
        "hỏi và phát triển nhân cách thông qua những chủ đề gần gũi, bổ ích như gia đình, "
        "sức khỏe, môi trường, và các đức tính cao quý được truyền cảm hứng từ cuộc đời các "
        "danh nhân thế giới.\n"
        " \n"
        " Bộ sách được thiết kế sinh động với hình minh họa hấp dẫn, nội dung dễ hiểu, phù "
        "hợp với nhiều lứa tuổi. Mỗi cuốn sách là một bài học giá trị, giúp trẻ nhận thức rõ "
        "hơn về tầm quan trọng của việc sống đạo đức, yêu thương gia đình, bảo vệ môi "
        "trường, chăm sóc sức khỏe và phát triển tư duy tích cực."
    )

    result = extract_historical_candidates(_make_input(1560, text))

    for candidate in result.candidates:
        assert not candidate.title_raw.startswith("động với hình minh họa")
        assert not candidate.title_raw[:1].islower()


# --- #1568: real bundle title recoverable without prose garbage ------------


def test_1568_bundle_title_is_not_prose_garbage():
    text = (
        "Sách cho bé 4 tuổi\n"
        "#preorder\n"
        "Lời Giới Thiệu Bộ Sách “Phẩm Chất Nhà Lãnh Đạo Nhí” dành cho trẻ 3-8 tuổi\n"
        "Bộ sách “Phẩm Chất Nhà Lãnh Đạo Nhí” gồm 16 cuốn là một tuyển tập ý nghĩa, giúp "
        "trẻ em hình thành những kỹ năng và phẩm chất cần thiết của một nhà lãnh đạo tương "
        "lai. Thông qua các câu chuyện gần gũi và minh họa sinh động, bộ sách khuyến khích "
        "trẻ phát triển các đức tính như tự tin, sáng tạo, tinh thần hợp tác, tự chủ, lạc "
        "quan và khiêm tốn.\n"
        "#preorder\n"
        " \n"
        " Lời Giới Thiệu Bộ Sách “Phẩm Chất Nhà Lãnh Đạo Nhí” dành cho trẻ 3-8 tuổi\n"
        " \n"
        " Bộ sách “Phẩm Chất Nhà Lãnh Đạo Nhí” gồm 16 cuốn là một tuyển tập ý nghĩa, giúp "
        "trẻ em hình thành những kỹ năng và phẩm chất cần thiết của một nhà lãnh đạo tương "
        "lai. Thông qua các câu chuyện gần gũi và minh họa sinh động, bộ sách khuyến khích "
        "trẻ phát triển các đức tính như tự tin, sáng tạo, tinh thần hợp tác, tự chủ, lạc "
        "quan và khiêm tốn."
    )

    result = extract_historical_candidates(_make_input(1568, text))

    for candidate in result.candidates:
        assert not candidate.title_raw.startswith('hí"')
        assert not candidate.title_raw[:1].islower()


# --- #1603: preserve real title without junk prefix / self-duplication -----


def test_1603_no_garbage_self_duplicated_title():
    text = (
        "Sách Có Sẵn tại Đức\n"
        "#Có Sẵn\n"
        "“Chữa lành đứa trẻ trong bạn” của Charles Whitfield\n"
        "“Chữa lành đứa trẻ trong bạn” của Charles Whitfield là một cuốn sách đầy ý nghĩa "
        "và sâu sắc dành cho những ai đang tìm kiếm con đường chữa lành nội tâm.\n"
        "Ngôn ngữ của cuốn sách gần gũi nhưng không kém phần sâu sắc, kết hợp giữa tâm lý "
        "học và trải nghiệm thực tế.\n"
        "#Có Sẵn\n"
        " \n"
        " “Chữa lành đứa trẻ trong bạn” của Charles Whitfield\n"
        " \n"
        " “Chữa lành đứa trẻ trong bạn” của Charles Whitfield là một cuốn sách đầy ý nghĩa "
        "và sâu sắc dành cho những ai đang tìm kiếm con đường chữa lành nội tâm.\n"
        " \n"
        " Ngôn ngữ của cuốn sách gần gũi nhưng không kém phần sâu sắc, kết hợp giữa tâm lý "
        "học và trải nghiệm thực tế."
    )

    result = extract_historical_candidates(_make_input(1603, text))

    for candidate in result.candidates:
        assert "#Có Sẵn" not in candidate.title_raw
        assert candidate.title_raw.count("Chữa lành đứa trẻ trong bạn") <= 1


# --- #1863: vacation notice must not become a title; real hint kept ------


def test_1863_vacation_notice_is_not_a_candidate():
    text = (
        "Tải lên từ di động\n"
        "Tuần sau tôi về, tôi sẽ ship sách chăm chỉ trở lại. Biết ơn sâu sắc sự chờ đợi của "
        "anh em 🥰🥰🥰\n"
        "Còn 1 bản Muôn kiếp nhân sinh phần 1, ai cần nhắn em nhé."
    )

    result = extract_historical_candidates(
        _make_input(
            1863,
            text,
            semantic_extracted_product_hints=("Muôn kiếp nhân sinh phần 1", "Còn 1 bản"),
        )
    )

    for candidate in result.candidates:
        assert "ship sách chăm chỉ" not in candidate.title_raw
    # The real hint remains available (as a non-book hint if the
    # deterministic engine itself did not independently confirm it) --
    # never silently discarded.
    assert (
        "Muôn kiếp nhân sinh phần 1" in result.non_book_hints
        or any("Muôn kiếp nhân sinh phần 1" in c.title_raw for c in result.candidates)
    )


# --- #1893: emoji/promotion boilerplate must not become a title -----------


def test_1893_emoji_heavy_fragment_is_not_a_candidate():
    text = (
        "Sách Có Sẵn tại Đức\n"
        "🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩🤩\n"
        " Nhân dịp Ngày của cha, tặng ngay 20% giá trị đơn hàng/ preorder cho tất cả các "
        "ông bố 🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵🩵\n"
        " \n"
        " Chỉ có duy nhất 1 lần 🤩🤩🤩 Vì chắc gì năm sau em còn bán sách 😁😁😁😁😁"
    )

    result = extract_historical_candidates(_make_input(1893, text))

    for candidate in result.candidates:
        assert "🤩" not in candidate.title_raw


# --- confirmed-correct cases must still work -------------------------------


def test_1231_suoi_nguon_still_correct():
    text = (
        "Tải lên từ di động\n"
        "#GiảmGiáĐặcBiệt\n"
        " \n"
        " Cơ hội duy nhất cho ai mê truyện này😍😍😍 Giá giảm chỉ còn 28€ thay vì 50€ (Sách "
        "mới còn nguyên Seal)\n"
        " \n"
        " “Suối nguồn” của Ayn Rand\n"
        " \n"
        " Suối nguồn (The Fountainhead) là tiểu thuyết nổi tiếng của Ayn Rand, kể về kiến "
        "trúc sư trẻ Howard Roark."
    )

    result = extract_historical_candidates(_make_input(1231, text))

    titles = [c.title_raw for c in result.candidates]
    assert "Suối nguồn" in titles


def test_1343_power_vs_force_still_correct():
    text = (
        "Sách Có Sẵn tại Đức\n"
        "Giới thiệu ngắn: “Power vs. Force” của David R. Hawkins là một tác phẩm kinh "
        "điển trong lĩnh vực tâm linh và khoa học nhận thức."
    )

    result = extract_historical_candidates(_make_input(1343, text))

    titles = [c.title_raw for c in result.candidates]
    assert "Power vs. Force" in titles


# --- safety behavior must still hold ---------------------------------------


def test_774_still_review_required_zero_candidates():
    text = (
        "Tải lên từ di động\n"
        "📚 Tiệm Sách Yêu Con đã có website rồi cả nhà ơi!\n"
        "Sau nhiều năm bán sách, giới thiệu sách và vận hành Thư Viện Sách Cộng Đồng tại "
        "Đức, cuối cùng mình cũng hoàn thành một điều đã ấp ủ từ rất lâu: xây dựng website "
        "riêng cho Tiệm sách Yêu Con ở Đức.\n"
        "Mục đích rất đơn giản thôi: để mọi người tìm sách, đặt sách, mượn sách hoặc thuê "
        "sách thuận tiện hơn, đỡ phải nhắn tin qua lại nhiều lần để hỏi từng đầu sách."
    )

    result = extract_historical_candidates(
        _make_input(
            774,
            text,
            deterministic_post_type="PROMOTION",
            decision_source="DETERMINISTIC_STRONG",
            semantic_post_type=None,
            local_image_paths=("images/1.jpg",),
        )
    )

    assert result.extraction_outcome == Outcome.REVIEW_REQUIRED
    assert result.candidates == ()


def test_1287_still_review_required_zero_candidates():
    text = "Sách Có Sẵn tại Đức"

    result = extract_historical_candidates(
        _make_input(
            1287,
            text,
            decision_source="DETERMINISTIC_STRONG",
            semantic_post_type=None,
            local_image_paths=tuple(f"images/{i}.jpg" for i in range(20)),
        )
    )

    assert result.extraction_outcome == Outcome.REVIEW_REQUIRED
    assert result.candidates == ()


def test_1455_numerology_bonus_never_becomes_a_candidate():
    text = (
        "Tải lên từ di động\n"
        "Giải cứu sách phần 2, giảm tất cả 15%, freeship cho đơn từ 50€\n"
        " \n"
        " Tặng kèm 30 phút luận giải bản đồ tính cách trẻ em, giúp cha mẹ tháo gỡ khó khăn "
        "hiện tại trong nuôi dạy trẻ hoặc giúp trẻ phát huy tối đa tiềm năng sẵn có ❤️\n"
        " \n"
        " Tặng 40% khi mua Map Kid Talent 😍😍😍 Cơ hộ ngàn năm có 1, chỉ xuất hiện khi em "
        "sửa nhà hết tiền 😁😁😁"
    )

    result = extract_historical_candidates(
        _make_input(
            1455,
            text,
            semantic_extracted_product_hints=(
                "Giải cứu sách phần 2",
                "Map Kid Talent giảm 40%",
            ),
        )
    )

    assert not any("map kid talent" in c.title_raw.casefold() for c in result.candidates)
    assert "Map Kid Talent giảm 40%" in result.non_book_hints
