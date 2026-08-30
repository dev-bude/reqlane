"""gridlib — minimal text grid with eager rendering."""

from contextlib import contextmanager
from typing import Iterator

__version__ = "1.1.0"


class Grid:
    def __init__(self, rows: int, cols: int, width: int = 8) -> None:
        self.rows = rows
        self.cols = cols
        self.width = width
        self._cells: list[list[str]] = [[""] * cols for _ in range(rows)]
        self._batch_depth = 0
        self.render_count = 0
        self.text = ""
        self._render()

    def set(self, r: int, c: int, value: object) -> None:
        """Set one cell. Renders the whole grid on every call (v1 contract),
        unless inside a batch() block — then rendering is deferred to batch exit."""
        self._cells[r][c] = str(value)
        if self._batch_depth == 0:
            self._render()

    @contextmanager
    def batch(self) -> Iterator["Grid"]:
        """Defer rendering: set() calls inside do not render; exactly one
        render happens when the outermost batch exits, even on exception,
        so text is never stale. Re-entrant."""
        self._batch_depth += 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self._render()

    def _render(self) -> None:
        self.render_count += 1
        self.text = "\n".join(
            "|".join(cell.ljust(self.width) for cell in row) for row in self._cells
        )
