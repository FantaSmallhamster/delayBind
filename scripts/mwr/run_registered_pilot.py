"""Execute a frozen development grid with credentials kept in process memory."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.cli import _load_env_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration', type=Path, required=True)
    args = parser.parse_args()
    registration = json.loads(args.registration.read_text())
    for path, expected in registration['code_sha256'].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise SystemExit('Registered core changed: ' + path)
    for condition in registration['configurations']:
        if hashlib.sha256(Path(condition['path']).read_bytes()).hexdigest() != condition['sha256']:
            raise SystemExit('Registered configuration changed: ' + condition['path'])
    _load_env_file()
    if not os.environ.get('MODEL_API_KEY'):
        os.environ['MODEL_API_KEY'] = getpass.getpass('API key (hidden, memory only): ')
    try:
        for condition in registration['configurations']:
            print('Starting registered capacity:', condition['live_state_tokens'], flush=True)
            result = subprocess.run([sys.executable, 'scripts/ultimate/run_experiment.py',
                '--config', condition['path'], '--expected-samples', str(len(condition['sample_ids']))], check=False)
            if result.returncode:
                raise SystemExit(result.returncode)
    finally:
        os.environ.pop('MODEL_API_KEY', None)


if __name__ == '__main__':
    main()
