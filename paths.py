"""Separate immutable application files from portable persistent data."""
import os
from pathlib import Path
CODE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('THUNDER_DATA_DIR', CODE)).resolve()

def initialize(directory=None):
    from machine_settings import atomic_json
    directory = Path(directory) if directory is not None else DATA
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = directory / '.runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    (runtime / 'known_hosts').touch(mode=0o600, exist_ok=True)
    if not (directory / 'config.json').exists():
        atomic_json(directory / 'config.json', dict(poll_seconds=15, idle_seconds=600, listen_port=8765, mode='observe', hosts=[]))
