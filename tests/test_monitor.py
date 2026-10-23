import json
from pathlib import Path
import tempfile
import unittest
from monitor import load_config

class ConfigTests(unittest.TestCase):
    def test_invalid_rules_rejected(self):
        base=dict(poll_seconds=15,idle_seconds=600,hosts=[dict(id='x',address='example',credentials_file='credentials.txt')])
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'config.json'
            for changes in [dict(mode='wrong'),dict(poll_seconds=0),dict(idle_seconds=15),dict(idle_seconds=float('nan'))]:
                p.write_text(json.dumps({**base,**changes}))
                with self.assertRaises(ValueError):load_config(p)
            p.write_text(json.dumps(base))
            self.assertEqual(load_config(p)['idle_seconds'],600)
