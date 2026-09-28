import json, os, subprocess, sys, time, urllib.request, urllib.error, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

def get(url):
    with urllib.request.urlopen(url, timeout=4) as r:
        return json.loads(r.read())

def post(url):
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.loads(r.read())

class SysWatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 18991
        cls.p = subprocess.Popen([PYTHON, str(ROOT / "server.py"), str(cls.port)], cwd=Path("/tmp"))
        for _ in range(40):
            try:
                get(f"http://127.0.0.1:{cls.port}/api/stats")
                break
            except Exception:
                time.sleep(.2)
        else:
            raise RuntimeError("server failed to start")

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate()
        cls.p.wait(timeout=5)

    def test_schema(self):
        s = get(f"http://127.0.0.1:{self.port}/api/stats")
        for k in ("os","cpu","memory","swap","processes","processCount","disk","net","monitor"):
            self.assertIn(k, s)
        self.assertIsInstance(s["processes"], list)

    def test_cpu_child_appears(self):
        if os.name == "nt":
            self.skipTest("CPU stress command is POSIX-specific")
        child = subprocess.Popen([PYTHON, "-c", "while True: pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            found = False
            for _ in range(8):
                time.sleep(1)
                rows = [p for p in get(f"http://127.0.0.1:{self.port}/api/stats")["processes"] if p["pid"] == child.pid]
                if rows and rows[0]["cpu"] > 10:
                    found = True
                    break
            self.assertTrue(found)
        finally:
            child.kill()
            child.wait()

    def test_control_dummy(self):
        child = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(30)"])
        try:
            if os.name != "nt":
                self.assertTrue(post(f"http://127.0.0.1:{self.port}/api/kill?pid={child.pid}&sig=19")["ok"])
                self.assertTrue(post(f"http://127.0.0.1:{self.port}/api/kill?pid={child.pid}&sig=18")["ok"])
            self.assertTrue(post(f"http://127.0.0.1:{self.port}/api/kill?pid={child.pid}&sig=15")["ok"])
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_export(self):
        raw = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/export?format=csv", timeout=4).read()
        self.assertIn(b"pid,ppid,name", raw)

    def test_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError):
            get(f"http://127.0.0.1:{self.port}/nope")

if __name__ == "__main__":
    unittest.main()
