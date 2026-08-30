"""dashboard — live metrics table on top of gridlib."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gridlib"))

from gridlib import Grid  # noqa: E402

ROWS, COLS = 200, 50


class MetricsView:
    def __init__(self) -> None:
        self.grid = Grid(ROWS, COLS)

    def tick(self, t: int) -> None:
        # Every cell changes every tick; each set() re-renders the whole grid.
        for r in range(ROWS):
            for c in range(COLS):
                self.grid.set(r, c, (r * COLS + c + t) % 997)
