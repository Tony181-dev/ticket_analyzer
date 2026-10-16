import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    except (AttributeError, ValueError):
        pass

from app.runtime import run_tool  # noqa: E402  (absolute: also works as the PyInstaller entry script)

if not run_tool(sys.argv[1:]):
    from app.server import main  # noqa: E402

    main()
