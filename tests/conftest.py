import os

# Qt tests run headless; the real window is checked manually under WSLg.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
