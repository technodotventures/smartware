"""Start and stop a real Smartware server (``smartware serve --http 0 --memory``) for one test class."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]  # .../smartware
SMARTWARE_BIN = ROOT / "packages" / "server" / "bin" / "smartware.js"
NODE = shutil.which("node") or "node"


class SmartwareServer:
    """A server subprocess with its own keys file. ``tokens[name]`` holds each principal's token."""

    def __init__(self, principals: Dict[str, Dict], extra_args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None) -> None:
        self.dir = tempfile.mkdtemp(prefix="smartware-pytest-")
        self.keys = os.path.join(self.dir, "keys.json")
        self.tokens: Dict[str, str] = {}
        for name, spec in principals.items():
            cmd = [NODE, str(SMARTWARE_BIN), "keys", "add", "--keys", self.keys, "--id", spec.get("id", name),
                   "--tenant", spec.get("tenant", "acme"), "--label", name]
            if spec.get("groups"):
                cmd += ["--groups", ",".join(spec["groups"])]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
            self.tokens[name] = r.stdout.strip().splitlines()[-1].strip()
        args = [NODE, str(SMARTWARE_BIN), "serve", "--http", "0", "--memory", "--keys", self.keys, "--log-level", "info"]
        self.proc = subprocess.Popen(args + list(extra_args or []), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True, env={**os.environ, **(env or {})})
        self.url: Optional[str] = None
        ready = threading.Event()
        self.log_lines: List[str] = []

        def drain() -> None:  # keep reading so the pipe never fills
            assert self.proc.stderr is not None
            for line in self.proc.stderr:
                self.log_lines.append(line)
                if self.url is None and '"smartware http ready"' in line:
                    try:
                        self.url = json.loads(line)["url"]
                    except (ValueError, KeyError):
                        pass
                    ready.set()
            ready.set()

        threading.Thread(target=drain, daemon=True).start()
        if not ready.wait(30) or self.url is None:
            self.stop()
            raise RuntimeError("smartware server did not start:\n" + "".join(self.log_lines[-20:]))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        shutil.rmtree(self.dir, ignore_errors=True)


DEFAULT_PRINCIPALS = {
    "alice": {"tenant": "acme", "groups": ["sales"]},
    "bob": {"tenant": "acme", "groups": ["sales"]},
    "carol": {"tenant": "globex"},
    "admin": {"tenant": "acme", "groups": ["admin"]},
}


class ServerTestCase(unittest.TestCase):
    """Each subclass gets its own server, started in setUpClass and stopped in tearDownClass."""

    principals: Dict[str, Dict] = DEFAULT_PRINCIPALS
    server_args: List[str] = []
    server: SmartwareServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = SmartwareServer(cls.principals, cls.server_args)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    @classmethod
    def client(cls, name: str, **kw):
        from smartware_client import SmartwareClient
        return SmartwareClient(cls.server.url, token=cls.server.tokens[name], **kw)


def wait_until(pred, timeout: float = 5.0, every: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(every)
    return pred()
