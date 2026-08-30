"""gridlib — minimal text grid with eager rendering."""

from collections.abc import Iterable

__version__ = "1.2.0"

_HL_ON = "\x1b[7m"
_HL_OFF = "\x1b[27m"


class Grid:
    def __init__(
        self, rows: int, cols: int, width: int = 8, highlight_changes: bool = False
    ) -> None:
        self.rows = rows
        self.cols = cols
        self.width = width
        self.highlight_changes = highlight_changes
        self._cells: list[list[str]] = [[""] * cols for _ in range(rows)]
        self._changed: set[tuple[int, int]] = set()
        self.render_count = 0
        self.text = ""
        self._render()

    def set(self, r: int, c: int, value: object) -> None:
        """Set one cell. Renders the whole grid on every call (v1 contract)."""
        text = str(value)
        self._changed = {(r, c)} if text != self._cells[r][c] else set()
        self._cells[r][c] = text
        self._render()

    def set_many(self, updates: Iterable[tuple[int, int, object]]) -> None:
        """Apply many (r, c, value) updates, rendering exactly once at the end."""
        self._changed = set()
        for r, c, value in updates:
            text = str(value)
            if text != self._cells[r][c]:
                self._changed.add((r, c))
                self._cells[r][c] = text
        self._render()

    def _render(self) -> None:
        self.render_count += 1
        if self.highlight_changes and self._changed:
            changed = self._changed
            self.text = "\n".join(
                "|".join(
                    _HL_ON + cell.ljust(self.width) + _HL_OFF
                    if (r, c) in changed
                    else cell.ljust(self.width)
                    for c, cell in enumerate(row)
                )
                for r, row in enumerate(self._cells)
            )
        else:
            self.text = "\n".join(
                "|".join(cell.ljust(self.width) for cell in row)
                for row in self._cells
            )
