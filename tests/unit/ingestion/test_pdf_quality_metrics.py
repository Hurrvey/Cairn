from scripts.evaluate_pdf_stage import structure_score


def test_structure_f1_counts_wrong_extra_and_missing_cells():
    gold = [(0, 1, 0, 1), (0, 1, 1, 2)]
    score = structure_score([(0, 1, 0, 1), (0, 1, 2, 3)], gold)
    assert score == {
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
        "true_positive": 1,
        "predicted": 2,
        "expected": 2,
    }
    assert structure_score([], gold)["f1"] == 0
    assert structure_score([], None) is None
