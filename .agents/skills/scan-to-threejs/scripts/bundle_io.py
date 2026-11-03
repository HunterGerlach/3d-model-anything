"""Bounded local I/O for scan bundles, whose metadata is untrusted input.

These limits are intended for room scans, not arbitrary 3D datasets. They bound
individual inputs and derived allocations; they are not a process sandbox.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zlib

import numpy as np

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10000
MAX_DEPTH_PIXELS = 1024 * 1024
MAX_TOTAL_DEPTH_PIXELS = 64 * 1024 * 1024
MAX_PHOTO_PIXELS = 50 * 1024 * 1024
MAX_RASTER_CELLS = 4 * 1024 * 1024
MAX_PLY_BYTES = 128 * 1024 * 1024
MAX_PLY_VERTICES = 2_000_000
MAX_PLY_FACES = 4_000_000
MAX_COORDINATE = 10_000.0
MAX_SCENE_SPAN = 200.0


def room_index(value):
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise ValueError('room index must be a non-negative 32-bit integer')
    return value


def bundle_path(root, relative):
    """Resolve a bundle-relative path without accepting symlinks or traversal.

    The caller chooses the root. Every component beneath it, including missing
    output paths and dangling links, is checked before use.
    """
    root = Path(root).resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f'{root}: bundle root is not a directory')
    if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
        raise ValueError('bundle path must be a non-empty relative POSIX path')
    rel = PurePosixPath(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError(f'path escapes bundle: {relative!r}')
    current = root
    for index, part in enumerate(rel.parts):
        current = current / part
        if current.is_symlink():
            raise ValueError(f'symlinks are not allowed in bundle paths: {relative!r}')
        if index < len(rel.parts) - 1 and current.exists() and not current.is_dir():
            raise ValueError(f'{current}: path parent is not a directory')
    if not current.resolve().is_relative_to(root):
        raise ValueError(f'path escapes bundle: {relative!r}')
    return current


def output_path(root, relative):
    path = bundle_path(root, relative)
    if path.exists() and not path.is_file():
        raise ValueError(f'{path}: output is not a regular file')
    return path


def read_bytes(path, limit):
    """Read a regular file without following its final symlink or blocking on a FIFO."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'{path}: expected a regular file')
        if info.st_size > limit:
            raise ValueError(f'{path}: exceeds {limit:,}-byte input limit')
        data = fh.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f'{path}: exceeds {limit:,}-byte input limit')
        return data


def read_json(path, default=None):
    def invalid_constant(value):
        raise ValueError(f'{path}: non-finite JSON number {value}')

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            invalid_constant(value)
        return number

    try:
        raw = read_bytes(path, MAX_JSON_BYTES)
    except FileNotFoundError:
        return default
    try:
        return json.loads(raw, parse_constant=invalid_constant, parse_float=finite_float)
    except RecursionError as error:
        raise ValueError(f'{path}: JSON nesting is too deep') from error


def records(value, label):
    if not isinstance(value, list) or len(value) > MAX_RECORDS or any(not isinstance(v, dict) for v in value):
        raise ValueError(f'{label}: expected at most {MAX_RECORDS:,} object records')
    return value


@contextmanager
def atomic_path(path):
    """Use a new private temporary file; never open a predictable .tmp filename."""
    path = Path(path)
    # Revalidate existing parents as well as the final file before creation.
    absolute = Path(os.path.abspath(path))
    path = bundle_path(absolute.anchor, absolute.relative_to(absolute.anchor).as_posix())
    if path.exists() and not path.is_file():
        raise ValueError(f'{path}: output is not a regular file')
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.stem}.', suffix=path.suffix, dir=path.parent)
    os.close(fd)
    try:
        yield tmp
        if path.is_symlink():
            raise ValueError(f'{path}: output became a symlink')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def atomic_output(path, mode='wb'):
    with atomic_path(path) as tmp:
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(tmp, flags)
        with os.fdopen(fd, mode) as fh:
            yield fh


def write_json(path, data):
    with atomic_output(path, 'w') as fh:
        json.dump(data, fh, indent=1, allow_nan=False)


def dimensions(record, limit=MAX_DEPTH_PIXELS):
    w, h = record['width'], record['height']
    if type(w) is not int or type(h) is not int or w < 1 or h < 1 or w * h > limit:
        raise ValueError(f'image dimensions must be positive integers totaling at most {limit:,} pixels')
    return w, h


def depth_budget(frames):
    records(frames, 'depth frames')
    pixels = sum(math.prod(dimensions(frame)) for frame in frames)
    if pixels > MAX_TOTAL_DEPTH_PIXELS:
        raise ValueError(f'depth frames exceed the {MAX_TOTAL_DEPTH_PIXELS:,}-pixel working-set limit; use fewer frames')


def pose_matrix(values):
    matrix = np.asarray(values, dtype=np.float64).reshape(4, 4).T
    if not np.isfinite(matrix).all() or np.abs(matrix[:3, 3]).max() > MAX_COORDINATE:
        raise ValueError('camera transform must contain finite, bounded coordinates')
    rotation = matrix[:3, :3]
    if (not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-5)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=0.02)
            or not 0.95 <= np.linalg.det(rotation) <= 1.05):
        raise ValueError('camera transform must be a rigid pose')
    return matrix


def intrinsics_matrix(values, w, h):
    matrix = np.asarray(values, dtype=np.float64).reshape(3, 3).T
    if (not np.isfinite(matrix).all() or not np.allclose(matrix[2], [0, 0, 1], atol=1e-5)
            or not 1 <= matrix[0, 0] <= 100_000 or not 1 <= matrix[1, 1] <= 100_000
            or abs(matrix[0, 2]) > 4 * w or abs(matrix[1, 2]) > 4 * h):
        raise ValueError('camera intrinsics must be finite with valid focal lengths and principal point')
    return matrix


def bounded_inflate(raw, expected):
    decoder = zlib.decompressobj(-15)
    try:
        data = decoder.decompress(raw, expected + 1)
    except zlib.error as error:
        raise ValueError('invalid raw DEFLATE depth frame') from error
    # No unbounded flush: reaching EOF and the exact length is required.
    if len(data) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('depth frame does not match its declared byte length or stream boundary')
    return data


def load_depth_frame(bundle, frame, min_conf=1):
    w, h = dimensions(frame)
    expected = w * h * 2
    k = intrinsics_matrix(frame['intrinsics'], w, h)
    pose = pose_matrix(frame['transform'])
    path = bundle_path(bundle, frame['file'])
    # DEFLATE overhead is small even for incompressible data; permit a generous
    # 64 KiB overhead but never read an unlimited compressed stream.
    raw = read_bytes(path, expected + 65536)
    if path.suffix == '.f16z':
        raw = bounded_inflate(raw, expected)
    elif len(raw) != expected:
        raise ValueError('depth frame does not match its declared byte length')
    depth = np.frombuffer(raw, dtype='<f2').astype(np.float32).reshape(h, w)
    depth[~np.isfinite(depth) | (depth < 0)] = 0
    sure = np.ones((h, w), bool)
    if frame.get('confidence'):
        confidence_path = bundle_path(bundle, frame['confidence'])
        if confidence_path.exists():
            raw_confidence = read_bytes(confidence_path, w * h)
            if len(raw_confidence) != w * h:
                raise ValueError('confidence frame does not match its declared byte length')
            confidence = np.frombuffer(raw_confidence, dtype=np.uint8).reshape(h, w)
            if (confidence > 2).any():
                raise ValueError('confidence values must be 0, 1 or 2')
            depth[confidence < min_conf] = 0
            sure = confidence >= 2
    return depth, k, pose, sure


def checked_shape(shape, label='raster', limit=MAX_RASTER_CELLS):
    values = tuple(int(v) for v in shape)
    if any(v < 1 or v > limit for v in values) or math.prod(values) > limit:
        raise ValueError(f'{label} exceeds the {limit:,}-cell limit; check scan poses or use a coarser cell size')
    return values


def check_points(points, label='scan'):
    points = np.asarray(points)
    if not len(points) or not np.isfinite(points).all() or np.abs(points).max() > MAX_COORDINATE:
        raise ValueError(f'{label}: points must be non-empty, finite and bounded')
    if np.ptp(points, axis=0).max() > MAX_SCENE_SPAN:
        raise ValueError(f'{label}: extent exceeds the {MAX_SCENE_SPAN:g} m room-scan limit')
    return points
