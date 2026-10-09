"""Actual pinned PyGithub page contract, using only a loopback HTTP fixture."""
from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version

from starwatch.release_collector import LiveReleaseSource


@unittest.skipUnless(importlib.util.find_spec('github'), 'PyGithub runtime dependency not installed')
class PinnedPyGithubContractTest(unittest.TestCase):
    def test_two_list_pages_do_not_request_release_details(self):
        from github import Github

        self.assertEqual(version('PyGithub'), '2.2.0', 'SDK upgrade needs a new page-cost review')
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith('/repos/owner/repo/releases?'):
                    from urllib.parse import parse_qs, urlsplit
                    page = int(parse_qs(urlsplit(self.path).query).get('page', ['1'])[0])
                    rows = [{'id': 1}, {'id': 2}] if page == 1 else [{'id': 3}]
                    body = json.dumps(rows).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Github(base_url=f'http://127.0.0.1:{server.server_port}',
                            per_page=2, retry=0, seconds_between_requests=0)
            source = LiveReleaseSource(client=client, per_page=2)
            first = source.fetch_page('owner/repo', 1, 2)
            second = source.fetch_page('owner/repo', 2, 2)
            self.assertEqual([row['id'] for row in first.releases], [1, 2])
            self.assertEqual([row['id'] for row in second.releases], [3])
            self.assertTrue(first.has_next)
            self.assertFalse(second.has_next)
            self.assertEqual(len(requests), 2, 'list page must cost one GET, not N detail GETs')
            self.assertTrue(all('/releases?' in request for request in requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
