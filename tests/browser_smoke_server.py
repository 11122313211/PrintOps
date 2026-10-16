"""Isolated, loopback-only test server for the optional browser suite.

All orders are synthetic. No saved model credentials are used and no test state
is written to the repository's data directory.
"""
from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from agent import Memory
from llm_adapter import OpenAICompatiblePlanner


def main():
    with tempfile.TemporaryDirectory(prefix="printops-browser-") as directory:
        server.MEMORY = Memory(Path(directory) / "synthetic.sqlite3", mode="sqlite")
        server.MEMORY_PATH = Path(directory) / "synthetic.sqlite3"
        server.LLM_CONFIG_PATH = Path(directory) / "unused-llm.json"
        server.SAVED_LLM_CONFIG = {}
        server.PLANNER = OpenAICompatiblePlanner()
        with server.build_server(0) as httpd:
            restore = server.install_signal_handlers(httpd)
            print("PRINTOPS_BROWSER_SMOKE_READY " + json.dumps({
                "url": f"http://127.0.0.1:{httpd.server_port}",
                "synthetic": True,
                "llmEnabled": server.PLANNER.enabled,
            }), flush=True)
            try:
                httpd.serve_forever(poll_interval=0.1)
            finally:
                restore()


if __name__ == "__main__":
    main()
