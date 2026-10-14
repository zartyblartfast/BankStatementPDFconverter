"""Production local dashboard launcher; no remote bind, debug mode or reloader."""
import argparse
import json
import socket
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


def probe_identity(port: int, expected: dict) -> bool:
    """Use direct loopback HTTP, never a system proxy or redirected server."""
    from urllib.request import HTTPRedirectHandler

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=.5) as response:
            return response.status == 200 and json.loads(response.read(4096)) == expected
    except (OSError, ValueError, URLError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1'])
    parser.add_argument('--port', default=5000, type=int)
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    try:
        from waitress import create_server
        from scripts.dashboard import app, health_identity
    except ImportError as exc:
        print(f'Missing dashboard dependency: {exc}. Run uv pip install --python .venv/Scripts/python.exe -r requirements.txt', file=sys.stderr)
        return 1
    expected = health_identity()
    url = f'http://127.0.0.1:{args.port}'
    if probe_identity(args.port, expected):
        print(f'Local dashboard already running at {url}')
        if args.open_browser:
            webbrowser.open(url)
        return 0
    # Non-listening occupied sockets also need a clear refusal.
    try:
        with socket.socket() as sock:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(('127.0.0.1', args.port))
        server = create_server(app, host='127.0.0.1', port=args.port, threads=4)
    except OSError:
        print(f'Port {args.port} is occupied by another server; no dashboard started. Choose --port or stop that server yourself.', file=sys.stderr)
        return 1

    def ready():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if probe_identity(args.port, expected):
                print(f'Local dashboard ready at {url}', flush=True)
                if args.open_browser:
                    webbrowser.open(url)
                return
            time.sleep(.1)
        print('Dashboard readiness check failed; browser was not opened.', file=sys.stderr)

    threading.Thread(target=ready, daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
