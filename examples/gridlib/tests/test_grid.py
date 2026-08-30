from gridlib import Grid


def test_set_renders_immediately():
    g = Grid(2, 2)
    g.set(0, 0, "a")
    assert g.text.startswith("a")
    assert g.render_count == 2  # constructor + one set
