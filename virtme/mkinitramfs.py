# -*- mode: python -*-
# virtme-mkinitramfs: Generate an initramfs image for virtme
# Copyright © 2014 Andy Lutomirski
# Licensed under the GPLv2, which is available in the virtme distribution
# as a file called LICENSE with SHA-256 hash:
# 8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643

from typing import List, Dict, Optional

import shutil
import io
import os.path
import shlex
import itertools
from . import cpiowriter
from . import util
from elftools.elf.elffile import ELFFile
from elftools.elf.dynamic import DynamicSection

ld_paths = ['/lib', '/lib64', '/usr/lib', '/usr/lib64']

def is_dt_needed(tag):
    return tag.entry.d_tag == 'DT_NEEDED'

def find_library_path(needed):
    for ld_path in ld_paths:
        ld_path = os.path.join(ld_path, needed)
        if os.path.isfile(ld_path):
            return (ld_path, ld_path.lstrip('/'))

def find_needed_paths(executable):
    with open(executable, 'rb') as f:
        elffile = ELFFile(f)

        dynamic = elffile.get_section_by_name('.dynamic')
        if not dynamic:
            return []

        tags = dynamic.iter_tags()
        neededs = [tag.needed for tag in tags if is_dt_needed(tag)]

        return [find_library_path(needed) for needed in neededs]

def make_base_layout(cw):
    for dir in (b'lib', b'bin', b'var', b'etc', b'newroot', b'dev', b'proc',
                b'tmproot', b'run_virtme', b'run_virtme/data', b'run_virtme/guesttools'):
        cw.mkdir(dir, 0o755)

    cw.symlink(b'bin', b'sbin')
    cw.symlink(b'lib', b'lib64')

def make_dev_nodes(cw):
    cw.mkchardev(b'dev/null', (1, 3), mode=0o666)
    cw.mkchardev(b'dev/kmsg', (1, 11), mode=0o666)
    cw.mkchardev(b'dev/console', (5, 1), mode=0o660)

def copy(cw, src, dst, mode):
    with open(src, 'rb') as f:
        cw.write_file(name=dst, body=f, mode=mode)

def install_busybox(cw, config):
    copy(cw, config.busybox, b'bin/busybox', 0o755)

    for (needed_path, target_path) in find_needed_paths(config.busybox):
        copy(cw, needed_path, target_path.encode('ascii'), 0o755)

    for tool in ('sh', 'mount', 'umount', 'switch_root', 'sleep', 'mkdir',
                 'mknod', 'insmod', 'cp', 'cat'):
        cw.symlink(b'busybox', ('bin/%s' % tool).encode('ascii'))

    cw.mkdir(b'bin/real_progs', mode=0o755)

def install_modprobe(cw):
    cw.write_file(name=b'bin/modprobe', body=b'\n'.join([
        b'#!/bin/sh',
        b'echo "virtme: initramfs does not have module $3" >/dev/console',
        b'exit 1',
    ]), mode=0o755)

_LOGFUNC = """log() {
    if [[ -e /dev/kmsg ]]; then
	echo "<6>virtme initramfs: $*" >/dev/kmsg
    else
	echo "virtme initramfs: $*"
    fi
}
"""

def install_modules(cw, modfiles):
    cw.mkdir(b'modules', 0o755)
    paths = []
    for mod in modfiles:
        with open(mod, 'rb') as f:
            modpath = 'modules/' + os.path.basename(mod)
            paths.append(modpath)
            cw.write_file(name=modpath.encode('ascii'),
                          body=f, mode=0o644)

    script = _LOGFUNC + '\n'.join('log \'loading %s...\'; insmod %s' %
                       (os.path.basename(p), shlex.quote(p)) for p in paths)
    cw.write_file(name=b'modules/load_all.sh',
                  body=script.encode('ascii'), mode=0o644)

_INIT = """#!/bin/sh

{logfunc}

source /modules/load_all.sh

log 'mounting hostfs...'

if ! /bin/mount -n -t 9p -o {access},version=9p2000.L,trans=virtio,access=any /dev/root /newroot/; then
  echo "Failed to mount real root.  We are stuck."
  sleep 5
  exit 1
fi

# Can we actually use /newroot/ as root?
if ! mount -t proc -o nosuid,noexec,nodev proc /newroot/proc 2>/dev/null; then
  # QEMU 1.5 and below have a bug in virtfs that prevents mounting
  # anything on top of a virtfs mount.
  log "your host's virtfs is broken -- using a fallback tmpfs"
  need_fallback_tmpfs=1
else
  umount /newroot/proc  # Don't leave garbage behind
fi

if ! [[ -d /newroot/run ]]; then
  log "your guest's root does not have /run -- using a fallback tmpfs"
  need_fallback_tmpfs=1
fi

if [[ "$need_fallback_tmpfs" != "" ]]; then
  mount --move /newroot /tmproot
  mount -t tmpfs root_workaround /newroot/
  cd tmproot
  mkdir /newroot/proc /newroot/sys /newroot/dev /newroot/run /newroot/tmp
  for i in *; do
    if [[ -d "$i" && \! -d "/newroot/$i" ]]; then
      mkdir /newroot/"$i"
      mount --bind "$i" /newroot/"$i"
    fi
  done
  mknod /newroot/dev/null c 1 3
  mount -o remount,ro -t tmpfs root_workaround /newroot
  umount -l /tmproot
fi

mount -t tmpfs run /newroot/run
cp -a /run_virtme /newroot/run/virtme

# Find init
mount -t proc none /proc
for arg in `cat /proc/cmdline`; do
  if [[ "${{arg%%=*}}" = "init" ]]; then
    init="${{arg#init=}}"
    break
  fi
done
umount /proc

if [[ -z "$init" ]]; then
  log 'no init= option'
  exit 1
fi

log 'done; switching to real root'
exec /bin/switch_root /newroot "$init" "$@"
"""


def generate_init(config) -> bytes:
    out = io.StringIO()
    out.write(_INIT.format(
        logfunc=_LOGFUNC,
        access=config.access))
    return out.getvalue().encode('utf-8')

class Config:
    __slots__ = ['modfiles', 'virtme_data', 'virtme_init_path', 'busybox', 'access']
    def __init__(self):
        self.modfiles: List[str] = []
        self.virtme_data: Dict[bytes, bytes] = {}
        self.virtme_init_path: Optional[str] = None
        self.busybox: Optional[str] = None
        self.access = 'ro'

def mkinitramfs(out, config) -> None:
    cw = cpiowriter.CpioWriter(out)
    make_base_layout(cw)
    make_dev_nodes(cw)
    install_busybox(cw, config)
    install_modprobe(cw)
    if config.modfiles is not None:
        install_modules(cw, config.modfiles)
    for name,contents in config.virtme_data.items():
        cw.write_file(b'run_virtme/data/' + name, body=contents, mode=0o755)
    cw.write_file(b'init', body=generate_init(config),
                  mode=0o755)
    cw.write_trailer()

def find_busybox(root, is_native) -> Optional[str]:
    return util.find_binary(['busybox-static', 'busybox.static', 'busybox'],
                            root=root, use_path=is_native)
