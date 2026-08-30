import time

from dashboard import MetricsView, ROWS, COLS

view = MetricsView()
before = view.grid.render_count
t0 = time.perf_counter()
view.tick(1)
dt = time.perf_counter() - t0
print(f"cells={ROWS*COLS} tick={dt*1000:.0f} ms renders={view.grid.render_count - before}")
