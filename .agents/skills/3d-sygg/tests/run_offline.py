#!/usr/bin/env python3
"""Run the suite with external HTTP and real credential/tunnel commands blocked."""
import json
import subprocess
import sys
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


def main():
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    real_open = urllib.request.urlopen
    real_run = subprocess.run
    real_popen = subprocess.Popen
    blocked = []

    def guarded_open(request, *args, **kwargs):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        if urllib.parse.urlparse(url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
            blocked.append('external HTTP')
            raise AssertionError('Offline validation attempted external HTTP')
        return real_open(request, *args, **kwargs)

    def check_command(args):
        name = Path(args[0]).name if isinstance(args, (list, tuple)) else str(args).split()[0]
        if name in {'cloudflared', 'security', 'curl', 'wget'}:
            blocked.append(name)
            raise AssertionError('Offline validation attempted a live credential or network command')

    def guarded_run(args, *a, **kw):
        check_command(args)
        return real_run(args, *a, **kw)

    def guarded_popen(args, *a, **kw):
        check_command(args)
        return real_popen(args, *a, **kw)

    with mock.patch.object(urllib.request, 'urlopen', side_effect=guarded_open), \
         mock.patch.object(subprocess, 'run', side_effect=guarded_run), \
         mock.patch.object(subprocess, 'Popen', side_effect=guarded_popen):
        suite = unittest.defaultTestLoader.discover(str(root), pattern='test_*.py')
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
                      'skipped':len(result.skipped),'blocked_external_attempts':blocked}, ensure_ascii=False))
    return 0 if result.wasSuccessful() and not blocked else 1


if __name__ == '__main__':
    raise SystemExit(main())
