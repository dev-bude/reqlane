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
