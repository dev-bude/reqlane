from gridlib import Grid


import pytest


def test_set_renders_immediately():
    g = Grid(2, 2)
    g.set(0, 0, "a")
    assert g.text.startswith("a")
    assert g.render_count == 2  # constructor + one set


def test_batch_renders_once():
    g = Grid(3, 3)
    with g.batch():
        for r in range(3):
            for c in range(3):
                g.set(r, c, r * 3 + c)
    assert g.render_count == 2  # constructor + one batch exit
    assert g.text.startswith("0")
    assert "8" in g.text


def test_nested_batches_render_once_at_outermost_exit():
    g = Grid(2, 2)
    with g.batch():
        g.set(0, 0, "a")
        with g.batch():
            g.set(1, 1, "b")
        assert g.render_count == 1  # inner exit does not render
    assert g.render_count == 2
    assert "b" in g.text


def test_set_after_batch_is_eager_again():
    g = Grid(2, 2)
    with g.batch():
        g.set(0, 0, "a")
    g.set(0, 1, "b")
    assert g.render_count == 3  # constructor + batch exit + eager set


def test_batch_renders_on_exception():
    g = Grid(2, 2)
    with pytest.raises(ValueError):
        with g.batch():
            g.set(0, 0, "a")
            raise ValueError("boom")
    assert g.render_count == 2  # text not stale after failed batch
    assert g.text.startswith("a")
