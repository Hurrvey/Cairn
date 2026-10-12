from __future__ import annotations

from cairn.ingestion.pdf_layout import PositionedWord, layout_page


def test_layout_reads_simple_columns_top_to_bottom_then_left_to_right() -> None:
    words = [
        PositionedWord("R2", (340, 680, 365, 692)),
        PositionedWord("L2", (40, 680, 65, 692)),
        PositionedWord("R1", (340, 720, 365, 732)),
        PositionedWord("L1", (40, 720, 65, 732)),
    ]

    blocks = layout_page(words, page=3, width=612, height=792, source_url="source:pdf")

    assert [block.text for block in blocks] == ["L1", "L2", "R1", "R2"]
    assert [block.ordinal for block in blocks] == [0, 1, 2, 3]
    assert all(block.page == 3 for block in blocks)
    assert all(block.source_url == "source:pdf" for block in blocks)


def test_layout_detects_only_consecutive_aligned_rows_as_a_table() -> None:
    words = [
        PositionedWord("Title", (40, 740, 75, 752)),
        PositionedWord("Value", (330, 740, 370, 752)),
        PositionedWord("Name", (40, 660, 75, 672)),
        PositionedWord("Amount", (180, 660, 225, 672)),
        PositionedWord("A|B", (40, 638, 65, 650)),
        PositionedWord("<42>", (180, 638, 210, 650)),
    ]

    blocks = layout_page(words, page=1, width=612, height=792, source_url=None)

    assert [block.text for block in blocks[:2]] == ["Title", "Value"]
    assert blocks[2].kind == "table"
    assert blocks[2].text == ("| Name | Amount |\n| --- | --- |\n| A\\|B | &lt;42&gt; |")
    assert blocks[2].bbox == (40, 638, 225, 672)


def test_layout_keeps_ambiguous_irregular_rows_as_text() -> None:
    words = [
        PositionedWord("One", (30, 700, 55, 712)),
        PositionedWord("Two", (160, 700, 185, 712)),
        PositionedWord("Three", (55, 675, 90, 687)),
        PositionedWord("Four", (280, 675, 310, 687)),
    ]

    blocks = layout_page(words, page=1, width=400, height=800, source_url=None)

    assert all(block.kind == "paragraph" for block in blocks)
    assert sorted(" ".join(block.text for block in blocks).split()) == [
        "Four",
        "One",
        "Three",
        "Two",
    ]


def test_spanning_header_and_footer_stay_around_local_columns() -> None:
    words = [
        PositionedWord("HEADING", (40, 750, 560, 770)),
        PositionedWord("L1", (40, 700, 80, 712)),
        PositionedWord("L2", (40, 680, 80, 692)),
        PositionedWord("R1", (330, 700, 370, 712)),
        PositionedWord("R2", (330, 680, 370, 692)),
        PositionedWord("FOOTER", (40, 40, 560, 55)),
    ]

    blocks = layout_page(words, page=1, width=600, height=800, source_url=None)

    assert [block.text for block in blocks] == ["HEADING", "L1 L2", "R1 R2", "FOOTER"]


def test_wide_financial_rows_with_numeric_body_form_a_table() -> None:
    words = [
        PositionedWord("Metric", (40, 700, 90, 712)),
        PositionedWord("2025", (300, 700, 335, 712)),
        PositionedWord("2024", (520, 700, 555, 712)),
        PositionedWord("Revenue", (40, 680, 100, 692)),
        PositionedWord("100", (300, 680, 330, 692)),
        PositionedWord("90", (520, 680, 540, 692)),
        PositionedWord("Cost", (40, 660, 75, 672)),
        PositionedWord("50", (300, 660, 320, 672)),
        PositionedWord("45", (520, 660, 540, 672)),
    ]

    blocks = layout_page(words, page=1, width=600, height=800, source_url=None)

    assert len(blocks) == 1
    assert blocks[0].kind == "table"
    assert blocks[0].text == (
        "| Metric | 2025 | 2024 |\n| --- | --- | --- |\n| Revenue | 100 | 90 |\n| Cost | 50 | 45 |"
    )


def test_wide_two_column_prose_is_not_misclassified_as_a_table() -> None:
    words = [
        PositionedWord("Left opening sentence", (40, 700, 170, 712)),
        PositionedWord("Right opening sentence", (360, 700, 510, 712)),
        PositionedWord("Left continuation", (40, 680, 150, 692)),
        PositionedWord("Right continuation", (360, 680, 480, 692)),
    ]

    blocks = layout_page(words, page=1, width=600, height=800, source_url=None)

    assert all(block.kind == "paragraph" for block in blocks)


def test_wide_formula_columns_are_not_misclassified_as_a_table() -> None:
    words = [
        PositionedWord("Equation", (40, 700, 105, 712)),
        PositionedWord("Number", (380, 700, 435, 712)),
        PositionedWord("E=mc^2", (40, 680, 100, 692)),
        PositionedWord("(1)", (380, 680, 400, 692)),
        PositionedWord("F=ma", (40, 660, 80, 672)),
        PositionedWord("(2)", (380, 660, 400, 672)),
    ]

    blocks = layout_page(words, page=1, width=600, height=800, source_url=None)

    assert all(block.kind == "paragraph" for block in blocks)


def test_wide_rows_with_drifting_numeric_columns_are_not_a_table() -> None:
    words = [
        PositionedWord("Label", (40, 700, 80, 712)),
        PositionedWord("First", (300, 700, 340, 712)),
        PositionedWord("Second", (520, 700, 570, 712)),
        PositionedWord("Alpha", (40, 680, 80, 692)),
        PositionedWord("100", (300, 680, 330, 692)),
        PositionedWord("n/a", (520, 680, 545, 692)),
        PositionedWord("Beta", (40, 660, 75, 672)),
        PositionedWord("n/a", (300, 660, 325, 672)),
        PositionedWord("50", (520, 660, 540, 672)),
    ]

    blocks = layout_page(words, page=1, width=600, height=800, source_url=None)

    assert all(block.kind == "paragraph" for block in blocks)


def test_explicit_line_breaks_preserve_extractor_reading_order() -> None:
    """pdfium already orders words and marks line ends; geometry must not re-sort them."""
    words = [
        PositionedWord("Body", (72, 700, 100, 712), line_break=True),
        PositionedWord("one", (104, 700, 130, 712)),
        PositionedWord("x=1", (240, 680, 270, 692), line_break=True),
        PositionedWord("(1)", (513, 680, 540, 692)),
        PositionedWord("Body", (72, 660, 100, 672), line_break=True),
        PositionedWord("two", (104, 660, 130, 672)),
    ]

    blocks = layout_page(words, page=1, width=612, height=792, source_url=None)

    assert [block.text for block in blocks] == ["Body one", "x=1 (1)", "Body two"]


def test_wrapped_lines_merge_into_one_paragraph() -> None:
    words = [
        PositionedWord("First", (72, 700, 110, 712), line_break=True),
        PositionedWord("line", (114, 700, 140, 712)),
        PositionedWord("second", (72, 686, 120, 698), line_break=True),
        PositionedWord("line", (124, 686, 150, 698)),
        PositionedWord("Indented", (120, 660, 180, 672), line_break=True),
    ]

    blocks = layout_page(words, page=1, width=612, height=792, source_url=None)

    assert [block.text for block in blocks] == ["First line second line", "Indented"]
    assert blocks[0].bbox == (72, 686, 150, 712)
