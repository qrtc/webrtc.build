# Copyright 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging
import os
import subprocess
import threading
import uuid

from devil.utils import reraiser_thread
from pylib import constants


_MINIUMUM_TIMEOUT = 5.0  # Large enough to account for process start-up.
_PER_LINE_TIMEOUT = .002  # Should be able to process 500 lines per second.


class Deobfuscator(object):
  def __init__(self, mapping_path):
    script_path = os.path.join(
        constants.GetOutDirectory(), 'bin', 'java_deobfuscate')
    cmd = [script_path, mapping_path]
    # Start process eagerly to hide start-up latency.
    self._proc = subprocess.Popen(
        cmd, bufsize=1, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        close_fds=True)
    # Allow only one thread to call TransformLines() at a time.
    self._lock = threading.Lock()
    self._closed_called = False

  def IsClosed(self):
    return self._closed_called or self._proc.returncode is not None

  def IsBusy(self):
    return self._lock.locked()

  def IsReady(self):
    return not self.IsClosed() and not self.IsBusy()

  def TransformLines(self, lines):
    """Deobfuscates obfuscated names found in the given lines.

    If anything goes wrong (process crashes, timeout, etc), returns |lines|.

    Args:
      lines: A list of strings without trailing newlines.

    Returns:
      A list of strings without trailing newlines.
    """
    if not lines:
      return []

    # Deobfuscated stacks contain more frames than obfuscated ones when method
    # inlining occurs. To account for the extra output lines, keep reading until
    # this eof_line token is reached.
    eof_line = uuid.uuid4().hex
    out_lines = []

    def deobfuscate_reader():
      while True:
        line = self._proc.stdout.readline()
        # Return an empty string at EOF (when stdin is closed).
        if not line:
          break
        line = line[:-1]
        if line == eof_line:
          break
        out_lines.append(line)

    if not self.IsReady():
      logging.warning('Having to wait for Java deobfuscation.')

    # Allow only one thread to operate at a time.
    with self._lock:
      if self.IsClosed():
        if not self._closed_called:
          logging.warning('java_deobfuscate process exited with code=%d.',
                          self._proc.returncode)
          self.Close()
        return lines

      # TODO(agrieve): Can probably speed this up by only sending lines through
      #     that might contain an obfuscated name.
      reader_thread = reraiser_thread.ReraiserThread(deobfuscate_reader)
      reader_thread.start()

      try:
        self._proc.stdin.write('\n'.join(lines))
        self._proc.stdin.write('\n{}\n'.format(eof_line))
        self._proc.stdin.flush()
        timeout = max(_MINIUMUM_TIMEOUT, len(lines) * _PER_LINE_TIMEOUT)
        reader_thread.join(timeout)
        if self.IsClosed():
          logging.warning('Close() called by another thread during join().')
          return lines
        if reader_thread.is_alive():
          logging.error('java_deobfuscate timed out.')
          self.Close()
          return lines
        return out_lines
      except IOError:
        logging.exception('Exception during java_deobfuscate')
        self.Close()
        return lines

  def Close(self):
    self._closed_called = True
    if not self.IsClosed():
      self._proc.stdin.close()
      self._proc.kill()
      self._proc.wait()

  def __del__(self):
    if not self._closed_called:
      logging.error('Forgot to Close() deobfuscator')


class DeobfuscatorPool(object):
  def __init__(self, mapping_path, pool_size=4):
    self._mapping_path = mapping_path
    self._pool = [Deobfuscator(mapping_path) for _ in xrange(pool_size)]
    # Allow only one thread to select from the pool at a time.
    self._lock = threading.Lock()

  def TransformLines(self, lines):
    with self._lock:
      assert self._pool, 'TransformLines() called on a closed DeobfuscatorPool.'
      # Restart any closed Deobfuscators.
      for i, d in enumerate(self._pool):
        if d.IsClosed():
          logging.warning('Restarting closed Deobfuscator instance.')
          self._pool[i] = Deobfuscator(self._mapping_path)

      selected = next((x for x in self._pool if x.IsReady()), self._pool[0])
      # Rotate the order so that next caller will not choose the same one.
      self._pool.remove(selected)
      self._pool.append(selected)

    return selected.TransformLines(lines)

  def Close(self):
    with self._lock:
      for d in self._pool:
        d.Close()
      self._pool = None
