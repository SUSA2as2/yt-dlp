from __future__ import annotations
import contextlib
import json
import os
import random
import re
import string
import tempfile


class TempFileWrapper:
    """
    Wrapper for NamedTemporaryFile, auto closes file after io and deletes file upon wrapper object gc

    @param {str | bytes | None} content: content to write to file upon creation
    @param {bool} text: whether to open file in text mode
    @param {str} encoding: encoding to use for text mode
    @param {str | None} suffix: suffix for filename of temporary file
    """

    def __init__(self, content: str | bytes | None = None, text: bool = True,
                 encoding='utf-8', suffix: str | None = None):
        self.encoding = None if not text else encoding
        self.text = text
        self._file = tempfile.NamedTemporaryFile('w' if text else 'wb', encoding=self.encoding,
                                                 suffix=suffix, delete=False)
        if content:
            self._file.write(content)
        self._file.close()

    @property
    def name(self):
        return self._file.name

    @contextlib.contextmanager
    def opened_file(self, mode, *, seek=None, seek_whence=0):
        mode = mode if (self.text or 'b' in mode) else mode + 'b'
        with open(self._file.name, mode, encoding=self.encoding) as f:
            if seek is not None:
                self._file.seek(seek, seek_whence)
            yield f

    def write(self, s, seek=None, seek_whence=0):
        """re-open file in write mode and write, optionally seek to position first"""
        with self.opened_file('w', seek=seek, seek_whence=seek_whence) as f:
            return f.write(s)

    def append_write(self, s, seek=None, seek_whence=0):
        """re-open file in append mode and write, optionally seek to position first"""
        with self.opened_file('a', seek=seek, seek_whence=seek_whence) as f:
            return f.write(s)

    def read(self, n=-1, seek=None, seek_whence=0):
        """re-open file and read, optionally seek to position first"""
        with self.opened_file('r', seek=seek, seek_whence=seek_whence) as f:
            return f.read(n)

    def cleanup(self):
        with contextlib.suppress(OSError):
            os.remove(self._file.name)

    def __del__(self):
        self.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()


def random_string(length: int = 10) -> str:
    return ''.join(random.choices(string.ascii_letters, k=length))


def override_navigator_js(user_agent: str) -> str:
    return '\n'.join([
        'Object.defineProperty(navigator, "%s", { value: %s, configurable: true });' % (k, json.dumps(v))
        for k, v in {
            'userAgent': user_agent,
            'language': 'en-US',
            'languages': ['en-US'],
            'webdriver': False,
            'cookieEnabled': True,
            'appCodeName': user_agent.split('/', maxsplit=1)[0],
            'appName': 'Netscape',
            'appVersion': user_agent.split('/', maxsplit=1)[-1],
            'platform': 'Win32',
            'product': 'Gecko',
            'productSub': '20030107',
            'vendor': 'Google Inc.',
            'vendorSub': '',
            'onLine': True,
        }.items()
    ])


def extract_script_tags(html: str) -> tuple[str, list[str]]:
    script_indicies = []
    inline_scripts = []

    for match_start in re.finditer(r'<script[^>]*>', html, re.DOTALL):
        end = html.find('</script>', match_start.end())
        if end > match_start.end():
            script_indicies.append((match_start.start(), end + len('</script>')))
            inline_scripts.append(html[match_start.end():end])

    for start, end in script_indicies:
        html = html[:start] + html[end:]

    return html, inline_scripts
