from gridlib import Grid


def test_set_renders_immediately():
    g = Grid(2, 2)
    g.set(0, 0, "a")
    assert g.text.startswith("a")
    assert g.render_count == 2  # constructor + one set


def test_set_many_renders_once():
    g = Grid(3, 3)
    g.set_many((r, c, r * 3 + c) for r in range(3) for c in range(3))
    assert g.render_count == 2  # constructor + one batch

    eager = Grid(3, 3)
    for r in range(3):
        for c in range(3):
            eager.set(r, c, r * 3 + c)
    assert g.text == eager.text


def test_set_many_empty_still_renders_once():
    g = Grid(2, 2)
    g.set_many([])
    assert g.render_count == 2
    assert g.text == Grid(2, 2).text


HL_ON, HL_OFF = "\x1b[7m", "\x1b[27m"


def _plain(text):
    return text.replace(HL_ON, "").replace(HL_OFF, "")


def test_default_output_identical_with_flag_off():
    g = Grid(2, 2)
    h = Grid(2, 2, highlight_changes=False)
    g.set_many([(0, 0, "a"), (1, 1, "b")])
    h.set_many([(0, 0, "a"), (1, 1, "b")])
    assert g.text == h.text
    assert HL_ON not in g.text


def test_highlight_marks_only_changed_cells():
    g = Grid(2, 3, highlight_changes=True)
    g.set_many([(0, 0, "a"), (0, 1, "b"), (1, 2, "c")])
    g.set_many([(0, 0, "a"), (0, 1, "B"), (1, 2, "c")])  # only (0,1) changes
    assert g.text.count(HL_ON) == 1
    assert HL_ON + "B".ljust(8) + HL_OFF in g.text
    assert g.text.count("|") == 2 * 2  # separators untouched
    assert _plain(g.text) == "\n".join(
        "|".join(cell.ljust(8) for cell in row) for row in [["a", "B", ""], ["", "", "c"]]
    )


def test_highlight_cleared_by_next_update():
    g = Grid(2, 2, highlight_changes=True)
    g.set(0, 0, "x")
    assert g.text.count(HL_ON) == 1
    g.set(0, 0, "x")  # same value: no change, old mark cleared
    assert HL_ON not in g.text


def test_constructor_render_marks_nothing():
    assert HL_ON not in Grid(3, 3, highlight_changes=True).text
