import pytest


@pytest.fixture(scope="session")
def tk_display():
    """Keep one native Tcl/Tk interpreter for all isolated GUI test windows."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
