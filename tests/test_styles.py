"""Check actual handler responses without binding a network socket."""
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from monitor import handler_for

class Request:
    def __init__(self, path):
        self.input=io.BytesIO(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n'.encode())
        self.output=io.BytesIO()
    def makefile(self,*args,**kwargs):return self.input
    def sendall(self,data):self.output.write(data)

class StylesTests(unittest.TestCase):
    def response(self,path):
        request=Request(path)
        handler_for(None)(request,('127.0.0.1',10000),SimpleNamespace(server_port=8765))
        raw=request.output.getvalue()
        head,body=raw.split(b'\r\n\r\n',1)
        lines=head.decode().split('\r\n')
        return lines[0],dict(line.split(': ',1) for line in lines[1:]),body

    def test_html_policy_allows_same_origin_stylesheet(self):
        status,headers,body=self.response('/')
        self.assertIn('200',status)
        self.assertIn(b'href="/style.css"',body)
        directives={p.strip().split()[0]:p.strip().split()[1:] for p in headers['Content-Security-Policy'].split(';') if p.strip()}
        self.assertIn("'self'",directives['style-src'])
        self.assertIn("'unsafe-inline'",directives['style-src'])
        self.assertEqual(directives['script-src'],["'self'"])

    def test_css_response_matches_stylesheet(self):
        status,headers,body=self.response('/style.css')
        self.assertIn('200',status)
        self.assertTrue(headers['Content-Type'].startswith('text/css'))
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(body,(Path(__file__).resolve().parents[1]/'web/style.css').read_bytes())
        self.assertIn(b'.sidebar',body)
