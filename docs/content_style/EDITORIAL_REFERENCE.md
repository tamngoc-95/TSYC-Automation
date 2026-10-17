# TSYC Editorial Reference

Version-controlled editorial reference for VI/EN/DE storefront descriptions (2026-10-17). It complements `TSYC_CONTENT_GUIDE.md`, which remains the style source of truth.

It is distilled only from:
- the guide;
- the one owner-approved example (`examples/dung-dinh-trang-di.md`);
- the defects measured on stored descriptions.

Machine-readable form: `src/domain/rules/content_style_lexicon.py`. The scorer (`content_quality_score.py`) and the evidence checks (`src/domain/content_evidence.py`) use it.

**Not a gold standard:** generated text, including text approved by the automated gates, is never added here or to `examples/`. Only descriptions the shop owner explicitly accepts may extend the examples (`examples/README.md`). Nothing here is sent to an external model for training.

## What good looks like (approved example + guide)

**Tone**
- Warm, literary, informative, in the third person.
- No sales voice.
- The Vietnamese may use long, image-rich sentences (guide section 4).

**Opening**
- Start from a real detail of the book: its form, its world, its first event.
- Never start from metadata.
- The approved example opens with what the book is ("một khúc ca Trung thu được viết dưới dạng thơ"), not with "X là cuốn sách của tác giả Y, do NXB Z ấn hành".

**Paragraphs**
- Usually 3–4 paragraphs of about 150–600 characters each.
- A shorter text is right when the evidence is thin (the example has 3).

**Detail**
- Every concrete detail traces to evidence: the Fahasa text, a READABLE approved preview page, or the cover.
- Preview pages give the most specific material (names, chapter titles, first scenes), but only what is legible.
- Never reveal more of the plot than the sources show.

**Reader relevance**
- One natural sentence on who the book is for, or how a family reads it.
- At most one suitability phrase.
- Educational benefits only when a source states them.

**Localization**
- Vietnamese is the semantic reference.
- EN/DE are natural rewrites, not word-for-word translations.
- Keep the Vietnamese title in the product name, with a neutral descriptor such as "(Vietnamese-language children's book)" or "(Kinderbuch auf Vietnamesisch)", unless an official localized title is verified.

## Rejected patterns (measured 2026-10-17)

On the 13 FAHASA-2026-001 descriptions:

| Defect | Example | Seen in |
|---|---|---|
| METADATA_OPENING | "… là cuốn sách … của tác giả X, do Nhà xuất bản Y ấn hành" | 9 of 13 vi |
| CLOSING_CLICHE | "Đây là cuốn sách phù hợp cho …", "món quà nhỏ xinh / ý nghĩa" | 8 of 13 vi |
| CLOSE_TO_SOURCE_WORDING | 15+ consecutive words from the Fahasa blurb | 10 of 13 vi |
| TEMPLATE_REUSE | 12%+ of 5-word phrases shared with another product's description | 8 of 13 vi |
| GENERIC_FILLER | "thế giới lung linh", "điều kỳ diệu", "trên từng trang sách" | 4 of 13 vi |
| BENEFIT_STACKING / UNSUPPORTED_BENEFIT | "phát huy trí tưởng tượng, rèn luyện khả năng quan sát và sự khéo léo" copied from retailer marketing | 2 of 13 vi |
| Live author block | "Thông tin sản phẩm / Tác giả: …" and "Tác giả của ấn phẩm là X." left in the live Woo text by the pre-2026-10-16 payload | 13 published products |

Also seen elsewhere:
- **Placeholder drafts:** generic safe drafts with internal workflow text ("cần được người quản lý kiểm tra"). These fail the hard gates and are never approved.
- **Same title, different book:** one Vietnamese title can name different works. Both "Tủ Sách Vàng Cho Con - Bà Chúa Tuyết" (Andersen's *The Snow Queen*) and "Cổ Tích Thế Giới Hay Nhất - Bà Chúa Tuyết" (a different tale: spindle in the well, shaken bedding becomes snow) exist. Never carry a translated title or origin across without verification.

## Scoring (explainable, `content_quality_score.py`)

| Dimension | Weight | Main signals |
|---|---|---|
| Specificity and usefulness | 30 | narrative evidence items used; generic filler |
| Natural language and engagement | 25 | metadata opening, stock closing, sales stacking, repeated openers, monotone rhythm |
| Structure and readability | 20 | 2–5 paragraphs, ≤ 700 characters each, sentence-length band per language |
| Reader relevance | 15 | reader sentence present; ≤ 1 suitability phrase; no benefit stacking |
| Originality and non-repetition | 10 | verbatim run vs sources; repeated phrases; overlap with other products |

- **Target:** 85, a calibration threshold only.
- **Calibration:** the approved example scores 100. The FAHASA-2026-001 Vietnamese texts score 73–98 (mean 83, 4 of 13 at 85 or above). The placeholder drafts score 65–74 and fail the gates.
- **Hard gates (never overridden by the score):** the existing approval validators, plus evidence support (unsupported names, numbers or benefits, or use of an UNKNOWN fact).
