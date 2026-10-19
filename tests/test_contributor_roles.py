"""Verified contributor roles (2026-10-17 owner request).

Protects: a reteller / illustrator is never presented as "Tác giả"; roles
come only from recorded evidence; no name is invented or dropped; the
verified author column is never rewritten; an existing record is never
silently overwritten. Offline.
"""
from __future__ import annotations

import pytest

import record_contributor_roles as rcr
from src.domain.rules import contributor_roles, woo_presentation

AUTHOR_FIELD = "Hiếu Minh, Cloud Pillow Studio"
CREDIT = "Hiếu Minh kể lại | Cloud Pillow Studio minh họa"
SOURCE_URL = "https://www.fahasa.com/co-tich-the-gioi-hay-nhat-co-be-lo-lem.html"
ROLES = [
    {"name": "Hiếu Minh", "role": "RETELLER"},
    {"name": "Cloud Pillow Studio", "role": "ILLUSTRATOR"},
]
PRODUCT = {"internal_product_id": "p1", "candidate_id": "c1", "author": AUTHOR_FIELD, "product_metadata": {"x": 1}}
REFERENCES = [{"reference_id": "r1", "source_url_id": "s1", "source_type": "FAHASA", "source_url": SOURCE_URL}]
EVIDENCE = {"source_url": SOURCE_URL, "image_url": "https://cdn1.fahasa.com/cover.jpg", "credit_text": CREDIT}
CONTENT = {"product_details": "Tác giả: Hiếu Minh, Cloud Pillow Studio\nNhà xuất bản: Kim Đồng\nSố trang: 20"}


def _stored(roles=ROLES):
    return {"contributor_roles": {"contributors": roles, "evidence": {"credit_text": CREDIT}}}


def test_merged_author_field_without_roles_keeps_legacy_attribute():
    attributes = woo_presentation.build_attributes({"author": AUTHOR_FIELD}, CONTENT)
    assert attributes[0]["name"] == "Tác giả"


def test_verified_roles_replace_the_merged_author_attribute():
    product = {"author": AUTHOR_FIELD, "product_metadata": _stored()}
    pairs = woo_presentation.comparable_attributes(woo_presentation.build_attributes(product, CONTENT))
    assert pairs == [
        ("Người kể lại", ("Hiếu Minh",)),
        ("Minh họa", ("Cloud Pillow Studio",)),
        ("Nhà xuất bản", ("Kim Đồng",)),
        ("Số trang", ("20",)),
    ]
    assert "Tác giả" not in [name for name, _ in pairs]


def test_roles_follow_canonical_order_and_group_names():
    roles = [
        {"name": "B", "role": "ILLUSTRATOR"},
        {"name": "A", "role": "AUTHOR"},
        {"name": "C", "role": "ILLUSTRATOR"},
    ]
    groups = contributor_roles.attribute_groups(contributor_roles.normalize_contributors(roles))
    assert groups == [("Tác giả", ["A"]), ("Minh họa", ["B", "C"])]


@pytest.mark.parametrize("metadata", [None, {}, {"contributor_roles": "x"}, _stored([{"name": "X", "role": "EDITOR"}])])
def test_malformed_record_falls_back_to_author_field(metadata):
    assert contributor_roles.verified_contributors(metadata) is None


def test_plan_records_roles_additively_and_never_touches_author():
    plan = rcr.plan_record(PRODUCT, REFERENCES, ROLES, EVIDENCE, "t")
    assert plan["outcome"] == rcr.RECORDED
    assert set(plan["update"]) == {"product_metadata", "updated_at"}
    assert plan["update"]["product_metadata"]["x"] == 1
    assert plan["record"]["evidence"]["source_url_id"] == "s1"


def test_name_not_in_credit_text_is_refused():
    plan = rcr.plan_record(PRODUCT, REFERENCES, ROLES, {**EVIDENCE, "credit_text": "Hiếu Minh kể lại"}, "t")
    assert plan["outcome"] == rcr.REFUSED and plan["update"] is None


def test_invented_contributor_is_refused():
    roles = ROLES + [{"name": "Charles Perrault", "role": "AUTHOR"}]
    plan = rcr.plan_record(PRODUCT, REFERENCES, roles, {**EVIDENCE, "credit_text": CREDIT + " Charles Perrault"}, "t")
    assert plan["outcome"] == rcr.REFUSED


def test_original_author_needs_the_credit_text_but_not_the_author_field():
    roles = ROLES + [{"name": "Hans Christian Andersen", "role": "ORIGINAL_AUTHOR"}]
    assert rcr.plan_record(PRODUCT, REFERENCES, roles, EVIDENCE, "t")["outcome"] == rcr.REFUSED
    credited = {**EVIDENCE, "credit_text": "Nguyên tác: Hans Christian Andersen | " + CREDIT}
    assert rcr.plan_record(PRODUCT, REFERENCES, roles, credited, "t")["outcome"] == rcr.RECORDED


def test_translator_and_illustrator_may_be_credited_outside_the_author_field():
    product = {**PRODUCT, "author": "Wouter De Jong"}
    refs = [{**REFERENCES[0], "raw_metadata": {"translator": "Trịnh Tuyết Linh"}}]
    roles = [
        {"name": "Wouter De Jong", "role": "AUTHOR"},
        {"name": "Hein de Kort", "role": "ILLUSTRATOR"},
        {"name": "Trịnh Tuyết Linh", "role": "TRANSLATOR"},
    ]
    credit = {**EVIDENCE, "credit_text": "Tác giả: Wouter De Jong | Minh họa: Hein de Kort | Người dịch: Trịnh Tuyết Linh"}
    assert rcr.plan_record(product, refs, roles, credit, "t")["outcome"] == rcr.RECORDED


def test_translator_must_match_the_reference_translator_field():
    product = {**PRODUCT, "author": "Wouter De Jong"}
    refs = [{**REFERENCES[0], "raw_metadata": {"translator": "Trịnh Tuyết Linh"}}]
    roles = [{"name": "Wouter De Jong", "role": "AUTHOR"}, {"name": "Someone Else", "role": "TRANSLATOR"}]
    credit = {**EVIDENCE, "credit_text": "Wouter De Jong | Người dịch: Someone Else"}
    assert rcr.plan_record(product, refs, roles, credit, "t")["outcome"] == rcr.REFUSED


def test_a_reteller_outside_the_author_field_is_still_refused():
    roles = ROLES + [{"name": "Thanh Thủy", "role": "RETELLER"}]
    credited = {**EVIDENCE, "credit_text": CREDIT + " Thanh Thủy kể lại"}
    assert rcr.plan_record(PRODUCT, REFERENCES, roles, credited, "t")["outcome"] == rcr.REFUSED


def test_dropping_an_author_field_name_is_refused():
    plan = rcr.plan_record(PRODUCT, REFERENCES, ROLES[:1], EVIDENCE, "t")
    assert plan["outcome"] == rcr.REFUSED


def test_unregistered_evidence_url_is_refused():
    plan = rcr.plan_record(PRODUCT, REFERENCES, ROLES, {**EVIDENCE, "source_url": "https://example.com"}, "t")
    assert plan["outcome"] == rcr.REFUSED
    unregistered = [{**REFERENCES[0], "source_url_id": None}]
    assert rcr.plan_record(PRODUCT, unregistered, ROLES, EVIDENCE, "t")["outcome"] == rcr.REFUSED


def test_existing_record_is_never_silently_overwritten():
    product = {**PRODUCT, "product_metadata": _stored()}
    assert rcr.plan_record(product, REFERENCES, ROLES, EVIDENCE, "t")["outcome"] == rcr.UNCHANGED
    swapped = [{"name": "Hiếu Minh", "role": "AUTHOR"}, ROLES[1]]
    plan = rcr.plan_record(product, REFERENCES, swapped, EVIDENCE, "t")
    assert plan["outcome"] == rcr.REFUSED and plan["update"] is None


def test_unknown_role_is_refused():
    plan = rcr.plan_record(PRODUCT, REFERENCES, [{"name": "Hiếu Minh", "role": "WRITER"}], EVIDENCE, "t")
    assert plan["outcome"] == rcr.REFUSED
