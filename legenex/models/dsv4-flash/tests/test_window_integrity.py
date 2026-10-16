"""Integrity refusals use real temporary files, including unsafe path fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('window_preflight', Path(__file__).resolve().parents[1] / 'tools/window_preflight.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.results = []
        for i in range(60):
            name = f'part-{i}.safetensors'
            raw = f'synthetic-checkpoint-{i}'.encode()
            (self.root / name).write_bytes(raw)
            (self.root / name).chmod(0o640)
            self.results.append({'file': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})

    def pin(self):
        raw = json.dumps({'target_rev': P.REVISION, 'files': 60, 'verified': 60, 'results': self.results}).encode()
        (self.root / 'VERIFY.json').write_bytes(raw)
        return patch.object(P, 'MANIFEST_SHA', hashlib.sha256(raw).hexdigest())

    def test_rehashes_the_entire_checkpoint(self):
        with self.pin():
            self.assertEqual(P.verify_weights(self.root)['files_rehashed'], 60)

    def test_same_size_corruption_is_refused(self):
        with self.pin():
            path = self.root / self.results[0]['file']
            path.write_bytes(b'x' * path.stat().st_size)
            with self.assertRaisesRegex(ValueError, 'integrity failed'):
                P.verify_weights(self.root)

    def test_manifest_change_is_refused_before_trusting_its_hashes(self):
        with self.pin():
            with (self.root / 'VERIFY.json').open('ab') as out:
                out.write(b' ')
            with self.assertRaisesRegex(ValueError, 'manifest differs'):
                P.verify_weights(self.root)

    def test_symlink_is_refused(self):
        with self.pin():
            path = self.root / self.results[0]['file']
            path.unlink()
            path.symlink_to(self.root / self.results[1]['file'])
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                P.verify_weights(self.root)

    def test_unmanifested_tensor_is_refused(self):
        with self.pin():
            (self.root / 'extra.safetensors').write_bytes(b'unapproved')
            with self.assertRaisesRegex(ValueError, 'unexpected'):
                P.verify_weights(self.root)

    def test_traversal_and_duplicate_paths_are_refused(self):
        self.results[0]['file'] = '../escape'
        with self.pin():
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                P.verify_weights(self.root)
        self.results[0]['file'] = self.results[1]['file']
        with self.pin():
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                P.verify_weights(self.root)


if __name__ == '__main__':
    unittest.main()
