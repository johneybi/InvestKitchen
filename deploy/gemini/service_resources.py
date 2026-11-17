"""Scoped cgroup-v1 reclaim preferences; no global VM or authentication changes.

This root-only operator helper is for legacy systemd hosts. It does not disable
swap system-wide or promise a resident-memory reservation. Existing hard limits
are checked and preserved. An operator-owned private state directory is required.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess

PROFILES = {
    'investkitchen-gemini-mcp.service': 128 * 1024 * 1024,
    'investkitchen-gemini-auth.service': 96 * 1024 * 1024,
}
KEYS = ('memory.swappiness', 'memory.soft_limit_in_bytes')


class ResourceController:
    def group(self, unit):
        if unit not in PROFILES:
            raise ValueError('Unexpected service')
        output = subprocess.check_output(
            ['systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ControlGroup'],
            text=True, timeout=10)
        values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        pid, group = values['MainPID'], values['ControlGroup']
        if not pid.isdigit() or int(pid) <= 0 or group != '/system.slice/' + unit:
            raise ValueError('Service is not in its expected cgroup')
        memberships = [line.split(':', 2) for line in Path('/proc', pid, 'cgroup').read_text().splitlines()]
        if not any('memory' in row[1].split(',') and row[2] == group for row in memberships):
            raise ValueError('Process memory cgroup differs from service')
        path = Path('/sys/fs/cgroup/memory') / group.lstrip('/')
        if path.is_symlink():
            raise ValueError('Unexpected cgroup link')
        return path

    def read(self, unit):
        group = self.group(unit)
        values = {}
        for key in (*KEYS, 'memory.limit_in_bytes'):
            path = group / key
            if path.is_symlink():
                raise ValueError('Unexpected resource link')
            value = path.read_text().strip()
            if not value.isdigit():
                raise ValueError('Unexpected resource value')
            values[key] = value
        if int(values['memory.limit_in_bytes']) < PROFILES[unit]:
            raise ValueError('Reclaim preference exceeds existing hard limit')
        return values

    def write(self, unit, key, value):
        if key not in KEYS or not str(value).isdigit():
            raise ValueError('Unexpected resource setting')
        path = self.group(unit) / key
        if path.is_symlink():
            raise ValueError('Unexpected resource link')
        path.write_text(str(value))
        if path.read_text().strip() != str(value):
            raise ValueError('Resource setting did not persist')


def desired(unit):
    return {'memory.swappiness': '0', 'memory.soft_limit_in_bytes': str(PROFILES[unit])}


def validate_baseline(baseline):
    if not isinstance(baseline, dict) or set(baseline) != set(PROFILES):
        raise ValueError('Unexpected resource baseline')
    for unit, values in baseline.items():
        if (not isinstance(values, dict) or set(values) != {*KEYS, 'memory.limit_in_bytes'}
                or any(not isinstance(v, str) or not v.isdigit() for v in values.values())
                or int(values['memory.swappiness']) > 100
                or int(values['memory.limit_in_bytes']) < PROFILES[unit]):
            raise ValueError('Invalid resource baseline')


def transition(controller, baseline, *, restore=False):
    """Validate every target before any mutation; undo partial writes on failure."""
    validate_baseline(baseline)
    current = {unit: controller.read(unit) for unit in PROFILES}
    for unit, values in current.items():
        if values['memory.limit_in_bytes'] != baseline[unit]['memory.limit_in_bytes']:
            raise ValueError('Existing hard memory limit changed')
        for key in KEYS:
            if values[key] not in (baseline[unit][key], desired(unit)[key]):
                raise ValueError('Resource preference drifted')
    touched = []
    try:
        for unit in PROFILES:
            target = baseline[unit] if restore else desired(unit)
            for key in KEYS:
                if current[unit][key] != target[key]:
                    touched.append((unit, key, current[unit][key]))
                    controller.write(unit, key, target[key])
    except Exception:
        for unit, key, value in reversed(touched):
            controller.write(unit, key, value)
        raise


def private_directory(path):
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('A root-owned private state directory is required')


def read_state(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError('A protected resource baseline is required')
        baseline = json.load(handle)
    validate_baseline(baseline)
    return baseline


def run(action, state_path):
    if os.geteuid() != 0:
        raise ValueError('Root is required for this scoped resource transition')
    private_directory(state_path.parent)
    lock = os.open(state_path.parent / '.resource-lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'r+') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Unexpected transition lock')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        controller = ResourceController()
        if state_path.exists() or state_path.is_symlink():
            baseline = read_state(state_path)
        elif action == 'apply':
            baseline = {unit: controller.read(unit) for unit in PROFILES}
            validate_baseline(baseline)
            fd = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump(baseline, target, sort_keys=True)
                target.flush()
                os.fsync(target.fileno())
            directory_fd = os.open(state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        else:
            raise ValueError('Resource baseline is missing')
        transition(controller, baseline, restore=action == 'restore')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'apply', 'restore'))
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'inspect':
            print(json.dumps({unit: ResourceController().read(unit) for unit in PROFILES}, sort_keys=True))
        else:
            if args.state is None:
                raise ValueError('Protected state is required')
            run(args.action, args.state)
            print('SCOPED RESOURCE ' + args.action.upper() + ' PASS')
    except Exception:
        # Never print subprocess output, local paths or arbitrary exception text.
        print('SCOPED RESOURCE STOPPED')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
