from __future__ import absolute_import, division, print_function

import io
import locale
import os
from os.path import join, isdir, isfile, abspath, basename, exists, normpath, expanduser
import re
import shutil
from subprocess import CalledProcessError
import sys
import time

from .conda_interface import download, TemporaryDirectory
from .conda_interface import hashsum_file

from conda_build.os_utils import external
from conda_build.conda_interface import url_path, CondaHTTPError
from conda_build.utils import (tar_xf, unzip, safe_print_unicode, copy_into, on_win, ensure_list,
                               check_output_env, check_call_env, convert_path_for_cygwin_or_msys2,
                               get_logger, rm_rf, LoggingContext)


if on_win:
    from conda_build.utils import convert_unix_path_to_win

if sys.version_info[0] == 3:
    from urllib.parse import urljoin
else:
    from urlparse import urljoin

git_submod_re = re.compile(r'(?:.+)\.(.+)\.(?:.+)\s(.+)')
ext_re = re.compile(r"(.*?)(\.(?:tar\.)?[^.]+)$")


def append_hash_to_fn(fn, hash_value):
    return ext_re.sub(r"\1_{}\2".format(hash_value[:10]), fn)


def download_to_cache(cache_folder, recipe_path, source_dict):
    ''' Download a source to the local cache. '''
    print('Source cache directory is: %s' % cache_folder)
    if not isdir(cache_folder):
        os.makedirs(cache_folder)

    fn = source_dict['fn'] if 'fn' in source_dict else basename(source_dict['url'])
    hash_added = False
    for hash_type in ('md5', 'sha1', 'sha256'):
        if hash_type in source_dict:
            fn = append_hash_to_fn(fn, source_dict[hash_type])
            hash_added = True
            break
    else:
        log = get_logger(__name__)
        log.warn("No hash (md5, sha1, sha256) provided.  Source download forced.  "
                 "Add hash to recipe to use source cache.")
    path = join(cache_folder, fn)
    if isfile(path):
        print('Found source in cache: %s' % fn)
    else:
        print('Downloading source to cache: %s' % fn)
        if not isinstance(source_dict['url'], list):
            source_dict['url'] = [source_dict['url']]

        for url in source_dict['url']:
            if "://" not in url:
                if url.startswith('~'):
                    url = expanduser(url)
                if not os.path.isabs(url):
                    url = os.path.normpath(os.path.join(recipe_path, url))
                url = url_path(url)
            else:
                if url.startswith('file:///~'):
                    url = 'file:///' + expanduser(url[8:]).replace('\\', '/')
            try:
                print("Downloading %s" % url)
                with LoggingContext():
                    download(url, path)
            except CondaHTTPError as e:
                print("Error: %s" % str(e).strip(), file=sys.stderr)
            except RuntimeError as e:
                print("Error: %s" % str(e).strip(), file=sys.stderr)
            else:
                print("Success")
                break
        else:  # no break
            raise RuntimeError("Could not download %s" % url)

    hashed = None
    for tp in ('md5', 'sha1', 'sha256'):
        if 'tp' in source_dict:
            expected_hash = source_dict[tp]
            hashed = hashsum_file(path, tp)
            if expected_hash != hashed:
                raise RuntimeError("%s mismatch: '%s' != '%s'" %
                           (tp.upper(), hashed, expected_hash))
            break

    # this is really a fallback.  If people don't provide the hash, we still need to prevent
    #    collisions in our source cache, but the end user will get no benefirt from the cache.
    if not hash_added:
        if not hashed:
            hashed = hashsum_file(path, 'sha256')
        dest_path = append_hash_to_fn(path, hashed)
        os.rename(path, dest_path)
        path = dest_path

    return path


def hoist_single_extracted_folder(nested_folder):
    """Moves all files/folders one level up.

    This is for when your archive extracts into its own folder, so that we don't need to
    know exactly what that folder is called."""
    flist = os.listdir(nested_folder)
    parent = os.path.dirname(nested_folder)
    for thing in flist:
        if not os.path.isdir(os.path.join(parent, thing)):
            shutil.move(os.path.join(nested_folder, thing), os.path.join(parent, thing))
        else:
            copy_into(os.path.join(nested_folder, thing), os.path.join(parent, thing))
            nested_folder = os.path.join(nested_folder, thing)
    rm_rf(nested_folder)


def unpack(source_dict, src_dir, cache_folder, recipe_path, croot, verbose=False,
           timeout=90, locking=True):
    ''' Uncompress a downloaded source. '''
    src_path = download_to_cache(cache_folder, recipe_path, source_dict)

    if not isdir(src_dir):
        os.makedirs(src_dir)
    if verbose:
        print("Extracting download")
    with TemporaryDirectory(dir=croot) as tmpdir:
        if src_path.lower().endswith(('.tar.gz', '.tar.bz2', '.tgz', '.tar.xz',
                '.tar', 'tar.z')):
            tar_xf(src_path, tmpdir)
        elif src_path.lower().endswith('.zip'):
            unzip(src_path, tmpdir)
        elif src_path.lower().endswith('.whl'):
            # copy wheel itself *and* unpack it
            # This allows test_files or about.license_file to locate files in the wheel,
            # as well as `pip install name-version.whl` as install command
            unzip(src_path, tmpdir)
            copy_into(src_path, tmpdir, timeout, locking=locking)
        else:
            # In this case, the build script will need to deal with unpacking the source
            print("Warning: Unrecognized source format. Source file will be copied to the SRC_DIR")
            copy_into(src_path, tmpdir, timeout, locking=locking)
        flist = os.listdir(tmpdir)
        folder = os.path.join(tmpdir, flist[0])
        if len(flist) == 1 and os.path.isdir(folder):
            hoist_single_extracted_folder(folder)
        flist = os.listdir(tmpdir)
        for f in flist:
            shutil.move(os.path.join(tmpdir, f), os.path.join(src_dir, f))


def git_mirror_checkout_recursive(git, mirror_dir, checkout_dir, git_url, git_cache, git_ref=None,
                                  git_depth=-1, is_top_level=True, verbose=True):
    """ Mirror (and checkout) a Git repository recursively.

        It's not possible to use `git submodule` on a bare
        repository, so the checkout must be done before we
        know which submodules there are.

        Worse, submodules can be identified by using either
        absolute URLs or relative paths.  If relative paths
        are used those need to be relocated upon mirroring,
        but you could end up with `../../../../blah` and in
        that case conda-build could be tricked into writing
        to the root of the drive and overwriting the system
        folders unless steps are taken to prevent that.
    """

    if verbose:
        stdout = None
        stderr = None
    else:
        FNULL = open(os.devnull, 'w')
        stdout = FNULL
        stderr = FNULL

    if not mirror_dir.startswith(git_cache + os.sep):
        sys.exit("Error: Attempting to mirror to %s which is outside of GIT_CACHE %s"
                 % (mirror_dir, git_cache))

    # This is necessary for Cygwin git and m2-git, although it is fixed in newer MSYS2.
    git_mirror_dir = convert_path_for_cygwin_or_msys2(git, mirror_dir)
    git_checkout_dir = convert_path_for_cygwin_or_msys2(git, checkout_dir)

    if not isdir(os.path.dirname(mirror_dir)):
        os.makedirs(os.path.dirname(mirror_dir))
    if isdir(mirror_dir):
        if git_ref != 'HEAD':
            check_call_env([git, 'fetch'], cwd=mirror_dir, stdout=stdout, stderr=stderr)
        else:
            # Unlike 'git clone', fetch doesn't automatically update the cache's HEAD,
            # So here we explicitly store the remote HEAD in the cache's local refs/heads,
            # and then explicitly set the cache's HEAD.
            # This is important when the git repo is a local path like "git_url: ../",
            # but the user is working with a branch other than 'master' without
            # explicitly providing git_rev.
            check_call_env([git, 'fetch', 'origin', '+HEAD:_conda_cache_origin_head'],
                       cwd=mirror_dir, stdout=stdout, stderr=stderr)
            check_call_env([git, 'symbolic-ref', 'HEAD', 'refs/heads/_conda_cache_origin_head'],
                       cwd=mirror_dir, stdout=stdout, stderr=stderr)
    else:
        args = [git, 'clone', '--mirror']
        if git_depth > 0:
            args += ['--depth', str(git_depth)]
        try:
            check_call_env(args + [git_url, git_mirror_dir], stdout=stdout, stderr=stderr)
        except CalledProcessError:
            # on windows, remote URL comes back to us as cygwin or msys format.  Python doesn't
            # know how to normalize it.  Need to convert it to a windows path.
            if sys.platform == 'win32' and git_url.startswith('/'):
                git_url = convert_unix_path_to_win(git_url)

            if os.path.exists(git_url):
                # Local filepaths are allowed, but make sure we normalize them
                git_url = normpath(git_url)
            check_call_env(args + [git_url, git_mirror_dir], stdout=stdout, stderr=stderr)
        assert isdir(mirror_dir)

    # Now clone from mirror_dir into checkout_dir.
    check_call_env([git, 'clone', git_mirror_dir, git_checkout_dir], stdout=stdout, stderr=stderr)
    if is_top_level:
        checkout = git_ref
        if git_url.startswith('.'):
            output = check_output_env([git, "rev-parse", checkout], stdout=stdout, stderr=stderr)
            checkout = output.decode('utf-8')
        if verbose:
            print('checkout: %r' % checkout)
        if checkout:
            check_call_env([git, 'checkout', checkout],
                           cwd=checkout_dir, stdout=stdout, stderr=stderr)

    # submodules may have been specified using relative paths.
    # Those paths are relative to git_url, and will not exist
    # relative to mirror_dir, unless we do some work to make
    # it so.
    try:
        submodules = check_output_env([git, 'config', '--file', '.gitmodules', '--get-regexp',
                                   'url'], stderr=stdout, cwd=checkout_dir)
        submodules = submodules.decode('utf-8').splitlines()
    except CalledProcessError:
        submodules = []
    for submodule in submodules:
        matches = git_submod_re.match(submodule)
        if matches and matches.group(2)[0] == '.':
            submod_name = matches.group(1)
            submod_rel_path = matches.group(2)
            submod_url = urljoin(git_url + '/', submod_rel_path)
            submod_mirror_dir = os.path.normpath(
                os.path.join(mirror_dir, submod_rel_path))
            if verbose:
                print('Relative submodule %s found: url is %s, submod_mirror_dir is %s' % (
                      submod_name, submod_url, submod_mirror_dir))
            with TemporaryDirectory() as temp_checkout_dir:
                git_mirror_checkout_recursive(git, submod_mirror_dir, temp_checkout_dir, submod_url,
                                              git_cache=git_cache, git_ref=git_ref,
                                              git_depth=git_depth, is_top_level=False,
                                              verbose=verbose)

    if is_top_level:
        # Now that all relative-URL-specified submodules are locally mirrored to
        # relatively the same place we can go ahead and checkout the submodules.
        check_call_env([git, 'submodule', 'update', '--init',
                    '--recursive'], cwd=checkout_dir, stdout=stdout, stderr=stderr)
        git_info(checkout_dir, verbose=verbose)
    if not verbose:
        FNULL.close()


def git_source(source_dict, git_cache, src_dir, recipe_path=None, verbose=True):
    ''' Download a source from a Git repo (or submodule, recursively) '''
    if not isdir(git_cache):
        os.makedirs(git_cache)

    git = external.find_executable('git')
    if not git:
        sys.exit("Error: git is not installed in your root environment.")

    git_url = source_dict['git_url']
    git_depth = int(source_dict.get('git_depth', -1))
    git_ref = source_dict.get('git_rev', 'HEAD')

    if git_url.startswith('.'):
        # It's a relative path from the conda recipe
        git_url = abspath(normpath(os.path.join(recipe_path, git_url)))
        if sys.platform == 'win32':
            git_dn = git_url.replace(':', '_')
        else:
            git_dn = git_url[1:]
    else:
        git_dn = git_url.split('://')[-1].replace('/', os.sep)
        if git_dn.startswith(os.sep):
            git_dn = git_dn[1:]
        git_dn = git_dn.replace(':', '_')
    mirror_dir = join(git_cache, git_dn)
    git_mirror_checkout_recursive(
        git, mirror_dir, src_dir, git_url, git_cache=git_cache, git_ref=git_ref,
        git_depth=git_depth, is_top_level=True, verbose=verbose)
    return git


def git_info(src_dir, verbose=True, fo=None):
    ''' Print info about a Git repo. '''
    assert isdir(src_dir)

    git = external.find_executable('git')
    if not git:
        log = get_logger(__name__)
        log.warn("git not installed in root environment.  Skipping recording of git info.")
        return

    if verbose:
        stderr = None
    else:
        FNULL = open(os.devnull, 'w')
        stderr = FNULL

    # Ensure to explicitly set GIT_DIR as some Linux machines will not
    # properly execute without it.
    env = os.environ.copy()
    env['GIT_DIR'] = join(src_dir, '.git')
    env = {str(key): str(value) for key, value in env.items()}
    for cmd, check_error in [
            ('git log -n1', True),
            ('git describe --tags --dirty', False),
            ('git status', True)]:
        try:
            stdout = check_output_env(cmd.split(), stderr=stderr, cwd=src_dir, env=env)
        except CalledProcessError as e:
            if check_error:
                raise Exception("git error: %s" % str(e))
        encoding = locale.getpreferredencoding()
        if not fo:
            encoding = sys.stdout.encoding
        encoding = encoding or 'utf-8'
        if hasattr(stdout, 'decode'):
            stdout = stdout.decode(encoding, 'ignore')
        if fo:
            fo.write(u'==> %s <==\n' % cmd)
            if verbose:
                fo.write(stdout + u'\n')
        else:
            if verbose:
                print(u'==> %s <==\n' % cmd)
                safe_print_unicode(stdout + u'\n')


def hg_source(source_dict, src_dir, hg_cache, verbose):
    ''' Download a source from Mercurial repo. '''
    if verbose:
        stdout = None
        stderr = None
    else:
        FNULL = open(os.devnull, 'w')
        stdout = FNULL
        stderr = FNULL

    hg_url = source_dict['hg_url']
    if not isdir(hg_cache):
        os.makedirs(hg_cache)
    hg_dn = hg_url.split(':')[-1].replace('/', '_')
    cache_repo = join(hg_cache, hg_dn)
    if isdir(cache_repo):
        check_call_env(['hg', 'pull'], cwd=cache_repo, stdout=stdout, stderr=stderr)
    else:
        check_call_env(['hg', 'clone', hg_url, cache_repo], stdout=stdout, stderr=stderr)
        assert isdir(cache_repo)

    # now clone in to work directory
    update = source_dict.get('hg_tag') or 'tip'
    if verbose:
        print('checkout: %r' % update)

    check_call_env(['hg', 'clone', cache_repo, src_dir], stdout=stdout,
                   stderr=stderr)
    check_call_env(['hg', 'update', '-C', update], cwd=src_dir, stdout=stdout,
                   stderr=stderr)

    if not verbose:
        FNULL.close()

    return src_dir


def svn_source(source_dict, src_dir, svn_cache, verbose=True, timeout=90, locking=True):
    ''' Download a source from SVN repo. '''
    if verbose:
        stdout = None
        stderr = None
    else:
        FNULL = open(os.devnull, 'w')
        stdout = FNULL
        stderr = FNULL

    def parse_bool(s):
        return str(s).lower().strip() in ('yes', 'true', '1', 'on')

    svn_url = source_dict['svn_url']
    svn_revision = source_dict.get('svn_rev') or 'head'
    svn_ignore_externals = parse_bool(source_dict.get('svn_ignore_externals') or 'no')
    if not isdir(svn_cache):
        os.makedirs(svn_cache)
    svn_dn = svn_url.split(':', 1)[-1].replace('/', '_').replace(':', '_')
    cache_repo = join(svn_cache, svn_dn)
    if svn_ignore_externals:
        extra_args = ['--ignore-externals']
    else:
        extra_args = []
    if isdir(cache_repo):
        check_call_env(['svn', 'up', '-r', svn_revision] + extra_args, cwd=cache_repo,
                       stdout=stdout, stderr=stderr)
    else:
        check_call_env(['svn', 'co', '-r', svn_revision] + extra_args + [svn_url, cache_repo],
                       stdout=stdout, stderr=stderr)
        assert isdir(cache_repo)

    # now copy into work directory
    copy_into(cache_repo, src_dir, timeout, symlinks=True, locking=locking)

    if not verbose:
        FNULL.close()

    return src_dir


def get_repository_info(recipe_path):
    """This tries to get information about where a recipe came from.  This is different
    from the source - you can have a recipe in svn that gets source via git."""
    try:
        if exists(join(recipe_path, ".git")):
            origin = check_output_env(["git", "config", "--get", "remote.origin.url"],
                                      cwd=recipe_path)
            rev = check_output_env(["git", "rev-parse", "HEAD"], cwd=recipe_path)
            return "Origin {}, commit {}".format(origin, rev)
        elif isdir(join(recipe_path, ".hg")):
            origin = check_output_env(["hg", "paths", "default"], cwd=recipe_path)
            rev = check_output_env(["hg", "id"], cwd=recipe_path).split()[0]
            return "Origin {}, commit {}".format(origin, rev)
        elif isdir(join(recipe_path, ".svn")):
            info = check_output_env(["svn", "info"], cwd=recipe_path)
            server = re.search("Repository Root: (.*)$", info, flags=re.M).group(1)
            revision = re.search("Revision: (.*)$", info, flags=re.M).group(1)
            return "{}, Revision {}".format(server, revision)
        else:
            return "{}, last modified {}".format(recipe_path,
                                             time.ctime(os.path.getmtime(
                                                 join(recipe_path, "meta.yaml"))))
    except CalledProcessError:
        get_logger(__name__).debug("Failed to checkout source in " + recipe_path)
        return "{}, last modified {}".format(recipe_path,
                                             time.ctime(os.path.getmtime(
                                                 join(recipe_path, "meta.yaml"))))


def _ensure_unix_line_endings(path):
    """Replace windows line endings with Unix.  Return path to modified file."""
    out_path = path + "_unix"
    with open(path, "rb") as inputfile:
        with open(out_path, "wb") as outputfile:
            for line in inputfile:
                outputfile.write(line.replace(b"\r\n", b"\n"))
    return out_path


def _ensure_win_line_endings(path):
    """Replace unix line endings with win.  Return path to modified file."""
    out_path = path + "_win"
    with open(path, "rb") as inputfile:
        with open(out_path, "wb") as outputfile:
            for line in inputfile:
                outputfile.write(line.replace(b"\n", b"\r\n"))
    return out_path


def _guess_patch_strip_level(filesstr, src_dir):
    """ Determine the patch strip level automatically. """
    maxlevel = None
    files = {filestr.encode(errors='ignore') for filestr in filesstr}
    src_dir = src_dir.encode(errors='ignore')
    for file in files:
        numslash = file.count(b'/')
        maxlevel = numslash if maxlevel is None else min(maxlevel, numslash)
    if maxlevel == 0:
        patchlevel = 0
    else:
        histo = dict()
        histo = {i: 0 for i in range(maxlevel + 1)}
        for file in files:
            parts = file.split(b'/')
            for level in range(maxlevel + 1):
                if os.path.exists(join(src_dir, *parts[-len(parts) + level:])):
                    histo[level] += 1
        order = sorted(histo, key=histo.get, reverse=True)
        if histo[order[0]] == histo[order[1]]:
            print("Patch level ambiguous, selecting least deep")
        patchlevel = min([key for key, value
                          in histo.items() if value == histo[order[0]]])
    return patchlevel


def _get_patch_file_details(path):
    re_files = re.compile('^(?:---|\+\+\+) ([^\n\t]+)')
    files = set()
    with io.open(path, errors='ignore') as f:
        files = []
        first_line = True
        is_git_format = True
        for l in f.readlines():
            if first_line and not re.match('From [0-9a-f]{40}', l):
                is_git_format = False
            first_line = False
            m = re_files.search(l)
            if m and m.group(1) != '/dev/null':
                files.append(m.group(1))
            elif is_git_format and l.startswith('git') and not l.startswith('git --diff'):
                is_git_format = False
    return (files, is_git_format)


def apply_patch(src_dir, path, config, git=None):
    if not isfile(path):
        sys.exit('Error: no such patch: %s' % path)

    files, is_git_format = _get_patch_file_details(path)
    if git and is_git_format:
        # Prevents git from asking interactive questions,
        # also necessary to achieve sha1 reproducibility;
        # as is --committer-date-is-author-date. By this,
        # we mean a round-trip of git am/git format-patch
        # gives the same file.
        git_env = os.environ
        git_env['GIT_COMMITTER_NAME'] = 'conda-build'
        git_env['GIT_COMMITTER_EMAIL'] = 'conda@conda-build.org'
        check_call_env([git, 'am', '--committer-date-is-author-date', path],
                       cwd=src_dir, stdout=None, env=git_env)
        config.git_commits_since_tag += 1
    else:
        print('Applying patch: %r' % path)
        patch = external.find_executable('patch', config.build_prefix)
        if patch is None:
            sys.exit("""\
        Error:
            Cannot use 'git' (not a git repo and/or patch) and did not find 'patch' in: %s
            You can install 'patch' using apt-get, yum (Linux), Xcode (MacOSX),
            or conda, m2-patch (Windows),
        """ % (os.pathsep.join(external.dir_paths)))
        patch_strip_level = _guess_patch_strip_level(files, src_dir)
        patch_args = ['-p%d' % patch_strip_level, '-i', path]

        # line endings are a pain.
        # https://unix.stackexchange.com/a/243748/34459

        try:
            log = get_logger(__name__)
            log.info("Trying to apply patch as-is")
            check_call_env([patch] + patch_args, cwd=src_dir)
        except CalledProcessError:
            if sys.platform == 'win32':
                unix_ending_file = _ensure_unix_line_endings(path)
                patch_args[-1] = unix_ending_file
                try:
                    log.info("Applying unmodified patch failed.  "
                             "Convert to unix line endings and trying again.")
                    check_call_env([patch] + patch_args, cwd=src_dir)
                except:
                    log.info("Applying unix patch failed.  "
                             "Convert to CRLF line endings and trying again with --binary.")
                    patch_args.insert(0, '--binary')
                    win_ending_file = _ensure_win_line_endings(path)
                    patch_args[-1] = win_ending_file
                    try:
                        check_call_env([patch] + patch_args, cwd=src_dir)
                    finally:
                        if os.path.exists(win_ending_file):
                            os.remove(win_ending_file)  # clean up .patch_win file
                finally:
                    if os.path.exists(unix_ending_file):
                        os.remove(unix_ending_file)  # clean up .patch_unix file
            else:
                raise


def provide(metadata, patch=True):
    """
    given a recipe_dir:
      - download (if necessary)
      - unpack
      - apply patches (if any)
    """
    meta = metadata.get_section('source')
    if not os.path.isdir(metadata.config.build_folder):
        os.makedirs(metadata.config.build_folder)
    git = None

    if hasattr(meta, 'keys'):
        dicts = [meta]
    else:
        dicts = meta

    for source_dict in dicts:
        folder = source_dict.get('folder')
        src_dir = (os.path.join(metadata.config.work_dir, folder) if folder else
                   metadata.config.work_dir)
        if any(k in source_dict for k in ('fn', 'url')):
            unpack(source_dict, src_dir, metadata.config.src_cache, recipe_path=metadata.path,
                   croot=metadata.config.croot, verbose=metadata.config.verbose,
                   timeout=metadata.config.timeout, locking=metadata.config.locking)
        elif 'git_url' in source_dict:
            git = git_source(source_dict, metadata.config.git_cache, src_dir, metadata.path,
                             verbose=metadata.config.verbose)
        # build to make sure we have a work directory with source in it.  We want to make sure that
        #    whatever version that is does not interfere with the test we run next.
        elif 'hg_url' in source_dict:
            hg_source(source_dict, src_dir, metadata.config.hg_cache,
                      verbose=metadata.config.verbose)
        elif 'svn_url' in source_dict:
            svn_source(source_dict, src_dir, metadata.config.svn_cache,
                       verbose=metadata.config.verbose, timeout=metadata.config.timeout,
                       locking=metadata.config.locking)
        elif 'path' in source_dict:
            path = normpath(abspath(join(metadata.path, source_dict['path'])))
            if metadata.config.verbose:
                print("Copying %s to %s" % (path, src_dir))
            # careful here: we set test path to be outside of conda-build root in setup.cfg.
            #    If you don't do that, this is a recursive function
            copy_into(path, src_dir, metadata.config.timeout, symlinks=True,
                    locking=metadata.config.locking, clobber=True)
        else:  # no source
            if not isdir(src_dir):
                os.makedirs(src_dir)

        if patch:
            patches = ensure_list(source_dict.get('patches', []))
            for patch in patches:
                apply_patch(src_dir, join(metadata.path, patch), metadata.config, git)

    return metadata.config.work_dir
