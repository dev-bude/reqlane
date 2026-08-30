from dashboard import MetricsView, ROWS, COLS


HL_ON = "\x1b[7m"


def test_tick_updates_all_cells():
    v = MetricsView()
    v.tick(0)
    assert v.grid.text.count("|") == ROWS * (COLS - 1)


def test_changed_cells_are_highlighted():
    v = MetricsView()
    v.tick(0)
    assert v.grid.text.count(HL_ON) == ROWS * COLS

    # Same t → same values → nothing changed, nothing highlighted.
    v.tick(0)
    assert HL_ON not in v.grid.text

    v.tick(1)
    assert v.grid.text.count(HL_ON) == ROWS * COLS
    assert v.grid.text.count("|") == ROWS * (COLS - 1)
