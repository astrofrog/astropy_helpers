# Licensed under a 3-clause BSD style license - see LICENSE.rst

"""
Utilities for generating the version string for Astropy (or an affiliated
package) and the version.py module, which contains version info for the
package.

Within the generated astropy.version module, the `major`, `minor`, and `bugfix`
variables hold the respective parts of the version number (bugfix is '0' if
absent). The `release` variable is True if this is a release, and False if this
is a development version of astropy. For the actual version string, use::

    from astropy.version import version

or::

    from astropy import __version__

"""

from __future__ import division

import datetime
import imp
import os
import pkgutil
import sys
import time
import warnings

from distutils import log
from configparser import ConfigParser

import pkg_resources

from . import git_helpers
from .distutils_helpers import is_distutils_display_option
from .git_helpers import get_git_devstr
from .utils import AstropyDeprecationWarning, import_file

__all__ = ['generate_version_py']


def _version_split(version):
    """
    Split a version string into major, minor, and bugfix numbers.  If any of
    those numbers are missing the default is zero.  Any pre/post release
    modifiers are ignored.

    Examples
    ========
    >>> _version_split('1.2.3')
    (1, 2, 3)
    >>> _version_split('1.2')
    (1, 2, 0)
    >>> _version_split('1.2rc1')
    (1, 2, 0)
    >>> _version_split('1')
    (1, 0, 0)
    >>> _version_split('')
    (0, 0, 0)
    """

    parsed_version = pkg_resources.parse_version(version)

    if hasattr(parsed_version, 'base_version'):
        # New version parsing for setuptools >= 8.0
        if parsed_version.base_version:
            parts = [int(part)
                     for part in parsed_version.base_version.split('.')]
        else:
            parts = []
    else:
        parts = []
        for part in parsed_version:
            if part.startswith('*'):
                # Ignore any .dev, a, b, rc, etc.
                break
            parts.append(int(part))

    if len(parts) < 3:
        parts += [0] * (3 - len(parts))

    # In principle a version could have more parts (like 1.2.3.4) but we only
    # support <major>.<minor>.<micro>
    return tuple(parts[:3])


# This is used by setup.py to create a new version.py - see that file for
# details. Note that the imports have to be absolute, since this is also used
# by affiliated packages.
_FROZEN_VERSION_PY_TEMPLATE = """
# Autogenerated by {packagetitle}'s setup.py on {timestamp!s} UTC
from __future__ import unicode_literals
import datetime

{header}

major = {major}
minor = {minor}
bugfix = {bugfix}

version_info = (major, minor, bugfix)

release = {rel}
timestamp = {timestamp!r}
debug = {debug}

astropy_helpers_version = "{ahver}"
"""[1:]


_FROZEN_VERSION_PY_WITH_GIT_HEADER = """
{git_helpers}


_packagename = "{packagename}"
_last_generated_version = "{verstr}"
_last_githash = "{githash}"

# Determine where the source code for this module
# lives.  If __file__ is not a filesystem path then
# it is assumed not to live in a git repo at all.
if _get_repo_path(__file__, levels=len(_packagename.split('.'))):
    version = update_git_devstr(_last_generated_version, path=__file__)
    githash = get_git_devstr(sha=True, show_warning=False,
                             path=__file__) or _last_githash
else:
    # The file does not appear to live in a git repo so don't bother
    # invoking git
    version = _last_generated_version
    githash = _last_githash
"""[1:]


_FROZEN_VERSION_PY_STATIC_HEADER = """
version = "{verstr}"
githash = "{githash}"
"""[1:]


def _get_version_py_str(packagename, version, githash, release, debug,
                        uses_git=True):
    try:
        from astropy_helpers import __version__ as ahver
    except ImportError:
        ahver = "unknown"

    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', time.time()))
    timestamp = datetime.datetime.utcfromtimestamp(epoch)
    major, minor, bugfix = _version_split(version)

    if packagename.lower() == 'astropy':
        packagetitle = 'Astropy'
    else:
        packagetitle = 'Astropy-affiliated package ' + packagename

    header = ''

    if uses_git:
        header = _generate_git_header(packagename, version, githash)
    elif not githash:
        # _generate_git_header will already generate a new git has for us, but
        # for creating a new version.py for a release (even if uses_git=False)
        # we still need to get the githash to include in the version.py
        # See https://github.com/astropy/astropy-helpers/issues/141
        githash = git_helpers.get_git_devstr(sha=True, show_warning=True)

    if not header:  # If _generate_git_header fails it returns an empty string
        header = _FROZEN_VERSION_PY_STATIC_HEADER.format(verstr=version,
                                                         githash=githash)

    return _FROZEN_VERSION_PY_TEMPLATE.format(packagetitle=packagetitle,
                                              timestamp=timestamp,
                                              header=header,
                                              major=major,
                                              minor=minor,
                                              bugfix=bugfix,
                                              ahver=ahver,
                                              rel=release, debug=debug)


def _generate_git_header(packagename, version, githash):
    """
    Generates a header to the version.py module that includes utilities for
    probing the git repository for updates (to the current git hash, etc.)
    These utilities should only be available in development versions, and not
    in release builds.

    If this fails for any reason an empty string is returned.
    """

    loader = pkgutil.get_loader(git_helpers)
    source = loader.get_source(git_helpers.__name__) or ''
    source_lines = source.splitlines()
    if not source_lines:
        log.warn('Cannot get source code for astropy_helpers.git_helpers; '
                 'git support disabled.')
        return ''

    idx = 0
    for idx, line in enumerate(source_lines):
        if line.startswith('# BEGIN'):
            break
    git_helpers_py = '\n'.join(source_lines[idx + 1:])

    verstr = version

    new_githash = git_helpers.get_git_devstr(sha=True, show_warning=False)

    if new_githash:
        githash = new_githash

    return _FROZEN_VERSION_PY_WITH_GIT_HEADER.format(
                git_helpers=git_helpers_py, packagename=packagename,
                verstr=verstr, githash=githash)


def generate_version_py(packagename=None, version=None, release=None, debug=None,
                        uses_git=None, srcdir='.'):
    """
    Generate a version.py file in the package with version information, and
    update developer version strings.

    This function should normally be called without any arguments. In this case
    the package name and version is read in from the ``setup.cfg`` file (from
    the ``name`` or ``package_name`` entry and the ``version`` entry in the
    ``[metadata]`` section).

    If the version is a developer version (of the form ``3.2.dev``), the
    version string will automatically be expanded to include a sequential
    number as a suffix (e.g. ``3.2.dev13312``), and the updated version string
    will be returned by this function.

    Based on this updated version string, a ``version.py`` file will be
    generated inside the package, containing the version string as well as more
    detailed information (for example the major, minor, and bugfix version
    numbers, a ``release`` flag indicating whether the current version is a
    stable or developer version, and so on.
    """

    if packagename is not None:
        warnings.warn('The packagename argument to generate_version_py has '
                      'been deprecated and will be removed in future. Specify '
                      'the package name in setup.cfg instead', AstropyDeprecationWarning)

    if version is not None:
        warnings.warn('The version argument to generate_version_py has '
                      'been deprecated and will be removed in future. Specify '
                      'the version number in setup.cfg instead', AstropyDeprecationWarning)

    if release is not None:
        warnings.warn('The release argument to generate_version_py has '
                      'been deprecated and will be removed in future. We now '
                      'use the presence of the "dev" string in the version to '
                      'determine whether this is a release', AstropyDeprecationWarning)

    # We use ConfigParser instead of read_configuration here because the latter
    # only reads in keys recognized by setuptools, but we need to access
    # package_name below.
    conf = ConfigParser()
    conf.read('setup.cfg')

    if conf.has_option('metadata', 'name'):
        packagename = conf.get('metadata', 'name')
    elif conf.has_option('metadata', 'package_name'):
        # The package-template used package_name instead of name for a while
        warnings.warn('Specifying the package name using the "package_name" '
                      'option in setup.cfg is deprecated - use the "name" '
                      'option instead.', AstropyDeprecationWarning)
        packagename = conf.get('metadata', 'package_name')
    elif packagename is not None:  # deprecated
        pass
    else:
        print('ERROR: Could not read package name from setup.cfg', file=sys.stderr)
        sys.exit(1)

    if conf.has_option('metadata', 'version'):
        version = conf.get('metadata', 'version')
        add_git_devstr = True
    elif version is not None:  # deprecated
        add_git_devstr = False
    else:
        print('ERROR: Could not read package version from setup.cfg', file=sys.stderr)
        sys.exit(1)

    if release is None:
        release = 'dev' not in version

    if not release and add_git_devstr:
        version += get_git_devstr(False)

    if uses_git is None:
        uses_git = not release

    # In some cases, packages have a - but this is a _ in the module. Since we
    # are only interested in the module here, we replace - by _
    packagename = packagename.replace('-', '_')

    try:
        version_module = get_pkg_version_module(packagename)

        try:
            last_generated_version = version_module._last_generated_version
        except AttributeError:
            last_generated_version = version_module.version

        try:
            last_githash = version_module._last_githash
        except AttributeError:
            last_githash = version_module.githash

        current_release = version_module.release
        current_debug = version_module.debug
    except ImportError:
        version_module = None
        last_generated_version = None
        last_githash = None
        current_release = None
        current_debug = None

    if release is None:
        # Keep whatever the current value is, if it exists
        release = bool(current_release)

    if debug is None:
        # Likewise, keep whatever the current value is, if it exists
        debug = bool(current_debug)

    package_srcdir = os.path.join(srcdir, *packagename.split('.'))
    version_py = os.path.join(package_srcdir, 'version.py')

    if (last_generated_version != version or current_release != release or
            current_debug != debug):
        if '-q' not in sys.argv and '--quiet' not in sys.argv:
            log.set_threshold(log.INFO)

        if is_distutils_display_option():
            # Always silence unnecessary log messages when display options are
            # being used
            log.set_threshold(log.WARN)

        log.info('Freezing version number to {0}'.format(version_py))

        with open(version_py, 'w') as f:
            # This overwrites the actual version.py
            f.write(_get_version_py_str(packagename, version, last_githash,
                                        release, debug, uses_git=uses_git))

    return version


def get_pkg_version_module(packagename, fromlist=None):
    """Returns the package's .version module generated by
    `astropy_helpers.version_helpers.generate_version_py`.  Raises an
    ImportError if the version module is not found.

    If ``fromlist`` is an iterable, return a tuple of the members of the
    version module corresponding to the member names given in ``fromlist``.
    Raises an `AttributeError` if any of these module members are not found.
    """

    version = import_file(os.path.join(packagename, 'version.py'), name='version')

    if fromlist:
        return tuple(getattr(version, member) for member in fromlist)
    else:
        return version
