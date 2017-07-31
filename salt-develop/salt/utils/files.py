# -*- coding: utf-8 -*-

from __future__ import absolute_import

# Import Python libs
import contextlib
import errno
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import time

# Import Salt libs
import salt.utils
import salt.modules.selinux
import salt.ext.six as six
from salt.exceptions import CommandExecutionError, FileLockError, MinionError
from salt.utils.decorators import jinja_filter

# Import 3rd-party libs
from stat import S_IMODE

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    # fcntl is not available on windows
    HAS_FCNTL = False

log = logging.getLogger(__name__)

REMOTE_PROTOS = ('http', 'https', 'ftp', 'swift', 's3')
VALID_PROTOS = ('salt', 'file') + REMOTE_PROTOS
TEMPFILE_PREFIX = '__salt.tmp.'


def guess_archive_type(name):
    '''
    Guess an archive type (tar, zip, or rar) by its file extension
    '''
    name = name.lower()
    for ending in ('tar', 'tar.gz', 'tar.bz2', 'tar.xz', 'tgz', 'tbz2', 'txz',
                   'tar.lzma', 'tlz'):
        if name.endswith('.' + ending):
            return 'tar'
    for ending in ('zip', 'rar'):
        if name.endswith('.' + ending):
            return ending
    return None


def mkstemp(*args, **kwargs):
    '''
    Helper function which does exactly what ``tempfile.mkstemp()`` does but
    accepts another argument, ``close_fd``, which, by default, is true and closes
    the fd before returning the file path. Something commonly done throughout
    Salt's code.
    '''
    if 'prefix' not in kwargs:
        kwargs['prefix'] = '__salt.tmp.'
    close_fd = kwargs.pop('close_fd', True)
    fd_, f_path = tempfile.mkstemp(*args, **kwargs)
    if close_fd is False:
        return fd_, f_path
    os.close(fd_)
    del fd_
    return f_path


def recursive_copy(source, dest):
    '''
    Recursively copy the source directory to the destination,
    leaving files with the source does not explicitly overwrite.

    (identical to cp -r on a unix machine)
    '''
    for root, _, files in os.walk(source):
        path_from_source = root.replace(source, '').lstrip('/')
        target_directory = os.path.join(dest, path_from_source)
        if not os.path.exists(target_directory):
            os.makedirs(target_directory)
        for name in files:
            file_path_from_source = os.path.join(source, path_from_source, name)
            target_path = os.path.join(target_directory, name)
            shutil.copyfile(file_path_from_source, target_path)


def copyfile(source, dest, backup_mode='', cachedir=''):
    '''
    Copy files from a source to a destination in an atomic way, and if
    specified cache the file.
    '''
    if not os.path.isfile(source):
        raise IOError(
            '[Errno 2] No such file or directory: {0}'.format(source)
        )
    if not os.path.isdir(os.path.dirname(dest)):
        raise IOError(
            '[Errno 2] No such file or directory: {0}'.format(dest)
        )
    bname = os.path.basename(dest)
    dname = os.path.dirname(os.path.abspath(dest))
    tgt = mkstemp(prefix=bname, dir=dname)
    shutil.copyfile(source, tgt)
    bkroot = ''
    if cachedir:
        bkroot = os.path.join(cachedir, 'file_backup')
    if backup_mode == 'minion' or backup_mode == 'both' and bkroot:
        if os.path.exists(dest):
            salt.utils.backup_minion(dest, bkroot)
    if backup_mode == 'master' or backup_mode == 'both' and bkroot:
        # TODO, backup to master
        pass
    # Get current file stats to they can be replicated after the new file is
    # moved to the destination path.
    fstat = None
    if not salt.utils.is_windows():
        try:
            fstat = os.stat(dest)
        except OSError:
            pass
    shutil.move(tgt, dest)
    if fstat is not None:
        os.chown(dest, fstat.st_uid, fstat.st_gid)
        os.chmod(dest, fstat.st_mode)
    # If SELINUX is available run a restorecon on the file
    rcon = salt.utils.which('restorecon')
    if rcon:
        policy = False
        try:
            policy = salt.modules.selinux.getenforce()
        except (ImportError, CommandExecutionError):
            pass
        if policy == 'Enforcing':
            with fopen(os.devnull, 'w') as dev_null:
                cmd = [rcon, dest]
                subprocess.call(cmd, stdout=dev_null, stderr=dev_null)
    if os.path.isfile(tgt):
        # The temp file failed to move
        try:
            os.remove(tgt)
        except Exception:
            pass


def rename(src, dst):
    '''
    On Windows, os.rename() will fail with a WindowsError exception if a file
    exists at the destination path. This function checks for this error and if
    found, it deletes the destination path first.
    '''
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        try:
            os.remove(dst)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise MinionError(
                    'Error: Unable to remove {0}: {1}'.format(
                        dst,
                        exc.strerror
                    )
                )
        os.rename(src, dst)


def process_read_exception(exc, path):
    '''
    Common code for raising exceptions when reading a file fails
    '''
    if exc.errno == errno.ENOENT:
        raise CommandExecutionError('{0} does not exist'.format(path))
    elif exc.errno == errno.EACCES:
        raise CommandExecutionError(
            'Permission denied reading from {0}'.format(path)
        )
    else:
        raise CommandExecutionError(
            'Error {0} encountered reading from {1}: {2}'.format(
                exc.errno, path, exc.strerror
            )
        )


@contextlib.contextmanager
def wait_lock(path, lock_fn=None, timeout=5, sleep=0.1, time_start=None):
    '''
    Obtain a write lock. If one exists, wait for it to release first
    '''
    if not isinstance(path, six.string_types):
        raise FileLockError('path must be a string')
    if lock_fn is None:
        lock_fn = path + '.w'
    if time_start is None:
        time_start = time.time()
    obtained_lock = False

    def _raise_error(msg, race=False):
        '''
        Raise a FileLockError
        '''
        raise FileLockError(msg, time_start=time_start)

    try:
        if os.path.exists(lock_fn) and not os.path.isfile(lock_fn):
            _raise_error(
                'lock_fn {0} exists and is not a file'.format(lock_fn)
            )

        open_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        while time.time() - time_start < timeout:
            try:
                # Use os.open() to obtain filehandle so that we can force an
                # exception if the file already exists. Concept found here:
                # http://stackoverflow.com/a/10979569
                fh_ = os.open(lock_fn, open_flags)
            except (IOError, OSError) as exc:
                if exc.errno != errno.EEXIST:
                    _raise_error(
                        'Error {0} encountered obtaining file lock {1}: {2}'
                        .format(exc.errno, lock_fn, exc.strerror)
                    )
                log.trace(
                    'Lock file %s exists, sleeping %f seconds', lock_fn, sleep
                )
                time.sleep(sleep)
            else:
                # Write the lock file
                with os.fdopen(fh_, 'w'):
                    pass
                # Lock successfully acquired
                log.trace('Write lock %s obtained', lock_fn)
                obtained_lock = True
                # Transfer control back to the code inside the with block
                yield
                # Exit the loop
                break

        else:
            _raise_error(
                'Timeout of {0} seconds exceeded waiting for lock_fn {1} '
                'to be released'.format(timeout, lock_fn)
            )

    except FileLockError:
        raise

    except Exception as exc:
        _raise_error(
            'Error encountered obtaining file lock {0}: {1}'.format(
                lock_fn,
                exc
            )
        )

    finally:
        if obtained_lock:
            os.remove(lock_fn)
            log.trace('Write lock for %s (%s) released', path, lock_fn)


@contextlib.contextmanager
def set_umask(mask):
    '''
    Temporarily set the umask and restore once the contextmanager exits
    '''
    if salt.utils.is_windows():
        # Don't attempt on Windows
        yield
    else:
        try:
            orig_mask = os.umask(mask)
            yield
        finally:
            os.umask(orig_mask)


def fopen(*args, **kwargs):
    '''
    Wrapper around open() built-in to set CLOEXEC on the fd.

    This flag specifies that the file descriptor should be closed when an exec
    function is invoked;

    When a file descriptor is allocated (as with open or dup), this bit is
    initially cleared on the new file descriptor, meaning that descriptor will
    survive into the new program after exec.

    NB! We still have small race condition between open and fcntl.
    '''
    binary = None
    # ensure 'binary' mode is always used on Windows in Python 2
    if ((six.PY2 and salt.utils.is_windows() and 'binary' not in kwargs) or
            kwargs.pop('binary', False)):
        if len(args) > 1:
            args = list(args)
            if 'b' not in args[1]:
                args[1] += 'b'
        elif kwargs.get('mode', None):
            if 'b' not in kwargs['mode']:
                kwargs['mode'] += 'b'
        else:
            # the default is to read
            kwargs['mode'] = 'rb'
    elif six.PY3 and 'encoding' not in kwargs:
        # In Python 3, if text mode is used and the encoding
        # is not specified, set the encoding to 'utf-8'.
        binary = False
        if len(args) > 1:
            args = list(args)
            if 'b' in args[1]:
                binary = True
        if kwargs.get('mode', None):
            if 'b' in kwargs['mode']:
                binary = True
        if not binary:
            kwargs['encoding'] = __salt_system_encoding__

    if six.PY3 and not binary and not kwargs.get('newline', None):
        kwargs['newline'] = ''

    f_handle = open(*args, **kwargs)  # pylint: disable=resource-leakage

    if is_fcntl_available():
        # modify the file descriptor on systems with fcntl
        # unix and unix-like systems only
        try:
            FD_CLOEXEC = fcntl.FD_CLOEXEC   # pylint: disable=C0103
        except AttributeError:
            FD_CLOEXEC = 1                  # pylint: disable=C0103
        old_flags = fcntl.fcntl(f_handle.fileno(), fcntl.F_GETFD)
        fcntl.fcntl(f_handle.fileno(), fcntl.F_SETFD, old_flags | FD_CLOEXEC)

    return f_handle


@contextlib.contextmanager
def flopen(*args, **kwargs):
    '''
    Shortcut for fopen with lock and context manager.
    '''
    with fopen(*args, **kwargs) as f_handle:
        try:
            if is_fcntl_available(check_sunos=True):
                fcntl.flock(f_handle.fileno(), fcntl.LOCK_SH)
            yield f_handle
        finally:
            if is_fcntl_available(check_sunos=True):
                fcntl.flock(f_handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def fpopen(*args, **kwargs):
    '''
    Shortcut for fopen with extra uid, gid, and mode options.

    Supported optional Keyword Arguments:

    mode
        Explicit mode to set. Mode is anything os.chmod would accept
        as input for mode. Works only on unix/unix-like systems.

    uid
        The uid to set, if not set, or it is None or -1 no changes are
        made. Same applies if the path is already owned by this uid.
        Must be int. Works only on unix/unix-like systems.

    gid
        The gid to set, if not set, or it is None or -1 no changes are
        made. Same applies if the path is already owned by this gid.
        Must be int. Works only on unix/unix-like systems.

    '''
    # Remove uid, gid and mode from kwargs if present
    uid = kwargs.pop('uid', -1)  # -1 means no change to current uid
    gid = kwargs.pop('gid', -1)  # -1 means no change to current gid
    mode = kwargs.pop('mode', None)
    with fopen(*args, **kwargs) as f_handle:
        path = args[0]
        d_stat = os.stat(path)

        if hasattr(os, 'chown'):
            # if uid and gid are both -1 then go ahead with
            # no changes at all
            if (d_stat.st_uid != uid or d_stat.st_gid != gid) and \
                    [i for i in (uid, gid) if i != -1]:
                os.chown(path, uid, gid)

        if mode is not None:
            mode_part = S_IMODE(d_stat.st_mode)
            if mode_part != mode:
                os.chmod(path, (d_stat.st_mode ^ mode_part) | mode)

        yield f_handle


def safe_rm(tgt):
    '''
    Safely remove a file
    '''
    try:
        os.remove(tgt)
    except (IOError, OSError):
        pass


def rm_rf(path):
    '''
    Platform-independent recursive delete. Includes code from
    http://stackoverflow.com/a/2656405
    '''
    def _onerror(func, path, exc_info):
        '''
        Error handler for `shutil.rmtree`.

        If the error is due to an access error (read only file)
        it attempts to add write permission and then retries.

        If the error is for another reason it re-raises the error.

        Usage : `shutil.rmtree(path, onerror=onerror)`
        '''
        if salt.utils.is_windows() and not os.access(path, os.W_OK):
            # Is the error an access error ?
            os.chmod(path, stat.S_IWUSR)
            func(path)
        else:
            raise  # pylint: disable=E0704
    if os.path.islink(path) or not os.path.isdir(path):
        os.remove(path)
    else:
        shutil.rmtree(path, onerror=_onerror)


@jinja_filter('is_empty')
def is_empty(filename):
    '''
    Is a file empty?
    '''
    try:
        return os.stat(filename).st_size == 0
    except OSError:
        # Non-existent file or permission denied to the parent dir
        return False


def is_fcntl_available(check_sunos=False):
    '''
    Simple function to check if the ``fcntl`` module is available or not.

    If ``check_sunos`` is passed as ``True`` an additional check to see if host is
    SunOS is also made. For additional information see: http://goo.gl/159FF8
    '''
    if check_sunos and salt.utils.is_sunos():
        return False
    return HAS_FCNTL