"""Run isolated official emulator checks with an already installed Java/JAR.

Never downloads software, discovers credentials, modifies global settings or
contacts a real Firestore project. The emulator process is always terminated.
"""
import argparse
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

JAR_SHA256 = '9b6498b7f62714d67f48f59b3818883cd682dbcd46b9f59511de81c97bb5166c'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java', type=Path, required=True)
    parser.add_argument('--jar', type=Path, required=True)
    parser.add_argument('--junitxml', type=Path)
    args = parser.parse_args()
    java, jar = args.java.resolve(strict=True), args.jar.resolve(strict=True)
    if hashlib.sha256(jar.read_bytes()).hexdigest() != JAR_SHA256:
        raise ValueError('Official pinned emulator JAR checksum differs')
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1]
    endpoint = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='assistant-firestore-emulator-') as directory:
        work = Path(directory)
        with (work / 'emulator.log').open('w+') as log:
            process = subprocess.Popen([str(java), '-Xmx512m', '-jar', str(jar), '--host', '127.0.0.1', '--port', str(port),
                '--project_id', 'demo-expense-storage', '--single_project_mode', 'true'], cwd=work,
                env={'PATH': str(java.parent) + ':/usr/bin:/bin', 'TMPDIR': directory}, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(timeout=1, trust_env=False) as client:
                    for _ in range(300):
                        if process.poll() is not None:
                            log.seek(0); raise RuntimeError('Emulator exited: ' + log.read()[-4000:])
                        try:
                            response = client.get(endpoint + '/v1/projects/demo-expense-storage/databases/(default)/documents')
                            if response.status_code in {200, 400, 404}:break
                        except httpx.TransportError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('Loopback emulator did not start within 30 seconds')
                cmd = [sys.executable, '-m', 'pytest', 'tests/test_storage_emulator.py', '-p', 'no:cacheprovider', '--tb=short']
                if args.junitxml:cmd.append('--junitxml=' + str(args.junitxml.resolve()))
                env = {k: v for k, v in os.environ.items() if not any(t in k.upper() for t in ['TOKEN', 'SECRET', 'CREDENTIAL', 'API_KEY', 'GOOGLE_', 'CLOUDSDK_'])}
                env.update(ASSISTANT_FIRESTORE_EMULATOR=endpoint, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(root / 'src') + ':' + str(root / 'tests'))
                result = subprocess.run(cmd, cwd=root, env=env, timeout=180)
                if result.returncode:raise SystemExit(result.returncode)
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


if __name__ == '__main__':main()
