from dashboard import MetricsView, ROWS, COLS


def test_tick_updates_all_cells():
    v = MetricsView()
    v.tick(0)
    assert v.grid.text.count("|") == ROWS * (COLS - 1)
