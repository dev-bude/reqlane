# gridlib

Minimal text grid. `Grid(rows, cols)`; `grid.set(r, c, value)` updates a cell and
re-renders the whole grid immediately; `grid.text` is the rendered output.
Public API is considered stable (v1.x).

For bulk updates, `with grid.batch():` defers rendering — `set()` calls inside
render nothing, and exactly one render happens when the outermost batch exits
(also on exception). Outside a batch, `set()` keeps its eager v1 contract.
