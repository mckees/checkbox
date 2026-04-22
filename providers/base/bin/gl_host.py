#!/usr/bin/env python3
# This file is part of Checkbox.
#
# Copyright 2025 Canonical Ltd.
# Written by:
#   Shane McKee <shane.mckee@canonical.com>
#
# Checkbox is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3,
# as published by the Free Software Foundation.
#
# Checkbox is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Checkbox.  If not, see <http://www.gnu.org/licenses/>.

"""
Host OpenGL helper for Checkbox.

Subcommands:
  resource          Emit a resource record if a GPU is available via host
                    OpenGL drivers (used by depends:
                    graphics/gl_classic_gpu_avail).
  validate-install  Emit a resource record if the host EGL library is
                    installed (used by depends: graphics/gl_classic_gl_avail).
  run-test ARGS...  Run the opengl-cts glcts binary with host EGL/Mesa,
                    forwarding all remaining arguments to the test.
"""

import logging
import os
import subprocess
import sys
import sysconfig


# PCI vendor IDs found in /sys/class/drm/<card>/device/vendor
_DRM_KNOWN_VENDORS = {"0x8086", "0x1002", "0x10de"}


def get_arch_triple():
    triple = sysconfig.get_config_var("MULTIARCH")
    if triple is None:
        raise RuntimeError("could not determine multiarch triple")
    return triple


def _has_drm_gpu():
    """Return True if a known physical GPU is present in DRM sysfs."""
    try:
        entries = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        return False
    for entry in entries:
        if not entry.startswith("card") or not entry[4:].isdigit():
            continue
        vendor_path = "/sys/class/drm/{}/device/vendor".format(entry)
        try:
            with open(vendor_path) as f:
                vid = f.read().strip().lower()
            if vid in _DRM_KNOWN_VENDORS:
                return True
        except OSError:
            continue
    return False


def cmd_resource():
    if _has_drm_gpu():
        print("gpu_available: True")
        return 0
    logging.error("No known GPU found in DRM sysfs (/sys/class/drm)")
    return 1


def cmd_validate_install():
    arch_triple = get_arch_triple()
    host_egl = "/usr/lib/{}/libEGL.so.1".format(arch_triple)
    if os.path.isfile(host_egl):
        print("egl_available: True")
        return 0
    logging.error("Host EGL library not found at %s", host_egl)
    logging.error(
        "Install libegl-mesa0 or equivalent before running host OpenGL tests"
    )
    return 1


def cmd_run_test(test_args):
    snap = "/snap/opengl-cts/current"
    result = subprocess.run(
        ["{}/test".format(snap), "--no-confinement"] + test_args,
        env=dict(os.environ, SNAP=snap),
    )
    return result.returncode


def main():
    logging.basicConfig(
        format="%(levelname)s: %(message)s", level=logging.INFO
    )
    if len(sys.argv) < 2:
        logging.error(
            "Usage: gl_host.py {resource,validate-install,run-test} [args...]"
        )
        return 1
    command = sys.argv[1]
    try:
        if command == "resource":
            return cmd_resource()
        elif command == "validate-install":
            return cmd_validate_install()
        elif command == "run-test":
            return cmd_run_test(sys.argv[2:])
        else:
            logging.error("Unknown command: %s", command)
            return 1
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
