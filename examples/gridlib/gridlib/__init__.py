"""gridlib — minimal text grid with eager rendering."""

__version__ = "1.0.0"


class Grid:
    def __init__(self, rows: int, cols: int, width: int = 8) -> None:
        self.rows = rows
        self.cols = cols
        self.width = width
        self._cells: list[list[str]] = [[""] * cols for _ in range(rows)]
        self.render_count = 0
        self.text = ""
        self._render()

    def set(self, r: int, c: int, value: object) -> None:
        """Set one cell. Renders the whole grid on every call (v1 contract)."""
        self._cells[r][c] = str(value)
        self._render()

    def _render(self) -> None:
        self.render_count += 1
        self.text = "\n".join(
            "|".join(cell.ljust(self.width) for cell in row) for row in self._cells
        )
