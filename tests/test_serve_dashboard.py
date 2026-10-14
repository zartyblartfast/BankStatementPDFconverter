import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen
from scripts import dashboard as d


class LauncherTests(unittest.TestCase):
    def test_identity_health_is_read_only(self):
        result = d.app.test_client().get('/health', base_url='http://127.0.0.1:5000')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['app'], 'bankstatement-local-dashboard')
        self.assertEqual(result.json['mode'], 'loopback')
        self.assertIn('instance', result.json)

    def test_reject_nonloopback_bind(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.serve_dashboard'), 'production launcher missing')
        result = subprocess.run([sys.executable, '-m', 'scripts.serve_dashboard', '--host', '0.0.0.0'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('127.0.0.1', result.stderr)

    def test_unrelated_http_server_refused_without_browser(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from unittest.mock import patch
        from scripts.serve_dashboard import main
        class ForeignServer(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"app":"not-our-dashboard"}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), ForeignServer)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            with patch('scripts.serve_dashboard.webbrowser.open') as browser:
                self.assertEqual(main(['--port', str(server.server_port), '--open-browser']), 1)
                browser.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); worker.join(2)

    def test_browser_only_opens_after_identity_readiness(self):
        import threading
        from unittest.mock import patch
        from scripts.serve_dashboard import main
        opened = threading.Event()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        with patch('scripts.serve_dashboard.probe_identity', side_effect=[False, False, True]) as probe, patch('scripts.serve_dashboard.webbrowser.open', side_effect=lambda url: opened.set()) as browser, patch('waitress.create_server') as create:
            create.return_value.run.side_effect = lambda: self.assertTrue(opened.wait(3))
            self.assertEqual(main(['--port', str(port), '--open-browser']), 0)
            self.assertEqual(probe.call_count, 3)
            browser.assert_called_once_with(f'http://127.0.0.1:{port}')
            self.assertEqual(create.call_args.kwargs['host'], '127.0.0.1')
            create.return_value.close.assert_called_once()

    def test_real_waitress_existing_instance_and_occupied_port(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.serve_dashboard'), 'production launcher missing')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            result = subprocess.run([sys.executable, '-m', 'scripts.serve_dashboard', '--port', str(port)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('occupied', result.stderr.lower())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'absent'
            env = {**os.environ, 'BANKSTATEMENT_PROJECT_ROOT': str(root)}
            proc = subprocess.Popen([sys.executable, '-m', 'scripts.serve_dashboard', '--port', str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                health = None
                while time.monotonic() < deadline:
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/health', timeout=.3) as response:
                            health = json.load(response)
                        break
                    except OSError:
                        time.sleep(.05)
                self.assertIsNotNone(health, 'Waitress did not become ready')
                self.assertEqual(health['app'], 'bankstatement-local-dashboard')
                result = subprocess.run([sys.executable, '-m', 'scripts.serve_dashboard', '--port', str(port)], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('already running', result.stdout.lower())
                self.assertFalse(root.exists())
            finally:
                proc.terminate()
                proc.communicate(timeout=5)
