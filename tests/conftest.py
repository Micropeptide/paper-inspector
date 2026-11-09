"""Shared test setup."""
import os
import tempfile

# The server answers only loopback Host names; FastAPI's TestClient calls itself "testserver".
os.environ.setdefault("PAPERINSPECTOR_TESTING", "1")
# Runs, settings and the cache go to a throwaway folder, never the real data folder, even if a test forgets to
# point PAPERINSPECTOR_HOME somewhere itself.
os.environ.setdefault("PAPERINSPECTOR_HOME", tempfile.mkdtemp(prefix="paperinspector-tests-"))
