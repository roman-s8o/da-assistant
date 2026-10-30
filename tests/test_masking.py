import pandas as pd
import pytest

from da_assistant.privacy import MASK, mask_rows, mask_text, mask_value
from da_assistant.prompts import guard_answer


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("Write to jane.doe+x@example.co.uk now.", f"Write to {MASK} now."),
        ("Call +1 555 123 4567 or (555) 123-4567.", f"Call {MASK} or {MASK}."),
        ("She lives at 6040 Webb Mountains, Springfield.", f"She lives at {MASK}, Springfield."),
        ("| 139 Wheeler Pass Suite 607 |", f"| {MASK} |"),
    ],
)
def test_mask_text_finds_personal_data(text, masked):
    assert mask_text(text)[0] == masked


@pytest.mark.parametrize(
    "text",
    [
        "Revenue was $8,024.59 on 2026-09-01 from 12,345 orders.",
        "Customer 61582 spent $1,748.",
        "| Michael Kors Mens 2 Button Flat Front Gray Sharkskin Wool Suit | $420 |",
        "Top seller: Levi's 501 Original Fit Jeans.",
    ],
)
def test_mask_text_leaves_business_text_alone(text):
    assert mask_text(text) == (text, 0)


def test_mask_value_masks_a_whole_address():
    assert mask_value("4507 Wright Lodge Apt. 262") == (MASK, 1)
    assert mask_value("Sean John Mens 1 Button Flat Front Light Gray Suit")[1] == 0


def test_mask_rows_drops_pii_columns_and_masks_text():
    rows = pd.DataFrame(
        {
            "email": ["a@b.com", None],
            "note": ["mail a@b.com", "fine"],
            "revenue": [1.0, 2.0],
        }
    ).astype({"note": "str"})

    masked, hits = mask_rows(rows)

    assert list(masked.columns) == ["note", "revenue"]
    assert masked["note"].tolist() == [f"mail {MASK}", "fine"]
    assert hits == 2


@pytest.mark.parametrize(
    "answer",
    [
        "Definition used: the date of a sale is order_items.created_at.",
        "Revenue excludes cancelled and returned items.",
        "I am a data analyst assistant for an online clothing retailer.",
    ],
)
def test_normal_answers_are_not_taken_for_a_leak(answer):
    assert guard_answer(answer) == (answer, False)


def test_a_quoted_instruction_is_a_leak():
    # Short sentences are not checked: a normal answer can contain them by chance.
    answer = "Here they are: Let SQL do the work: aggregate, sort and limit in the query"

    assert guard_answer(answer)[1]
