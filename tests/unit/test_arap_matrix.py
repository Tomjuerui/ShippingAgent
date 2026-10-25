"""AR/AP 决策矩阵穷举单测（WP-06 DoD，≥24 用例，含双重反转陷阱回归）。"""

from __future__ import annotations

import pytest

from app.agent.nodes.arap import determine_posting_type, document_amount

ENTITY = "Origin Agent"
ALIASES = ["Origin Agent Co., Ltd.", "OA"]
AS_ISSUER = ("Origin Agent", "Destination Agent")
AS_RECIPIENT = ("Destination Agent", "Origin Agent")
THIRD_PARTY = ("Destination Agent", "Another Customer Ltd.")
SELF_TRADE = ("Origin Agent", "Origin Agent Co., Ltd.")
AR_KEYWORDS = ["collect from", "bill to"]
AP_KEYWORDS = ["we will pay", "debit us"]


def _call(doc_type, party, amount, policy="reversed_posting", statement=None, entity=ENTITY, aliases=None):
    issuer, recipient = party
    return determine_posting_type(
        document_type=doc_type,
        issuer=issuer,
        recipient=recipient,
        entity=entity,
        aliases=aliases if aliases is not None else ALIASES,
        amount=amount,
        policy=policy,
        direction_statement=statement,
        ar_keywords=AR_KEYWORDS,
        ap_keywords=AP_KEYWORDS,
    )


MATRIX_CASES = [
    # id, doc_type, party, amount, policy, expected posting, expected review, multiplier
    ("DN 签发方正数", "Debit Note", AS_ISSUER, 40.0, "reversed_posting", "AR", None, 1.0),
    ("DN 接收方正数", "Debit Note", AS_RECIPIENT, 40.0, "reversed_posting", "AP", None, 1.0),
    ("DN 签发方负数反转", "Debit Note", AS_ISSUER, -40.0, "reversed_posting", "AP", None, 1.0),
    ("DN 接收方负数反转", "Debit Note", AS_RECIPIENT, -40.0, "reversed_posting", "AR", None, 1.0),
    ("Invoice 签发方正数", "Invoice", AS_ISSUER, 40.0, "reversed_posting", "AR", None, 1.0),
    ("Invoice 接收方正数", "Invoice", AS_RECIPIENT, 40.0, "reversed_posting", "AP", None, 1.0),
    ("Invoice 签发方负数", "Invoice", AS_ISSUER, -40.0, "reversed_posting", "AP", None, 1.0),
    ("Invoice 接收方负数", "Invoice", AS_RECIPIENT, -40.0, "reversed_posting", "AR", None, 1.0),
    ("CN 签发方正数", "Credit Note", AS_ISSUER, 40.0, "reversed_posting", "AP", None, 1.0),
    ("CN 接收方正数", "Credit Note", AS_RECIPIENT, 40.0, "reversed_posting", "AR", None, 1.0),
    ("CN 签发方负数不二次反转", "Credit Note", AS_ISSUER, -40.0, "reversed_posting", "AP", None, 1.0),
    ("CN 接收方负数→AR（陷阱回归）", "Credit Note", AS_RECIPIENT, -40.0, "reversed_posting", "AR", None, 1.0),
    ("CREDIT NOTE 签发方正数", "CREDIT NOTE", AS_ISSUER, 40.0, "reversed_posting", "AP", None, 1.0),
    ("CREDIT NOTE 接收方负数", "CREDIT NOTE", AS_RECIPIENT, -40.0, "reversed_posting", "AR", None, 1.0),
    ("CN 签发方零金额", "Credit Note", AS_ISSUER, 0.0, "reversed_posting", "AP", None, 1.0),
    ("CN 接收方零金额", "Credit Note", AS_RECIPIENT, 0.0, "reversed_posting", "AR", None, 1.0),
    ("DN 签发方零金额", "Debit Note", AS_ISSUER, 0.0, "reversed_posting", "AR", None, 1.0),
    ("DN 第三方无法对应", "Debit Note", THIRD_PARTY, 40.0, "reversed_posting", None, "ENTITY_UNRESOLVED", 1.0),
    ("CN 第三方无法对应", "Credit Note", THIRD_PARTY, -40.0, "reversed_posting", None, "ENTITY_UNRESOLVED", 1.0),
    ("DN 自交易", "Debit Note", SELF_TRADE, 40.0, "reversed_posting", None, "SELF_TRADE", 1.0),
    ("CN 自交易", "Credit Note", SELF_TRADE, -40.0, "reversed_posting", None, "SELF_TRADE", 1.0),
    ("CN 同科目红冲签发方正值", "Credit Note", AS_ISSUER, 40.0, "same_account_offset", "AR", None, -1.0),
    ("CN 同科目红冲接收方正值", "Credit Note", AS_RECIPIENT, 40.0, "same_account_offset", "AP", None, -1.0),
    ("CN 同科目红冲签发方负值", "Credit Note", AS_ISSUER, -40.0, "same_account_offset", "AR", None, 1.0),
    ("DN 负数在两种口径下都反转", "Debit Note", AS_ISSUER, -40.0, "same_account_offset", "AP", None, 1.0),
    ("全大写 DEBIT NOTE", "DEBIT NOTE", AS_RECIPIENT, -40.0, "reversed_posting", "AR", None, 1.0),
    ("CN 简写", "CN", AS_RECIPIENT, -40.0, "reversed_posting", "AR", None, 1.0),
]


@pytest.mark.parametrize(
    "case_id,doc_type,party,amount,policy,expected_posting,expected_review,multiplier",
    MATRIX_CASES,
    ids=[case[0] for case in MATRIX_CASES],
)
def test_matrix_exhaustive(
    case_id, doc_type, party, amount, policy, expected_posting, expected_review, multiplier
):
    decision = _call(doc_type, party, amount, policy)
    assert decision.posting_type == expected_posting
    assert decision.review_reason == expected_review
    assert decision.amount_multiplier == multiplier
    assert decision.reasoning[-1].startswith("结论") or expected_posting is None


def test_credit_note_negative_regression_is_explicit():
    """附录 A 必测回归：CN × 接收方 × -40.00 → AR，绝不二次反转。"""
    decision = _call("Credit Note", AS_RECIPIENT, -40.0)
    assert decision.posting_type == "AR"
    assert any("不二次反转" in line for line in decision.reasoning)


def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        _call("Debit Note", AS_ISSUER, 40.0, policy="not-a-policy")


def test_alias_suffix_matching():
    decision = _call("Debit Note", ("Origin Agent Co., Ltd.", "Destination Agent"), 40.0)
    assert decision.posting_type == "AR"


def test_fuzzy_entity_matching():
    decision = _call(
        "Debit Note",
        ("Origin Agent Logistics", "Destination Agent"),
        40.0,
    )
    assert decision.posting_type == "AR"


def test_generic_company_word_not_matched():
    decision = _call("Debit Note", ("Agent", "Destination Agent"), 40.0)
    assert decision.posting_type is None
    assert decision.review_reason == "ENTITY_UNRESOLVED"


def test_direction_statement_consistent():
    decision = _call(
        "Debit Note", AS_RECIPIENT, 40.0, statement="we will pay the invoice"
    )
    assert decision.posting_type == "AP"
    assert decision.review_reason is None


def test_direction_statement_contradiction_reviews():
    decision = _call(
        "Debit Note", AS_RECIPIENT, 40.0, statement="we will pay the invoice"
    )
    assert decision.posting_type == "AP"
    assert decision.review_reason is None
    contradiction = _call(
        "Debit Note", AS_ISSUER, 40.0, statement="we will pay the invoice"
    )
    assert contradiction.posting_type == "AR"
    assert contradiction.review_reason == "OTHER"


def test_direction_statement_ar_contradiction():
    decision = _call(
        "Debit Note", AS_RECIPIENT, 40.0, statement="please collect from the shipper"
    )
    assert decision.posting_type == "AP"
    assert decision.review_reason == "OTHER"


def test_document_amount_falls_back_to_totals():
    assert document_amount([None, None], [40.0]) == 40.0
    assert document_amount([20.0, 20.0], [999.0]) == 40.0
