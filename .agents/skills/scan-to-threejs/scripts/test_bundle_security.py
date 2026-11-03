"""Small, bounded regression cases for the bundle trust boundary.

All fixtures are generated locally in temporary directories. No network access,
real scans, large allocations, or external-file modifications are needed.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np
from PIL import Image

from android_synthesize_room import read_ply, synthesize, write_ply
from bundle_io import (MAX_DEPTH_PIXELS, MAX_PLY_VERTICES, MAX_TOTAL_DEPTH_PIXELS,
                       bounded_inflate, bundle_path, check_points, checked_shape,
                       depth_budget, dimensions, intrinsics_matrix, load_depth_frame,
                       output_path, pose_matrix, read_bytes, read_json, room_index, write_json)
from reconstruct_bundle import bundle_image

SCRIPTS = Path(__file__).resolve().parent


def deflate(data):
    encoder = zlib.compressobj(wbits=-15)
    return encoder.compress(data) + encoder.flush()


class BundleSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def frame(self):
        (self.root / 'depth').mkdir(exist_ok=True)
        (self.root / 'depth/frame.f16z').write_bytes(deflate(np.array([1, 2, 3, 4], dtype='<f2').tobytes()))
        return {'width': 2, 'height': 2, 'file': 'depth/frame.f16z',
                'intrinsics': [10, 0, 0, 0, 10, 0, 1, 1, 1],
                'transform': np.eye(4).T.ravel().tolist()}

    def test_room_index_requires_integer_before_reading_scan(self):
        for value in (-1, True, 0.0, '0', '../room', 2**31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                synthesize(str(self.root), value, write_bundle=True)
        self.assertEqual(room_index(0), 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_default_cli_room_is_validated(self):
        write_json(self.root / 'manifest.json', {'rooms': [{'index': '0'}]})
        result = subprocess.run([sys.executable, str(SCRIPTS / 'android_synthesize_room.py'), str(self.root)],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('room index must be', result.stderr)
        self.assertFalse((self.root / 'synth').exists())

    def test_non_object_manifest_has_clean_cli_error(self):
        write_json(self.root / 'manifest.json', [1])
        result = subprocess.run([sys.executable, str(SCRIPTS / 'android_synthesize_room.py'), str(self.root)],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('manifest must be a JSON object', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_json_rejects_overflowing_numbers_when_read(self):
        path = self.root / 'manifest.json'
        path.write_text('{"extra": 1e999}')
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            read_json(path)

    def test_output_preflight_checks_destination_and_parent_types(self):
        (self.root / 'room.json').mkdir()
        with self.assertRaises(ValueError):
            output_path(self.root, 'room.json')
        (self.root / 'mesh').write_text('not a directory')
        with self.assertRaises(ValueError):
            output_path(self.root, 'mesh/room.ply')

    def test_bundle_paths_reject_escape_and_symlink_components(self):
        for value in ('../frame', '/frame', 'depth/../../frame', 'depth\\frame', '', '.'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bundle_path(self.root, value)
        (self.root / 'link').symlink_to(self.root / 'missing', target_is_directory=True)
        with self.assertRaises(ValueError):
            bundle_path(self.root, 'link/frame')
        self.assertEqual(bundle_path(self.root, 'depth/frame'), self.root / 'depth/frame')

    def test_writes_reject_symlink_output_and_parent(self):
        (self.root / 'room.json').symlink_to(self.root / 'missing.json')
        with self.assertRaises(ValueError):
            write_json(self.root / 'room.json', {})
        (self.root / 'parent').symlink_to(self.root / 'missing-parent', target_is_directory=True)
        with self.assertRaises(ValueError):
            write_json(self.root / 'parent/room.json', {})
        self.assertFalse((self.root / 'missing.json').exists())

    def test_atomic_write_preserves_prior_file_on_failure_and_ignores_old_temp(self):
        output = self.root / 'room.json'
        output.write_text('{"old": true}')
        old_temp = self.root / 'room.json.tmp'
        old_temp.write_text('unrelated stale file')
        with self.assertRaises(ValueError):
            write_json(output, {'number': float('nan')})
        self.assertEqual(json.loads(output.read_text()), {'old': True})
        write_json(output, {'new': True})
        self.assertEqual(json.loads(output.read_text()), {'new': True})
        self.assertEqual(old_temp.read_text(), 'unrelated stale file')
        self.assertEqual({p.name for p in self.root.iterdir()}, {'room.json', 'room.json.tmp'})

    def test_backup_preflight_rejects_link_before_reconstruction(self):
        write_json(self.root / 'manifest.json', {'rooms': [{'index': 0}]})
        (self.root / 'rooms').mkdir()
        (self.root / 'rooms/room-0.json').symlink_to(self.root / 'missing.json')
        with mock.patch('android_synthesize_room.load_frame') as load:
            with self.assertRaises(ValueError):
                synthesize(str(self.root), 0, write_bundle=True)
            load.assert_not_called()
        self.assertFalse((self.root / 'mesh').exists())

    def test_bounded_file_read_rejects_size_and_non_regular_file(self):
        file = self.root / 'input'
        file.write_bytes(b'abcd')
        self.assertEqual(read_bytes(file, 4), b'abcd')
        with self.assertRaises(ValueError):
            read_bytes(file, 3)
        with self.assertRaises((ValueError, OSError)):
            read_bytes(self.root, 100)

    def test_depth_decoding_preserves_layout_and_confidence(self):
        frame = self.frame()
        frame['confidence'] = 'depth/frame.conf'
        (self.root / frame['confidence']).write_bytes(bytes([0, 1, 2, 2]))
        depth, k, pose, sure = load_depth_frame(self.root, frame)
        np.testing.assert_array_equal(depth, [[0, 2], [3, 4]])
        np.testing.assert_array_equal(sure, [[False, False], [True, True]])
        np.testing.assert_array_equal(pose, np.eye(4))
        self.assertEqual(k[0, 0], 10)

    def test_deflate_requires_exact_length_and_complete_single_stream(self):
        raw = deflate(b'12345678')
        self.assertEqual(bounded_inflate(raw, 8), b'12345678')
        for data, expected in ((raw, 4), (raw, 9), (raw[:-1], 8), (raw + b'trailing', 8)):
            with self.subTest(expected=expected, length=len(data)), self.assertRaises(ValueError):
                bounded_inflate(data, expected)

    def test_depth_and_confidence_paths_stay_inside_bundle(self):
        frame = self.frame()
        for key in ('file', 'confidence'):
            changed = dict(frame, **{key: '../not-a-scan-frame'})
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_depth_frame(self.root, changed)

    def test_depth_dimensions_and_total_budget_are_checked_before_read(self):
        for w, h in ((0, 2), (-1, 2), (True, 2), (2.0, 2), (MAX_DEPTH_PIXELS + 1, 1)):
            with self.subTest(w=w, h=h), self.assertRaises(ValueError):
                dimensions({'width': w, 'height': h})
        count = MAX_TOTAL_DEPTH_PIXELS // MAX_DEPTH_PIXELS + 1
        with self.assertRaises(ValueError):
            depth_budget([{'width': MAX_DEPTH_PIXELS, 'height': 1}] * count)

    def test_confidence_length_and_values_are_validated(self):
        frame = self.frame()
        frame['confidence'] = 'depth/frame.conf'
        for data in (bytes([2]), bytes([2, 2, 2, 3])):
            (self.root / frame['confidence']).write_bytes(data)
            with self.assertRaises(ValueError):
                load_depth_frame(self.root, frame)

    def test_camera_metadata_requires_finite_rigid_pose_and_intrinsics(self):
        frame = self.frame()
        for value in (float('nan'), float('inf'), 2):
            pose = list(frame['transform'])
            pose[0] = value
            with self.subTest(pose=value), self.assertRaises(ValueError):
                pose_matrix(pose)
        for value in (0, float('nan'), 1e100):
            intrinsics = list(frame['intrinsics'])
            intrinsics[0] = value
            with self.subTest(focal=value), self.assertRaises(ValueError):
                intrinsics_matrix(intrinsics, 2, 2)

    def test_scene_extent_and_raster_limits_reject_before_allocation(self):
        with self.assertRaises(ValueError):
            checked_shape((100000, 100000))
        with self.assertRaises(ValueError):
            check_points([[0, 0, 0], [201, 1, 1]])
        self.assertEqual(checked_shape((100, 100)), (100, 100))

    def test_photo_reader_uses_contained_path(self):
        Image.new('RGB', (2, 3)).save(self.root / 'photo.png')
        with bundle_image(self.root, 'photo.png') as photo:
            self.assertEqual(photo.size, (2, 3))
        with self.assertRaises(ValueError):
            bundle_image(self.root, '../photo.png')

    def test_ply_roundtrip_and_invalid_metadata(self):
        vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
        normals = np.tile([0, 0, 1.0], (3, 1))
        faces = np.array([[0, 1, 2]])
        path = self.root / 'mesh.ply'
        write_ply(path, vertices, normals, np.zeros((3, 3), np.uint8), faces, np.array([1]))
        v, n, f = read_ply(path)
        np.testing.assert_array_equal(v, vertices)
        np.testing.assert_array_equal(n, normals)
        np.testing.assert_array_equal(f, faces)
        header = ('ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\n'
                  'property float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n')
        valid = header + '0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n'
        path.write_text(valid)
        np.testing.assert_array_equal(read_ply(path)[2], faces)
        for changed in (valid.replace('element vertex 3', f'element vertex {MAX_PLY_VERTICES + 1}'),
                        valid.replace('3 0 1 2', '3 0 1 9'), valid.replace('0 0 0\n', 'nan 0 0\n'),
                        header, valid.replace('element vertex 3', 'element vertex -1'),
                        valid.replace('3 0 1 2', '3 0 1 4294967298'),
                        valid.replace('property list uchar int', 'property list uchar uint').replace('3 0 1 2', '3 0 1 -1')):
            path.write_text(changed)
            with self.assertRaises(ValueError):
                read_ply(path)


if __name__ == '__main__':
    unittest.main()
