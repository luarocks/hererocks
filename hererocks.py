#!/usr/bin/env python

"""A tool for installing Lua and LuaRocks locally."""

from __future__ import print_function

import argparse
import contextlib
import hashlib
import inspect
import json
import locale
import os
import platform
import re
import shutil
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import zipfile


try:
    from urllib2 import URLError, urlopen
except ImportError:
    from urllib.error import URLError
    from urllib.request import urlopen

if os.name == "nt":
    try:
        import _winreg as winreg
    except ImportError:
        import winreg

hererocks_version = "Hererocks 0.25.1"
__all__ = ["main"]

opts = None
temp_dir = None

activation_script_templates = {
    "get_deactivated_path.lua": """
        local path = os.getenv("PATH")
        local dir_sep = package.config:sub(1, 1)
        local path_sep = dir_sep == "\\\\" and ";" or ":"
        local hererocks_path = "#LOCATION_DQ#" .. dir_sep .. "bin"
        local new_path_parts = {}
        local for_fish = arg[1] == "--fish"

        if for_fish then
            io.stdout:write("set -gx PATH ")
        end

        for path_part in (path .. path_sep):gmatch("([^" .. path_sep .. "]*)" .. path_sep) do
            if path_part ~= hererocks_path then
                if for_fish then
                    path_part = "'" .. path_part:gsub("'", [['\'']]) .. "'"
                end

                table.insert(new_path_parts, path_part)
            end
        end

        io.stdout:write(table.concat(new_path_parts, for_fish and " " or path_sep))
    """,
    "activate": """
        if declare -f -F deactivate-lua >/dev/null; then
            deactivate-lua
        fi

        deactivate-lua () {
            if [ -x '#LOCATION_SQ#/bin/lua' ]; then
                PATH=`'#LOCATION_SQ#/bin/lua' '#LOCATION_SQ#/bin/get_deactivated_path.lua'`
                export PATH

                # Need to rehash under bash and zsh so that new PATH is taken into account.
                if [ -n "${BASH-}" ] || [ -n "${ZSH_VERSION-}" ]; then
                    hash -r 2>/dev/null
                fi
            fi

            unset -f deactivate-lua
        }

        PATH='#LOCATION_SQ#/bin':"$PATH"
        export PATH

        # As in deactivate-lua, rehash after changing PATH.
        if [ -n "${BASH-}" ] || [ -n "${ZSH_VERSION-}" ]; then
            hash -r 2>/dev/null
        fi
    """,
    "activate_posix": """
        s=$(command -V deactivate_lua 2>&1)
        if [ $? -eq 0 ]; then
            if [ "${s##*function*}" = '' ]; then
                deactivate_lua
            fi;
        fi;

        deactivate_lua () {
            if [ -x '#LOCATION_SQ#/bin/lua' ]; then
                PATH=`'#LOCATION_SQ#/bin/lua' '#LOCATION_SQ#/bin/get_deactivated_path.lua'`
                export PATH

                hash -r 2>/dev/null
            fi

            unset -f deactivate_lua
        }

        PATH='#LOCATION_SQ#/bin':"$PATH"
        export PATH

        hash -r 2>/dev/null
    """,
    "activate.csh": """
        which deactivate-lua >&/dev/null && deactivate-lua

        alias deactivate-lua 'if ( -x '\\''#LOCATION_NESTED_SQ#/bin/lua'\\'' ) then; setenv PATH `'\\''#LOCATION_NESTED_SQ#/bin/lua'\\'' '\\''#LOCATION_NESTED_SQ#/bin/get_deactivated_path.lua'\\''`; rehash; endif; unalias deactivate-lua'

        setenv PATH '#LOCATION_SQ#/bin':"$PATH"
        rehash
    """,
    "activate.fish": """
        if functions -q deactivate-lua
            deactivate-lua
        end

        function deactivate-lua
            if test -x '#LOCATION_SQ#/bin/lua'
                eval ('#LOCATION_SQ#/bin/lua' '#LOCATION_SQ#/bin/get_deactivated_path.lua' --fish)
            end

            functions -e deactivate-lua
        end

        set -gx PATH '#LOCATION_SQ#/bin' $PATH
    """,
    "activate.bat": """
        @echo off
        where deactivate-lua >nul 2>nul
        if %errorlevel% equ 0 call deactivate-lua
        set "PATH=#LOCATION#\\bin;%PATH%"
    """,
    "deactivate-lua.bat": """
        @echo off
        if exist "#LOCATION#\\bin\\lua.exe" for /f "usebackq delims=" %%p in (`""#LOCATION_PAREN#\\bin\\lua" "#LOCATION_PAREN#\\bin\\get_deactivated_path.lua""`) DO set "PATH=%%p"
    """,
    "activate.ps1": """
        if (test-path function:deactivate-lua) {
            deactivate-lua
        }

        function global:deactivate-lua () {
            if (test-path "#LOCATION#\\bin\\lua.exe") {
                $env:PATH = & "#LOCATION#\\bin\\lua.exe" "#LOCATION#\\bin\\get_deactivated_path.lua"
            }

            remove-item function:deactivate-lua
        }

        $env:PATH = "#LOCATION#\\bin;" + $env:PATH
    """
}

def write_activation_scripts():
    if os.name == "nt":
        template_names = ["get_deactivated_path.lua", "activate.bat", "deactivate-lua.bat", "activate.ps1"]
    else:
        template_names = ["get_deactivated_path.lua", "activate", "activate.csh", "activate.fish", "activate_posix"]

    replacements = {
        "LOCATION": opts.location,
        "LOCATION_DQ": opts.location.replace("\\", "\\\\").replace('"', '\\"'),
        "LOCATION_SQ": opts.location.replace("'", "'\\''"),
        "LOCATION_NESTED_SQ": opts.location.replace("'", "'\\''").replace("'", "'\\''"),
        "LOCATION_PAREN": re.sub("[&,=()]", r"^\g<0>", opts.location)
    }

    for template_name in template_names:
        with open(os.path.join(opts.location, "bin", template_name), "w") as script_handle:
            template = activation_script_templates[template_name][1:]
            template = textwrap.dedent(template)
            script = re.sub(r'#([a-zA-Z_]+)#', lambda match: replacements[match.group(1)], template)
            script_handle.write(script)

def is_executable(path):
    return os.path.exists(path) and os.access(path, os.F_OK | os.X_OK) and not os.path.isdir(path)

def program_exists(prog):
    path = os.environ.get("PATH", os.defpath)

    if not path:
        return False

    if os.name == "nt":
        pathext = os.environ.get("PATHEXT", "").split(os.pathsep)
        candidates = [prog + ext for ext in pathext]
    else:
        candidates = [prog]

    for directory in path.split(os.pathsep):
        for candidate in candidates:
            if is_executable(os.path.join(directory, candidate)):
                return True

    return False

platform_to_lua_target = {
    "linux": "linux",
    "win": "mingw" if os.name == "nt" and program_exists("gcc") and not program_exists("cl") else "vs",
    "darwin": "macosx",
    "freebsd": "freebsd"
}

def using_cl():
    return opts.target.startswith("vs")

def get_default_lua_target():
    for plat, lua_target in platform_to_lua_target.items():
        if sys.platform.startswith(plat):
            return lua_target

    return "posix" if os.name == "posix" else "generic"

def get_default_cache():
    if os.name == "nt":
        cache_root = os.getenv("LOCALAPPDATA")

        if cache_root is None:
            cache_root = os.getenv("USERPROFILE")

            if cache_root is None:
                return None

            cache_root = os.path.join(cache_root, "Local Settings", "Application Data")

        return os.path.join(cache_root, "HereRocks", "Cache")
    else:
        cache_root = os.getenv("XDG_CACHE_HOME", "~/.cache")
        expanded_cache_root = os.path.expanduser(cache_root)

        if expanded_cache_root.startswith("~"):
            return None

        return os.path.join(expanded_cache_root, "hererocks")

def download(url, filename):
    response = urlopen(url, timeout=opts.timeout)
    data = response.read()

    with open(filename, "wb") as out:
        out.write(data)

default_encoding = locale.getpreferredencoding()

def run(*args, **kwargs):
    """Execute a command.

    Command can be passed as several arguments, each being a string
    or a list of strings; lists are flattened.
    If opts.verbose is True, output of the command is shown.
    If the command exits with non-zero, print an error message and exit.
    If keyward argument get_output is True, output is returned.
    Additionally, non-zero exit code with empty output is ignored.
    """

    capture = kwargs.get("get_output", False)
    args = [arg for arglist in args for arg in (arglist if isinstance(arglist, list) else [arglist])]

    if opts.verbose:
        print("Running {}".format(" ".join(args)))

    live_output = opts.verbose and not capture
    runner = subprocess.check_call if live_output else subprocess.check_output

    try:
        output = runner(args, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exception:
        if capture and not exception.output.strip():
            # Ignore errors if output is empty.
            return ""

        if not live_output:
            sys.stdout.write(exception.output.decode(default_encoding, "ignore"))

        sys.exit("Error: got exitcode {} from command {}".format(
            exception.returncode, " ".join(args)))
    except OSError:
        sys.exit("Error: couldn't run {}: is {} in PATH?".format(" ".join(args), args[0]))

    if opts.verbose and capture:
        sys.stdout.write(output.decode(default_encoding, "ignore"))

    return capture and output.decode(default_encoding, "ignore").strip()

def get_output(*args):
    return run(get_output=True, *args)

def memoize(func):
    cache = {}

    def wrapper(arg):
        if cache.get(arg) is None:
            cache[arg] = func(arg)

        return cache[arg]

    return wrapper

def query_registry(key, value):
    keys = [key, key.replace("\\", "\\Wow6432Node\\", 1)]

    for candidate in keys:
        if opts.verbose:
            print("Querying registry key HKEY_LOCAL_MACHINE\\{}:{}".format(candidate, value))

        try:
            handle = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, candidate)
        except WindowsError:
            pass
        else:
            res = winreg.QueryValueEx(handle, value)[0]
            winreg.CloseKey(handle)
            return res

@memoize
def check_existence(path):
    if opts.verbose:
        print("Checking existence of {}".format(path))

    return os.path.exists(path)

def copy_dir(src, dst, ignore_git_dir=True):
    shutil.copytree(src, dst, ignore=(lambda _, __: {".git"}) if ignore_git_dir else None)

def remove_read_only_or_reraise(func, path, exc_info):
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise

def remove_dir(path):
    shutil.rmtree(path, onerror=remove_read_only_or_reraise)

clever_http_git_whitelist = [
    "http://github.com/", "https://github.com/",
    "http://bitbucket.com/", "https://bitbucket.com/"
]

git_branch_does_accept_tags = None

def git_branch_accepts_tags():
    global git_branch_does_accept_tags

    if git_branch_does_accept_tags is None:
        version_output = get_output("git", "--version")
        match = re.search(r"(\d+)\.(\d+)\.?(\d*)", version_output)

        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            tiny = int(match.group(3) or "0")
            git_branch_does_accept_tags = (major, minor, tiny) >= (1, 7, 10)
        else:
            git_branch_does_accept_tags = False

    return git_branch_does_accept_tags

def git_clone_command(repo, ref, is_cache):
    if is_cache:
        # Cache full repos.
        return ["git", "clone"], True

    # Http(s) transport may be dumb and not understand --depth.
    if repo.startswith("http://") or repo.startswith("https://"):
        if not any(map(repo.startswith, clever_http_git_whitelist)):
            return ["git", "clone"], True

    # Have to clone whole repo to get a specific commit.
    if all(c in string.hexdigits for c in ref):
        return ["git", "clone"], True

    if git_branch_accepts_tags():
        return ["git", "clone", "--depth=1", "--branch=" + ref], False
    else:
        return ["git", "clone", "--depth=1"], True

important_identifiers = ["name", "source", "version", "repo", "commit", "location"]
other_identifiers = ["target", "compat", "c flags", "patched", "readline"]

def escape_path(s):
    return re.sub(r"[^\w]", "_", s)

def hash_identifiers(identifiers):
    return "-".join(escape_path(
        identifiers.get(name, "")) for name in important_identifiers + other_identifiers)

def show_identifiers(identifiers):
    title = identifiers["name"]

    if "version" in identifiers:
        title += " " + identifiers["version"]
    elif "major version" in identifiers and title != "LuaJIT":
        title += " " + identifiers["major version"]

    if identifiers["source"] == "release":
        print(title)
    elif identifiers["source"] == "git":
        print("{} @{} (cloned from {})".format(title, identifiers["commit"][:7], identifiers["repo"]))
    else:
        print("{} (from local sources)".format(title))

    for name in other_identifiers:
        if identifiers.get(name):
            print("    {}: {}".format(name.capitalize(), identifiers[name]))

def copy_files(path, *files):
    if not os.path.exists(path):
        os.makedirs(path)

    for src in files:
        if src is not None:
            shutil.copy(src, path)

def exe(name):
    if os.name == "nt":
        return name + ".exe"
    else:
        return name

def objext():
    return ".obj" if using_cl() else ".o"

def sha256_of_file(filename):
    with open(filename, "rb") as handler:
        contents = handler.read()

    return hashlib.sha256(contents).hexdigest()

def strip_extensions(filename):
    if filename.endswith(".zip"):
        return filename[:-len(".zip")]
    elif filename.endswith(".tar.gz"):
        return filename[:-len(".tar.gz")]
    else:
        return filename

class Program(object):
    needs_git_dir_for_build = False

    def __init__(self, version):
        version = self.translations.get(version, version)

        if version in self.versions:
            # Simple version.
            self.source = "release"
            self.fetched = False
            self.version = version
            self.version_suffix = " " + version
        elif "@" in version:
            # Version from a git repo.
            self.source = "git"

            if version.startswith("@"):
                # Use the default git repo for this program.
                self.repo = self.default_repo
                ref = version[1:] or "master"
            else:
                self.repo, _, ref = version.partition("@")

            # Have to clone the repo to get the commit ref points to.
            self.fetch_repo(ref)
            self.commit = get_output("git", "rev-parse", "HEAD")
            self.version_suffix = " @" + self.commit[:7]
        else:
            # Local directory.
            self.source = "local"

            if not os.path.exists(version):
                sys.exit("Error: bad {} version {}".format(self.title, version))

            print("Using {} from {}".format(self.title, version))
            result_dir = os.path.join(temp_dir, self.name)
            copy_dir(version, result_dir, ignore_git_dir=not self.needs_git_dir_for_build)
            os.chdir(result_dir)
            self.fetched = True
            self.version_suffix = ""

    def fetch_repo(self, ref):
        message = "Cloning {} from {} @{}".format(self.title, self.repo, ref)

        if self.repo == self.default_repo and not opts.no_git_cache and opts.downloads is not None:
            # Default repos are cached.
            if not os.path.exists(opts.downloads):
                os.makedirs(opts.downloads)

            repo_path = os.path.join(opts.downloads, self.name)
            self.fetched = False

            if os.path.exists(repo_path):
                print(message + " (cached)")
                # Sync with origin first.
                os.chdir(repo_path)

                if not get_output("git", "rev-parse", "--quiet", "--verify", ref):
                    run("git", "fetch")

                run("git", "checkout", ref)

                # If HEAD is not detached, we are on a branch that must be synced.
                if get_output("git", "symbolic-ref", "-q", "HEAD"):
                    run("git", "pull", "--rebase")

                return
        else:
            self.fetched = True
            repo_path = os.path.join(temp_dir, self.name)

        print(message)
        clone_command, need_checkout = git_clone_command(self.repo, ref, not self.fetched)
        run(clone_command, self.repo, repo_path)
        os.chdir(repo_path)

        if need_checkout and ref != "master":
            run("git", "checkout", ref)

    def fetch(self):
        if self.fetched:
            return

        if self.source == "git":
            # Currently inside the cached git repo, just copy it somewhere.
            result_dir = os.path.join(temp_dir, self.name)
            copy_dir(".", result_dir, ignore_git_dir=not self.needs_git_dir_for_build)
            os.chdir(result_dir)
            return

        if opts.downloads is None:
            archive_name = os.path.join(temp_dir, self.get_download_name())
        else:
            if not os.path.exists(opts.downloads):
                os.makedirs(opts.downloads)

            archive_name = os.path.join(opts.downloads, self.get_download_name())

        if opts.downloads and os.path.exists(archive_name):
            print("Fetching {}{} (cached)".format(self.title, self.version_suffix))
        else:
            for url in self.get_download_urls():
                print("Fetching {}{} from {}".format(self.title, self.version_suffix, url))

                try:
                    download(url, archive_name)
                except URLError as error:
                    print("Download failed: {}".format(str(error.reason)))
                else:
                    break
            else:
                sys.exit(1)

        print("Verifying SHA256 checksum")
        expected_checksum = self.checksums[self.get_download_name()]
        observed_checksum = sha256_of_file(archive_name)
        if expected_checksum != observed_checksum:
            message = "SHA256 checksum mismatch for {}\nExpected: {}\nObserved: {}".format(
                archive_name, expected_checksum, observed_checksum)

            if opts.ignore_checksums:
                print("Warning: " + message)
            else:
                sys.exit("Error: " + message)

        if archive_name.endswith(".zip"):
            archive = zipfile.ZipFile(archive_name)
        else:
            archive = tarfile.open(archive_name, "r:gz")

        archive.extractall(temp_dir)
        archive.close()
        os.chdir(os.path.join(temp_dir, re.sub("-rc[0-9]*$", "", strip_extensions(self.get_download_name()))))
        self.fetched = True

    def set_identifiers(self):
        self.identifiers = {
            "name": self.title,
            "source": self.source
        }

        if self.source == "release":
            self.identifiers["version"] = self.version
        elif self.source == "git":
            self.identifiers["repo"] = self.repo
            self.identifiers["commit"] = self.commit

    def update_identifiers(self, all_identifiers):
        self.all_identifiers = all_identifiers
        installed_identifiers = all_identifiers.get(self.name)
        self.set_identifiers()

        if not opts.ignore_installed and self.source != "local" and installed_identifiers is not None:
            if hash_identifiers(self.identifiers) == hash_identifiers(installed_identifiers):
                print(self.title + self.version_suffix + " already installed")
                return False

        self.build()
        self.install()
        all_identifiers[self.name] = self.identifiers
        return True

class Lua(Program):
    def __init__(self, version):
        super(Lua, self).__init__(version)

        self.source_files_prefix = self.get_source_files_prefix()

        if self.source == "release":
            self.major_version = self.major_version_from_version()
        else:
            self.major_version = self.major_version_from_source()

        if not self.version_suffix:
            self.set_version_suffix()

        self.set_compat()
        self.add_options_to_version_suffix()

        self.redefines = []
        self.compat_cflags = []
        self.set_package_paths()
        self.add_package_paths_redefines()
        self.add_compat_cflags_and_redefines()

    @staticmethod
    def get_source_files_prefix():
        return "src"

    def get_source_file_path(self, file_name):
        if self.source_files_prefix is None:
            return file_name
        else:
            return os.path.join(self.source_files_prefix, file_name)

    @contextlib.contextmanager
    def in_source_files_prefix(self):
        if self.source_files_prefix is not None:
            start_dir = os.getcwd()
            os.chdir(self.source_files_prefix)

        yield

        if self.source_files_prefix is not None:
            os.chdir(start_dir)

    def major_version_from_source(self):
        with open(self.get_source_file_path("lua.h")) as lua_h:
            for line in lua_h:
                match = re.match(r"^\s*#define\s+LUA_VERSION_NUM\s+50(\d)\s*$", line)

                if match:
                    return "5." + match.group(1)

        sys.exit("Error: couldn't infer Lua major version from lua.h")

    def set_identifiers(self):
        super(Lua, self).set_identifiers()

        self.identifiers["target"] = opts.target
        self.identifiers["compat"] = self.compat
        self.identifiers["c flags"] = opts.cflags or ""
        self.identifiers["location"] = opts.location
        self.identifiers["major version"] = self.major_version

        if using_cl():
            cl_help = get_output("cl")
            cl_version = re.search(r"(1[56789])\.\d+", cl_help)
            cl_arch = re.search(r"(x(?:86)|(?:64))", cl_help)

            if not cl_version or not cl_arch:
                sys.exit("Error: couldn't determine cl.exe version and architecture")

            cl_version = cl_version.group(1)
            cl_arch = cl_arch.group(1)

            self.identifiers["vs year"] = cl_version_to_vs_year[cl_version]
            self.identifiers["vs arch"] = cl_arch

    def add_options_to_version_suffix(self):
        options = []

        if os.name == "nt" or opts.target != get_default_lua_target():
            options.append(("target", opts.target))

        if self.compat != "default":
            options.append(("compat", self.compat))

        if opts.cflags is not None:
            options.append(("cflags", opts.cflags))

        if opts.no_readline:
            options.append(("readline", "false"))

        if options:
            self.version_suffix += " (" + (", ".join(
                opt + ": " + value for opt, value in options)) + ")"

    def set_package_paths(self):
        local_paths_first = self.major_version == "5.1"

        module_path = os.path.join(opts.location, "share", "lua", self.major_version)
        module_path_parts = [
            os.path.join(module_path, "?.lua"),
            os.path.join(module_path, "?", "init.lua")
        ]
        module_path_parts.insert(0 if local_paths_first else 2, os.path.join(".", "?.lua"))

        if self.major_version in ["5.3", "5.4"]:
            module_path_parts.append(os.path.join(".", "?", "init.lua"))

        self.package_path = ";".join(module_path_parts)

        cmodule_path = os.path.join(opts.location, "lib", "lua", self.major_version)
        so_extension = ".dll" if os.name == "nt" else ".so"
        cmodule_path_parts = [
            os.path.join(cmodule_path, "?" + so_extension),
            os.path.join(cmodule_path, "loadall" + so_extension)
        ]
        cmodule_path_parts.insert(0 if local_paths_first else 2,
                                  os.path.join(".", "?" + so_extension))
        self.package_cpath = ";".join(cmodule_path_parts)

    def add_package_paths_redefines(self):
        package_path = self.package_path.replace("\\", "\\\\").replace('"', '\\"')
        package_cpath = self.package_cpath.replace("\\", "\\\\").replace('"', '\\"')
        self.redefines.extend([
            "#undef LUA_PATH_DEFAULT",
            "#undef LUA_CPATH_DEFAULT",
            "#define LUA_PATH_DEFAULT \"{}\"".format(package_path),
            "#define LUA_CPATH_DEFAULT \"{}\"".format(package_cpath)
        ])

    def patch_redefines(self):
        luaconf_path = self.get_source_file_path("luaconf.h")
        redefines = "\n".join(self.redefines)

        with open(luaconf_path, "rb") as luaconf_h:
            luaconf_src = luaconf_h.read()

        body, _, tail = luaconf_src.rpartition(b"#endif")

        with open(luaconf_path, "wb") as luaconf_h:
            luaconf_h.write(body)
            luaconf_h.write(redefines.encode("UTF-8"))
            luaconf_h.write(b"\n#endif")
            luaconf_h.write(tail)

    def build(self):
        if opts.builds and self.source != "local":
            self.cached_build_path = os.path.join(opts.builds,
                                                  hash_identifiers(self.identifiers))

            if os.path.exists(self.cached_build_path):
                print("Building " + self.title + self.version_suffix + " (cached)")
                os.chdir(self.cached_build_path)
                return
        else:
            self.cached_build_path = None

        self.fetch()
        print("Building " + self.title + self.version_suffix)
        self.patch_redefines()
        self.make()

        if self.cached_build_path is not None:
            copy_dir(".", self.cached_build_path)

    def install(self):
        print("Installing " + self.title + self.version_suffix)
        self.make_install()

class PatchError(Exception):
    pass

class LineScanner(object):
    def __init__(self, lines):
        self.lines = lines
        self.line_number = 1

    def consume_line(self):
        if self.line_number > len(self.lines):
            raise PatchError("source is too short")
        else:
            self.line_number += 1
            return self.lines[self.line_number - 2]

class Hunk(object):
    def __init__(self, start_line, lines):
        self.start_line = start_line
        self.lines = lines

    def add_new_lines(self, old_lines_scanner, new_lines):
        while old_lines_scanner.line_number < self.start_line:
            new_lines.append(old_lines_scanner.consume_line())

        for line in self.lines:
            first_char, rest = line[0], line[1:]

            if first_char in " -":
                # Deleting or copying a line: it must match what's in the diff.
                if rest != old_lines_scanner.consume_line():
                    raise PatchError("source is different")

            if first_char in " +":
                # Adding or copying a line: add it to the line list.
                new_lines.append(rest)

class FilePatch(object):
    def __init__(self, file_name, lines):
        self.file_name = file_name
        self.hunks = []
        self.new_lines = []
        hunk_lines = None
        start_line = None

        for line in lines:
            first_char = line[0]

            if first_char == "@":
                if start_line is not None:
                    self.hunks.append(Hunk(start_line, hunk_lines))

                match = re.match(r"^@@ \-(\d+)", line)
                start_line = int(match.group(1))
                hunk_lines = []
            else:
                hunk_lines.append(line)

        if start_line is not None:
            self.hunks.append(Hunk(start_line, hunk_lines))

    def prepare_application(self):
        if not os.path.exists(self.file_name):
            raise PatchError("{} doesn't exist".format(self.file_name))

        with open(self.file_name, "r") as handler:
            source = handler.read()

        old_lines = source.splitlines()
        old_lines_scanner = LineScanner(old_lines)

        for hunk in self.hunks:
            hunk.add_new_lines(old_lines_scanner, self.new_lines)

        while old_lines_scanner.line_number <= len(old_lines):
            self.new_lines.append(old_lines_scanner.consume_line())

        self.new_lines.append("")

    def apply(self):
        with open(self.file_name, "wb") as handler:
            handler.write("\n".join(self.new_lines).encode("UTF-8"))

class Patch(object):
    def __init__(self, src):
        # The first and the last lines are empty.
        lines = textwrap.dedent(src[1:-1]).splitlines()
        lines = [line if line else " " for line in lines]
        self.file_patches = []
        file_lines = None
        file_name = None

        for line in lines:
            match = re.match(r"^([\w\.]+):$", line)

            if match:
                if file_name is not None:
                    self.file_patches.append(FilePatch(file_name, file_lines))

                file_name = match.group(1)
                file_lines = []
            else:
                file_lines.append(line)

        if file_name is not None:
            self.file_patches.append(FilePatch(file_name, file_lines))

    def apply(self):
        try:
            for file_patch in self.file_patches:
                file_patch.prepare_application()
        except PatchError as e:
            return e.args[0]

        for file_patch in self.file_patches:
            file_patch.apply()

class RioLua(Lua):
    name = "lua"
    title = "Lua"
    base_download_urls = ["https://www.lua.org/ftp", "https://webserver2.tecgraf.puc-rio.br/lua/mirror/ftp"]
    work_base_download_url = "https://www.lua.org/work"
    default_repo = "https://github.com/lua/lua"
    versions = [
        "5.1", "5.1.1", "5.1.2", "5.1.3", "5.1.4", "5.1.5",
        "5.2.0", "5.2.1", "5.2.2", "5.2.3", "5.2.4",
        "5.3.0", "5.3.1", "5.3.2", "5.3.3", "5.3.4", "5.3.5", "5.3.6",
        "5.4.0", "5.4.1", "5.4.2", "5.4.3", "5.4.4", "5.4.5", "5.4.6", "5.4.7", "5.4.8",
        "5.5.0-rc1"
    ]
    translations = {
        "5": "5.4.8",
        "5.1": "5.1.5",
        "5.1.0": "5.1",
        "5.2": "5.2.4",
        "5.3": "5.3.6",
        "5.4": "5.4.8",
        "5.5": "5.5.0-rc1",
        "5.5.0": "5.5.0-rc1",
        "^": "5.4.8",
        "latest": "5.4.8"
    }
    checksums = {
        "lua-5.1.tar.gz"        : "7f5bb9061eb3b9ba1e406a5aa68001a66cb82bac95748839dc02dd10048472c1",
        "lua-5.1.1.tar.gz"      : "c5daeed0a75d8e4dd2328b7c7a69888247868154acbda69110e97d4a6e17d1f0",
        "lua-5.1.2.tar.gz"      : "5cf098c6fe68d3d2d9221904f1017ff0286e4a9cc166a1452a456df9b88b3d9e",
        "lua-5.1.3.tar.gz"      : "6b5df2edaa5e02bf1a2d85e1442b2e329493b30b0c0780f77199d24f087d296d",
        "lua-5.1.4.tar.gz"      : "b038e225eaf2a5b57c9bcc35cd13aa8c6c8288ef493d52970c9545074098af3a",
        "lua-5.1.5.tar.gz"      : "2640fc56a795f29d28ef15e13c34a47e223960b0240e8cb0a82d9b0738695333",
        "lua-5.2.0.tar.gz"      : "cabe379465aa8e388988073d59b69e76ba0025429d2c1da80821a252cdf6be0d",
        "lua-5.2.1.tar.gz"      : "64304da87976133196f9e4c15250b70f444467b6ed80d7cfd7b3b982b5177be5",
        "lua-5.2.2.tar.gz"      : "3fd67de3f5ed133bf312906082fa524545c6b9e1b952e8215ffbd27113f49f00",
        "lua-5.2.3.tar.gz"      : "13c2fb97961381f7d06d5b5cea55b743c163800896fd5c5e2356201d3619002d",
        "lua-5.2.4.tar.gz"      : "b9e2e4aad6789b3b63a056d442f7b39f0ecfca3ae0f1fc0ae4e9614401b69f4b",
        "lua-5.3.0.tar.gz"      : "ae4a5eb2d660515eb191bfe3e061f2b8ffe94dce73d32cfd0de090ddcc0ddb01",
        "lua-5.3.1.tar.gz"      : "072767aad6cc2e62044a66e8562f51770d941e972dc1e4068ba719cd8bffac17",
        "lua-5.3.2.tar.gz"      : "c740c7bb23a936944e1cc63b7c3c5351a8976d7867c5252c8854f7b2af9da68f",
        "lua-5.3.3.tar.gz"      : "5113c06884f7de453ce57702abaac1d618307f33f6789fa870e87a59d772aca2",
        "lua-5.3.4.tar.gz"      : "f681aa518233bc407e23acf0f5887c884f17436f000d453b2491a9f11a52400c",
        "lua-5.3.5.tar.gz"      : "0c2eed3f960446e1a3e4b9a1ca2f3ff893b6ce41942cf54d5dd59ab4b3b058ac",
        "lua-5.3.6.tar.gz"      : "fc5fd69bb8736323f026672b1b7235da613d7177e72558893a0bdcd320466d60",
        "lua-5.4.0.tar.gz"      : "eac0836eb7219e421a96b7ee3692b93f0629e4cdb0c788432e3d10ce9ed47e28",
        "lua-5.4.1.tar.gz"      : "4ba786c3705eb9db6567af29c91a01b81f1c0ac3124fdbf6cd94bdd9e53cca7d",
        "lua-5.4.2.tar.gz"      : "11570d97e9d7303c0a59567ed1ac7c648340cd0db10d5fd594c09223ef2f524f",
        "lua-5.4.3.tar.gz"      : "f8612276169e3bfcbcfb8f226195bfc6e466fe13042f1076cbde92b7ec96bbfb",
        "lua-5.4.4.tar.gz"      : "164c7849653b80ae67bec4b7473b884bf5cc8d2dca05653475ec2ed27b9ebf61",
        "lua-5.4.5.tar.gz"      : "59df426a3d50ea535a460a452315c4c0d4e1121ba72ff0bdde58c2ef31d6f444",
        "lua-5.4.6.tar.gz"      : "7d5ea1b9cb6aa0b59ca3dde1c6adcb57ef83a1ba8e5432c0ecd06bf439b3ad88",
        "lua-5.4.7.tar.gz"      : "9fbf5e28ef86c69858f6d3d34eccc32e911c1a28b4120ff3e84aaa70cfbf1e30",
        "lua-5.4.8.tar.gz"      : "4f18ddae154e793e46eeab727c59ef1c0c0c2b744e7b94219710d76f530629ae",
        "lua-5.5.0-rc1.tar.gz"  : "34a4dcca0c04877fbce4baff54054b9793b70bca5d8c676ca4d3504dd47c3772",
    }
    all_patches = {
        "When loading a file, Lua may call the reader function again after it returned end of input": """
            lzio.h:
            @@ -59,6 +59,7 @@
               lua_Reader reader;
               void* data;\t\t\t/* additional data */
               lua_State *L;\t\t\t/* Lua state (for reader) */
            +  int eoz;\t\t\t/* true if reader has no more data */
             };


            lzio.c:
            @@ -22,10 +22,14 @@
               size_t size;
               lua_State *L = z->L;
               const char *buff;
            +  if (z->eoz) return EOZ;
               lua_unlock(L);
               buff = z->reader(L, z->data, &size);
               lua_lock(L);
            -  if (buff == NULL || size == 0) return EOZ;
            +  if (buff == NULL || size == 0) {
            +    z->eoz = 1;  /* avoid calling reader function next time */
            +    return EOZ;
            +  }
               z->n = size - 1;
               z->p = buff;
               return char2int(*(z->p++));
            @@ -51,6 +55,7 @@
               z->data = data;
               z->n = 0;
               z->p = NULL;
            +  z->eoz = 0;
             }
        """,
        "Metatable may access its own deallocated field when it has a self reference in __newindex": """
            lvm.c:
            @@ -190,18 +190,19 @@
               for (loop = 0; loop < MAXTAGLOOP; loop++) {
                 const TValue *tm;
                 if (oldval != NULL) {
            -      lua_assert(ttistable(t) && ttisnil(oldval));
            +      Table *h = hvalue(t);  /* save 't' table */
            +      lua_assert(ttisnil(oldval));
                   /* must check the metamethod */
            -      if ((tm = fasttm(L, hvalue(t)->metatable, TM_NEWINDEX)) == NULL &&
            +      if ((tm = fasttm(L, h->metatable, TM_NEWINDEX)) == NULL &&
                      /* no metamethod; is there a previous entry in the table? */
                      (oldval != luaO_nilobject ||
                      /* no previous entry; must create one. (The next test is
                         always true; we only need the assignment.) */
            -         (oldval = luaH_newkey(L, hvalue(t), key), 1))) {
            +         (oldval = luaH_newkey(L, h, key), 1))) {
                     /* no metamethod and (now) there is an entry with given key */
                     setobj2t(L, cast(TValue *, oldval), val);
            -        invalidateTMcache(hvalue(t));
            -        luaC_barrierback(L, hvalue(t), val);
            +        invalidateTMcache(h);
            +        luaC_barrierback(L, h, val);
                     return;
                   }
                   /* else will try the metamethod */
        """,
        "Label between local definitions can mix-up their initializations": """
            lparser.c:
            @@ -1226,7 +1226,7 @@
               checkrepeated(fs, ll, label);  /* check for repeated labels */
               checknext(ls, TK_DBCOLON);  /* skip double colon */
               /* create new entry for this label */
            -  l = newlabelentry(ls, ll, label, line, fs->pc);
            +  l = newlabelentry(ls, ll, label, line, luaK_getlabel(fs));
               skipnoopstat(ls);  /* skip other no-op statements */
               if (block_follow(ls, 0)) {  /* label is last no-op statement in the block? */
                 /* assume that locals are already out of scope */
        """,
        "gmatch iterator fails when called from a coroutine different from the one that created it": """
            lstrlib.c:
            @@ -688,6 +688,7 @@
             static int gmatch_aux (lua_State *L) {
               GMatchState *gm = (GMatchState *)lua_touserdata(L, lua_upvalueindex(3));
               const char *src;
            +  gm->ms.L = L;
               for (src = gm->src; src <= gm->ms.src_end; src++) {
                 const char *e;
                 reprepstate(&gm->ms);
        """,
        "Expression list with four or more expressions in a 'for' loop can crash the interpreter": """
            lparser.c:
            @@ -323,6 +323,8 @@
                   luaK_nil(fs, reg, extra);
                 }
               }
            +  if (nexps > nvars)
            +    ls->fs->freereg -= nexps - nvars;  /* remove extra values */
             }


            @@ -1160,11 +1162,8 @@
                 int nexps;
                 checknext(ls, '=');
                 nexps = explist(ls, &e);
            -    if (nexps != nvars) {
            +    if (nexps != nvars)
                   adjust_assign(ls, nvars, nexps, &e);
            -      if (nexps > nvars)
            -        ls->fs->freereg -= nexps - nvars;  /* remove extra values */
            -    }
                 else {
                   luaK_setoneret(ls->fs, &e);  /* close last expression */
                   luaK_storevar(ls->fs, &lh->v, &e);
        """,
        "Checking a format for os.date may read past the format string": """
            loslib.c:
            @@ -263,1 +263,2 @@
            -  for (option = LUA_STRFTIMEOPTIONS; *option != '\\0'; option += oplen) {
            +  int convlen = (int)strlen(conv);
            +  for (option = LUA_STRFTIMEOPTIONS; *option != '\\0' && oplen <= convlen; option += oplen) {
        """,
        "Lua can generate wrong code in functions with too many constants": """
            lcode.c:
            @@ -1017,8 +1017,8 @@
             */
             static void codebinexpval (FuncState *fs, OpCode op,
                                        expdesc *e1, expdesc *e2, int line) {
            -  int rk1 = luaK_exp2RK(fs, e1);  /* both operands are "RK" */
            -  int rk2 = luaK_exp2RK(fs, e2);
            +  int rk2 = luaK_exp2RK(fs, e2);  /* both operands are "RK" */
            +  int rk1 = luaK_exp2RK(fs, e1);
               freeexps(fs, e1, e2);
               e1->u.info = luaK_codeABC(fs, op, 0, rk1, rk2);  /* generate opcode */
               e1->k = VRELOCABLE;  /* all those operations are relocatable */
        """,
        "Wrong code generated for a 'goto' followed by a label inside an 'if'": """
            lparser.c:
            @@ -1392,7 +1392,7 @@
                 luaK_goiffalse(ls->fs, &v);  /* will jump to label if condition is true */
                 enterblock(fs, &bl, 0);  /* must enter block before 'goto' */
                 gotostat(ls, v.t);  /* handle goto/break */
            -    skipnoopstat(ls);  /* skip other no-op statements */
            +    while (testnext(ls, ';')) {}  /* skip semicolons */
                 if (block_follow(ls, 0)) {  /* 'goto' is the entire block? */
                   leaveblock(fs);
                   return;  /* and that is it */
        """,
        "Lua does not check GC when creating error messages": """
            ldebug.c:
            @@ -653,6 +653,7 @@
               CallInfo *ci = L->ci;
               const char *msg;
               va_list argp;
            +  luaC_checkGC(L);  /* error message uses memory */
               va_start(argp, fmt);
               msg = luaO_pushvfstring(L, fmt, argp);  /* format message */
               va_end(argp);
        """,
        "Dead keys with nil values can stay in weak tables": """
            lgc.c:
            @@ -643,8 +643,9 @@
                 for (n = gnode(h, 0); n < limit; n++) {
                   if (!ttisnil(gval(n)) && (iscleared(g, gkey(n)))) {
                     setnilvalue(gval(n));  /* remove value ... */
            -        removeentry(n);  /* and remove entry from table */
                   }
            +      if (ttisnil(gval(n)))  /* is entry empty? */
            +        removeentry(n);  /* remove entry from table */
                 }
               }
             }
        """,
        "lua_pushcclosure should not call the garbage collector when n is zero": """
            lapi.c:
            @@ -533,6 +533,7 @@
               lua_lock(L);
               if (n == 0) {
                 setfvalue(L->top, fn);
            +    api_incr_top(L);
               }
               else {
                 CClosure *cl;
            @@ -546,9 +547,9 @@
                   /* does not need barrier because closure is white */
                 }
                 setclCvalue(L, L->top, cl);
            +    api_incr_top(L);
            +    luaC_checkGC(L);
               }
            -  api_incr_top(L);
            -  luaC_checkGC(L);
               lua_unlock(L);
             }
        """,
        "Lua crashes when building sequences with more than 2^30 elements": """
            ltable.c:
            @@ -223,7 +223,9 @@
               unsigned int na = 0;  /* number of elements to go to array part */
               unsigned int optimal = 0;  /* optimal size for array part */
               /* loop while keys can fill more than half of total size */
            -  for (i = 0, twotoi = 1; *pna > twotoi / 2; i++, twotoi *= 2) {
            +  for (i = 0, twotoi = 1;
            +       twotoi > 0 && *pna > twotoi / 2;
            +       i++, twotoi *= 2) {
                 if (nums[i] > 0) {
                   a += nums[i];
                   if (a > twotoi/2) {  /* more than half elements present? */
        """,
        "Table length computation overflows for sequences larger than 2^31 elements": """
            ltable.h:
            @@ -56,3 +56,3 @@
             LUAI_FUNC int luaH_next (lua_State *L, Table *t, StkId key);
            -LUAI_FUNC int luaH_getn (Table *t);
            +LUAI_FUNC lua_Unsigned luaH_getn (Table *t);

            ltable.c:
            @@ -614,4 +614,4 @@

            -static int unbound_search (Table *t, unsigned int j) {
            -  unsigned int i = j;  /* i is zero or a present index */
            +static lua_Unsigned unbound_search (Table *t, lua_Unsigned j) {
            +  lua_Unsigned i = j;  /* i is zero or a present index */
               j++;
            @@ -620,3 +620,3 @@
                 i = j;
            -    if (j > cast(unsigned int, MAX_INT)/2) {  /* overflow? */
            +    if (j > l_castS2U(LUA_MAXINTEGER) / 2) {  /* overflow? */
                   /* table was built with bad purposes: resort to linear search */
            @@ -630,3 +630,3 @@
               while (j - i > 1) {
            -    unsigned int m = (i+j)/2;
            +    lua_Unsigned m = (i+j)/2;
                 if (ttisnil(luaH_getint(t, m))) j = m;
            @@ -642,3 +642,3 @@
             */
            -int luaH_getn (Table *t) {
            +lua_Unsigned luaH_getn (Table *t) {
               unsigned int j = t->sizearray;
        """,
        "Memory-allocation error when resizing a table can leave it in an inconsistent state":
        """
            ltable.c:
            @@ -332,17 +332,34 @@
             }


            +typedef struct {
            +  Table *t;
            +  unsigned int nhsize;
            +} AuxsetnodeT;
            +
            +
            +static void auxsetnode (lua_State *L, void *ud) {
            +  AuxsetnodeT *asn = cast(AuxsetnodeT *, ud);
            +  setnodevector(L, asn->t, asn->nhsize);
            +}
            +
            +
             void luaH_resize (lua_State *L, Table *t, unsigned int nasize,
                                                       unsigned int nhsize) {
               unsigned int i;
               int j;
            +  AuxsetnodeT asn;
               unsigned int oldasize = t->sizearray;
               int oldhsize = allocsizenode(t);
               Node *nold = t->node;  /* save old hash ... */
               if (nasize > oldasize)  /* array part must grow? */
                 setarrayvector(L, t, nasize);
               /* create new hash part with appropriate size */
            -  setnodevector(L, t, nhsize);
            +  asn.t = t; asn.nhsize = nhsize;
            +  if (luaD_rawrunprotected(L, auxsetnode, &asn) != LUA_OK) {  /* mem. error? */
            +    setarrayvector(L, t, oldasize);  /* array back to its original size */
            +    luaD_throw(L, LUA_ERRMEM);  /* rethrow memory error */
            +  }
               if (nasize < oldasize) {  /* array part must shrink? */
                 t->sizearray = nasize;
                 /* re-insert elements from vanishing slice */
        """,
        "Joining an upvalue with itself can cause a use-after-free crash": """
            lapi.c:
            @@ -1289,6 +1289,8 @@
               LClosure *f1;
               UpVal **up1 = getupvalref(L, fidx1, n1, &f1);
               UpVal **up2 = getupvalref(L, fidx2, n2, NULL);
            +  if (*up1 == *up2)
            +    return;
               luaC_upvdeccount(L, *up1);
               *up1 = *up2;
               (*up1)->refcount++;
        """,
        "Old finalized object may not be visited by GC": """
            lgc.c:
            @@ -1140,7 +1140,7 @@ static void finishgencycle (lua_State *L, global_State *g) {
             static void youngcollection (lua_State *L, global_State *g) {
               GCObject **psurvival;  /* to point to first non-dead survival object */
               lua_assert(g->gcstate == GCSpropagate);
            -  markold(g, g->survival, g->reallyold);
            +  markold(g, g->allgc, g->reallyold);
               markold(g, g->finobj, g->finobjrold);
               atomic(L);
        """,
        "Computation of stack limit when entering a coroutine is wrong": """
            ldo.c:
            @@ -674,7 +674,7 @@ LUA_API int lua_resume (lua_State *L, lua_State *from, int nargs,
               if (from == NULL)
                 L->nCcalls = CSTACKTHREAD;
               else  /* correct 'nCcalls' for this thread */
            -    L->nCcalls = getCcalls(from) + from->nci - L->nci - CSTACKCF;
            +    L->nCcalls = getCcalls(from) - L->nci - CSTACKCF;
               if (L->nCcalls <= CSTACKERR)
                 return resume_error(L, "C stack overflow", nargs);
               luai_userstateresume(L, nargs);
        """,
        "An emergency collection when handling an error while loading the upvalues of a function can cause a segfault": """
            lundump.c:
            @@ -205,8 +205,9 @@ static void loadUpvalues (LoadState *S, Proto *f) {
               n = loadInt(S);
               f->upvalues = luaM_newvectorchecked(S->L, n, Upvaldesc);
               f->sizeupvalues = n;
            -  for (i = 0; i < n; i++) {
            +  for (i = 0; i < n; i++)
                 f->upvalues[i].name = NULL;
            +  for (i = 0; i < n; i++) {
                 f->upvalues[i].instack = loadByte(S);
                 f->upvalues[i].idx = loadByte(S);
                 f->upvalues[i].kind = loadByte(S);
        """,
        "'checkstackp' can run a GC step and destroy a preallocated CallInfo": """
            ldo.c:
            @@ -466,13 +466,13 @@ void luaD_call (lua_State *L, StkId func, int nresults) {
                   f = fvalue(s2v(func));
                  Cfunc: {
                   int n;  /* number of returns */
            -      CallInfo *ci = next_ci(L);
            +      CallInfo *ci;
                   checkstackp(L, LUA_MINSTACK, func);  /* ensure minimum stack size */
            +      L->ci = ci = next_ci(L);
                   ci->nresults = nresults;
                   ci->callstatus = CIST_C;
                   ci->top = L->top + LUA_MINSTACK;
                   ci->func = func;
            -      L->ci = ci;
                   lua_assert(ci->top <= L->stack_last);
                   if (L->hookmask & LUA_MASKCALL) {
                     int narg = cast_int(L->top - func) - 1;
            @@ -486,18 +486,18 @@ void luaD_call (lua_State *L, StkId func, int nresults) {
                   break;
                 }
                 case LUA_VLCL: {  /* Lua function */
            -      CallInfo *ci = next_ci(L);
            +      CallInfo *ci;
                   Proto *p = clLvalue(s2v(func))->p;
                   int narg = cast_int(L->top - func) - 1;  /* number of real arguments */
                   int nfixparams = p->numparams;
                   int fsize = p->maxstacksize;  /* frame size */
                   checkstackp(L, fsize, func);
            +      L->ci = ci = next_ci(L);
                   ci->nresults = nresults;
                   ci->u.l.savedpc = p->code;  /* starting point */
                   ci->callstatus = 0;
                   ci->top = func + 1 + fsize;
                   ci->func = func;
            -      L->ci = ci;
                   for (; narg < nfixparams; narg++)
                     setnilvalue(s2v(L->top++));  /* complete missing arguments */
                   lua_assert(ci->top <= L->stack_last);
        """,
        "GC after resizing stack can shrink it again": """
            ldo.h:
            @@ -44,7 +44,7 @@

             /* macro to check stack size and GC */
             #define checkstackGC(L,fsize)  \\
            -	luaD_checkstackaux(L, (fsize), (void)0, luaC_checkGC(L))
            +	luaD_checkstackaux(L, (fsize), luaC_checkGC(L), (void)0)


             /* type of protected functions, to be ran by 'runprotected' */
        """,
        "Errors in finalizers need a valid 'pc' to produce an error message": """
            lvm.c:
            @@ -1104,7 +1104,7 @@ void luaV_finishOp (lua_State *L) {


             #define checkGC(L,c)  \\
            -	{ luaC_condGC(L, L->top = (c),  /* limit of live values */ \\
            +	{ luaC_condGC(L, (savepc(L), L->top = (c)), \\
                                      updatetrap(ci)); \\
                        luai_threadyield(L); }

            @@ -1792,8 +1792,7 @@ void luaV_execute (lua_State *L, CallInfo *ci) {
                     vmbreak;
                   }
                   vmcase(OP_VARARGPREP) {
            -        luaT_adjustvarargs(L, GETARG_A(i), ci, cl->p);
            -        updatetrap(ci);
            +        ProtectNT(luaT_adjustvarargs(L, GETARG_A(i), ci, cl->p));
                     if (trap) {
                       luaD_hookcall(L, ci);
                       L->oldpc = pc + 1;  /* next opcode will be seen as a "new" line */
        """,
        "'popen' can crash if called with an invalid mode": """
            liolib.c:
            @@ -279,6 +279,8 @@ static int io_popen (lua_State *L) {
               const char *filename = luaL_checkstring(L, 1);
               const char *mode = luaL_optstring(L, 2, "r");
               LStream *p = newprefile(L);
            +  luaL_argcheck(L, ((mode[0] == 'r' || mode[0] == 'w') && mode[1] == '\0'),
            +                   2, "invalid mode");
               p->f = l_popen(L, filename, mode);
               p->closef = &io_pclose;
               return (p->f == NULL) ? luaL_fileresult(L, 0, filename) : 1;
        """,
        "Field 'L->oldpc' is not always updated when returning to a function": """
            lgc.c:
            @@ -856,6 +856,8 @@ static void GCTM (lua_State *L) {
                 if (unlikely(status != LUA_OK)) {  /* error while running __gc? */
                   luaE_warnerror(L, "__gc metamethod");
                   L->top--;  /* pops error object */
            +      if (isLua(L->ci))
            +        L->oldpc = L->ci->u.l.savedpc;  /* update 'oldpc' */
                 }
               }
             }
        """,
        "Parameter 'what' of 'debug.getinfo' cannot start with '>'": """
            ldblib.c:
            @@ -152,6 +152,7 @@ static int db_getinfo (lua_State *L) {
               lua_State *L1 = getthread(L, &arg);
               const char *options = luaL_optstring(L, arg+2, "flnSrtu");
               checkstack(L, L1, 3);
            +  luaL_argcheck(L, options[0] != '>', arg + 2, "invalid option '>'");
               if (lua_isfunction(L, arg + 1)) {  /* info about a function? */
                 options = lua_pushfstring(L, ">%s", options);  /* add '>' to 'options' */
                 lua_pushvalue(L, arg + 1);  /* move function to 'L1' stack */
        """,
        "Error message in 'string.concat' uses wrong format": """
            ltablib.c:
            @@ -146,7 +146,7 @@ static int tmove (lua_State *L) {
             static void addfield (lua_State *L, luaL_Buffer *b, lua_Integer i) {
               lua_geti(L, 1, i);
               if (!lua_isstring(L, -1))
            -    luaL_error(L, "invalid value (%s) at index %d in table for 'concat'",
            +    luaL_error(L, "invalid value (%s) at index %I in table for 'concat'",
                               luaL_typename(L, -1), i);
               luaL_addvalue(b);
             }
        """,
        "C99 comments are not compatible with C89": """
            lvm.c:
            @@ -1156,8 +1156,10 @@ void luaV_execute (lua_State *L, CallInfo *ci) {
                 Instruction i;  /* instruction being executed */
                 StkId ra;  /* instruction's A register */
                 vmfetch();
            -// low-level line tracing for debugging Lua
            -// printf("line: %d\\n", luaG_getfuncline(cl->p, pcRel(pc, cl->p)));
            +    #if 0
            +      /* low-level line tracing for debugging Lua */
            +      printf("line: %d\\n", luaG_getfuncline(cl->p, pcRel(pc, cl->p)));
            +    #endif
                 lua_assert(base == ci->func + 1);
                 lua_assert(base <= L->top && L->top < L->stack_last);
                 /* invalidate top for instructions not expecting it */
        """,
        "Yielding in a __close metamethod called when returning vararg results mess up the returned values": """
            lvm.c:
            @@ -847,10 +847,19 @@ void luaV_finishOp (lua_State *L) {
                   luaV_concat(L, total);  /* concat them (may yield again) */
                   break;
                 }
            -    case OP_CLOSE:  case OP_RETURN: {  /* yielded closing variables */
            +    case OP_CLOSE: {  /* yielded closing variables */
                   ci->u.l.savedpc--;  /* repeat instruction to close other vars. */
                   break;
                 }
            +    case OP_RETURN: {  /* yielded closing variables */
            +      StkId ra = base + GETARG_A(inst);
            +      /* correct top to signal correct number of returns (in case the
            +         return is "in top" */
            +      L->top = ra + ci->u2.nres;
            +      /* repeat instruction to close other vars. and complete the return */
            +      ci->u.l.savedpc--;
            +      break;
            +    }
                 default: {
                   /* only these other opcodes can yield */
                   lua_assert(op == OP_TFORCALL || op == OP_CALL ||
            @@ -1672,6 +1681,7 @@ void luaV_execute (lua_State *L, CallInfo *ci) {
                       n = cast_int(L->top - ra);  /* get what is available */
                     savepc(ci);
                     if (TESTARG_k(i)) {  /* may there be open upvalues? */
            +          ci->u2.nres = n;  /* save number of returns */
                       if (L->top < ci->top)
                         L->top = ci->top;
        """,
        "'luaL_tolstring' may get confused with negative indices": """
            lauxlib.c:
            @@ -881,6 +881,7 @@ LUALIB_API lua_Integer luaL_len (lua_State *L, int idx) {


             LUALIB_API const char *luaL_tolstring (lua_State *L, int idx, size_t *len) {
            +  idx = lua_absindex(L,idx);
               if (luaL_callmeta(L, idx, "__tostring")) {  /* metafield? */
                 if (!lua_isstring(L, -1))
                   luaL_error(L, "'__tostring' must return a string");
        """,
        "negation in macro 'luaV_shiftr' may overflow": """
            lvm.c:
            @@ -766,7 +766,7 @@ lua_Number luaV_modf (lua_State *L, lua_Number m, lua_Number n) {
             /*
             ** Shift left operation. (Shift right just negates 'y'.)
             */
            -#define luaV_shiftr(x,y)	luaV_shiftl(x,-(y))
            +#define luaV_shiftr(x,y)	luaV_shiftl(x,intop(-, 0, y))

             lua_Integer luaV_shiftl (lua_Integer x, lua_Integer y) {
               if (y < 0) {  /* shift right? */
        """,
        "Lua can generate wrong code when _ENV is <const>": """
            lparser.c:
            @@ -468,6 +468,7 @@ static void singlevar (LexState *ls, expdesc *var) {
                 expdesc key;
                 singlevaraux(fs, ls->envn, var, 1);  /* get environment variable */
                 lua_assert(var->k != VVOID);  /* this one must exist */
            +    luaK_exp2anyregup(fs, var);  /* but could be a constant */
                 codestring(&key, varname);  /* key is variable name */
                 luaK_indexed(fs, var, &key);  /* env[varname] */
               }
        """,
        "Wrong code generation for constants in bitwise operations": """
            lcode.c:
            @@ -1391,7 +1391,10 @@ static void finishbinexpval (FuncState *fs, expdesc *e1, expdesc *e2,
             */
             static void codebinexpval (FuncState *fs, OpCode op,
                                        expdesc *e1, expdesc *e2, int line) {
            -  int v2 = luaK_exp2anyreg(fs, e2);  /* both operands are in registers */
            +  int v2 = luaK_exp2anyreg(fs, e2);  /* make sure 'e2' is in a register */
            +  /* 'e1' must be already in a register or it is a constant */
            +  lua_assert((VNIL <= e1->k && e1->k <= VKSTR) ||
            +             e1->k == VNONRELOC || e1->k == VRELOC);
               lua_assert(OP_ADD <= op && op <= OP_SHR);
               finishbinexpval(fs, e1, e2, op, v2, 0, line, OP_MMBIN,
                               cast(TMS, (op - OP_ADD) + TM_ADD));
            @@ -1478,7 +1481,7 @@ static void codecommutative (FuncState *fs, BinOpr op,


             /*
            -** Code bitwise operations; they are all associative, so the function
            +** Code bitwise operations; they are all commutative, so the function
             ** tries to put an integer constant as the 2nd operand (a K operand).
             */
             static void codebitwise (FuncState *fs, BinOpr opr,
            @@ -1486,11 +1489,11 @@ static void codebitwise (FuncState *fs, BinOpr opr,
               int flip = 0;
               int v2;
               OpCode op;
            -  if (e1->k == VKINT && luaK_exp2RK(fs, e1)) {
            +  if (e1->k == VKINT && luaK_exp2K(fs, e1)) {
                 swapexps(e1, e2);  /* 'e2' will be the constant operand */
                 flip = 1;
               }
            -  else if (!(e2->k == VKINT && luaK_exp2RK(fs, e2))) {  /* no constants? */
            +  else if (!(e2->k == VKINT && luaK_exp2K(fs, e2))) {  /* no constants? */
                 op = cast(OpCode, opr + OP_ADD);
                 codebinexpval(fs, op, e1, e2, line);  /* all-register opcodes */
                 return;
            @@ -1551,7 +1554,7 @@ static void codeeq (FuncState *fs, BinOpr opr, expdesc *e1, expdesc *e2) {
                 op = OP_EQI;
                 r2 = im;  /* immediate operand */
               }
            -  else if (luaK_exp2RK(fs, e2)) {  /* 1st expression is constant? */
            +  else if (luaK_exp2RK(fs, e2)) {  /* 2nd expression is constant? */
                 op = OP_EQK;
                 r2 = e2->u.info;  /* constant index */
               }
            @@ -1611,7 +1614,8 @@ void luaK_infix (FuncState *fs, BinOpr op, expdesc *v) {
                 case OPR_SHL: case OPR_SHR: {
                   if (!tonumeral(v, NULL))
                     luaK_exp2anyreg(fs, v);
            -      /* else keep numeral, which may be folded with 2nd operand */
            +      /* else keep numeral, which may be folded or used as an immediate
            +         operand */
                   break;
                 }
                 case OPR_EQ: case OPR_NE: {
        """,
        "Lua-stack overflow when C stack overflows while handling an error": """
            ldebug.c:
            @@ -824,8 +824,11 @@ l_noret luaG_runerror (lua_State *L, const char *fmt, ...) {
               va_start(argp, fmt);
               msg = luaO_pushvfstring(L, fmt, argp);  /* format message */
               va_end(argp);
            -  if (isLua(ci))  /* if Lua function, add source:line information */
            +  if (isLua(ci)) {  /* if Lua function, add source:line information */
                 luaG_addinfo(L, msg, ci_func(ci)->p->source, getcurrentline(ci));
            +    setobjs2s(L, L->top - 2, L->top - 1);  /* remove 'msg' from the stack */
            +    L->top--;
            +  }
               luaG_errormsg(L);
             }

            lvm.c:
            @@ -656,8 +656,10 @@ void luaV_concat (lua_State *L, int total) {
                   /* collect total length and number of strings */
                   for (n = 1; n < total && tostring(L, s2v(top - n - 1)); n++) {
                     size_t l = vslen(s2v(top - n - 1));
            -        if (l_unlikely(l >= (MAX_SIZE/sizeof(char)) - tl))
            +        if (l_unlikely(l >= (MAX_SIZE/sizeof(char)) - tl)) {
            +          L->top = top - total;  /* pop strings to avoid wasting stack */
                       luaG_runerror(L, "string length overflow");
            +        }
                     tl += l;
                   }
                   if (tl <= LUAI_MAXSHORTLEN) {  /* is result a short string? */
            @@ -672,7 +674,7 @@ void luaV_concat (lua_State *L, int total) {
                   setsvalue2s(L, top - n, ts);  /* create result */
                 }
                 total -= n-1;  /* got 'n' strings to create 1 new */
            -    L->top -= n-1;  /* popped 'n' strings and pushed one */
            +    L->top = top - (n - 1);  /* popped 'n' strings and pushed one */
               } while (total > 1);  /* repeat until only 1 result left */
             }

        """,
        "'lua_settop' may use a pointer to stack invalidated by 'luaF_close'": """
            lapi.c:
            @@ -202,7 +202,7 @@ LUA_API void lua_settop (lua_State *L, int idx) {
               newtop = L->top + diff;
               if (diff < 0 && L->tbclist >= newtop) {
                 lua_assert(hastocloseCfunc(ci->nresults));
            -    luaF_close(L, newtop, CLOSEKTOP, 0);
            +    newtop = luaF_close(L, newtop, CLOSEKTOP, 0);
               }
               L->top = newtop;  /* correct top only after closing any upvalue */
               lua_unlock(L);
            @@ -215,8 +215,7 @@ LUA_API void lua_closeslot (lua_State *L, int idx) {
               level = index2stack(L, idx);
               api_check(L, hastocloseCfunc(L->ci->nresults) && L->tbclist == level,
                  "no variable to close at given level");
            -  luaF_close(L, level, CLOSEKTOP, 0);
            -  level = index2stack(L, idx);  /* stack may be moved */
            +  level = luaF_close(L, level, CLOSEKTOP, 0);
               setnilvalue(s2v(level));
               lua_unlock(L);
             }
            ldo.c:
            @@ -427,14 +427,15 @@ l_sinline void moveresults (lua_State *L, StkId res, int nres, int wanted) {
                   break;
                 default:  /* two/more results and/or to-be-closed variables */
                   if (hastocloseCfunc(wanted)) {  /* to-be-closed variables? */
            -        ptrdiff_t savedres = savestack(L, res);
                     L->ci->callstatus |= CIST_CLSRET;  /* in case of yields */
                     L->ci->u2.nres = nres;
            -        luaF_close(L, res, CLOSEKTOP, 1);
            +        res = luaF_close(L, res, CLOSEKTOP, 1);
                     L->ci->callstatus &= ~CIST_CLSRET;
            -        if (L->hookmask)  /* if needed, call hook after '__close's */
            +        if (L->hookmask) {  /* if needed, call hook after '__close's */
            +          ptrdiff_t savedres = savestack(L, res);
                       rethook(L, L->ci, nres);
            -        res = restorestack(L, savedres);  /* close and hook can move stack */
            +          res = restorestack(L, savedres);  /* hook can move stack */
            +        }
                     wanted = decodeNresults(wanted);
                     if (wanted == LUA_MULTRET)
                       wanted = nres;  /* we want all results */
            @@ -651,8 +652,7 @@ static int finishpcallk (lua_State *L,  CallInfo *ci) {
               else {  /* error */
                 StkId func = restorestack(L, ci->u2.funcidx);
                 L->allowhook = getoah(ci->callstatus);  /* restore 'allowhook' */
            -    luaF_close(L, func, status, 1);  /* can yield or raise an error */
            -    func = restorestack(L, ci->u2.funcidx);  /* stack may be moved */
            +    func = luaF_close(L, func, status, 1);  /* can yield or raise an error */
                 luaD_seterrorobj(L, status, func);
                 luaD_shrinkstack(L);   /* restore stack size in case of overflow */
                 setcistrecst(ci, LUA_OK);  /* clear original status */
            lfunc.c:
            @@ -223,9 +223,9 @@ static void poptbclist (lua_State *L) {

             /*
             ** Close all upvalues and to-be-closed variables up to the given stack
            -** level.
            +** level. Return restored 'level'.
             */
            -void luaF_close (lua_State *L, StkId level, int status, int yy) {
            +StkId luaF_close (lua_State *L, StkId level, int status, int yy) {
               ptrdiff_t levelrel = savestack(L, level);
               luaF_closeupval(L, level);  /* first, close the upvalues */
               while (L->tbclist >= level) {  /* traverse tbc's down to that level */
            @@ -234,6 +234,7 @@ void luaF_close (lua_State *L, StkId level, int status, int yy) {
                 prepcallclosemth(L, tbc, status, yy);  /* close variable */
                 level = restorestack(L, levelrel);
               }
            +  return level;
             }


            lfunc.h:
            @@ -54,7 +54,7 @@ LUAI_FUNC void luaF_initupvals (lua_State *L, LClosure *cl);
             LUAI_FUNC UpVal *luaF_findupval (lua_State *L, StkId level);
             LUAI_FUNC void luaF_newtbcupval (lua_State *L, StkId level);
             LUAI_FUNC void luaF_closeupval (lua_State *L, StkId level);
            -LUAI_FUNC void luaF_close (lua_State *L, StkId level, int status, int yy);
            +LUAI_FUNC StkId luaF_close (lua_State *L, StkId level, int status, int yy);
             LUAI_FUNC void luaF_unlinkupval (UpVal *uv);
             LUAI_FUNC void luaF_freeproto (lua_State *L, Proto *f);
             LUAI_FUNC const char *luaF_getlocalname (const Proto *func, int local_number,
        """,
        "read overflow in 'l_strcmp'": """
            lvm.c:
            @@ -366,30 +366,32 @@ void luaV_finishset (lua_State *L, const TValue *t, TValue *key,


             /*
            -** Compare two strings 'ls' x 'rs', returning an integer less-equal-
            -** -greater than zero if 'ls' is less-equal-greater than 'rs'.
            +** Compare two strings 'ts1' x 'ts2', returning an integer less-equal-
            +** -greater than zero if 'ts1' is less-equal-greater than 'ts2'.
             ** The code is a little tricky because it allows '\\0' in the strings
            -** and it uses 'strcoll' (to respect locales) for each segments
            -** of the strings.
            +** and it uses 'strcoll' (to respect locales) for each segment
            +** of the strings. Note that segments can compare equal but still
            +** have different lengths.
             */
            -static int l_strcmp (const TString *ls, const TString *rs) {
            -  const char *l = getstr(ls);
            -  size_t ll = tsslen(ls);
            -  const char *r = getstr(rs);
            -  size_t lr = tsslen(rs);
            +static int l_strcmp (const TString *ts1, const TString *ts2) {
            +  const char *s1 = getstr(ts1);
            +  size_t rl1 = tsslen(ts1);  /* real length */
            +  const char *s2 = getstr(ts2);
            +  size_t rl2 = tsslen(ts2);
               for (;;) {  /* for each segment */
            -    int temp = strcoll(l, r);
            +    int temp = strcoll(s1, s2);
                 if (temp != 0)  /* not equal? */
                   return temp;  /* done */
                 else {  /* strings are equal up to a '\\0' */
            -      size_t len = strlen(l);  /* index of first '\\0' in both strings */
            -      if (len == lr)  /* 'rs' is finished? */
            -        return (len == ll) ? 0 : 1;  /* check 'ls' */
            -      else if (len == ll)  /* 'ls' is finished? */
            -        return -1;  /* 'ls' is less than 'rs' ('rs' is not finished) */
            -      /* both strings longer than 'len'; go on comparing after the '\\0' */
            -      len++;
            -      l += len; ll -= len; r += len; lr -= len;
            +      size_t zl1 = strlen(s1);  /* index of first '\\0' in 's1' */
            +      size_t zl2 = strlen(s2);  /* index of first '\\0' in 's2' */
            +      if (zl2 == rl2)  /* 's2' is finished? */
            +        return (zl1 == rl1) ? 0 : 1;  /* check 's1' */
            +      else if (zl1 == rl1)  /* 's1' is finished? */
            +        return -1;  /* 's1' is less than 's2' ('s2' is not finished) */
            +      /* both strings longer than 'zl'; go on comparing after the '\\0' */
            +      zl1++; zl2++;
            +      s1 += zl1; rl1 -= zl1; s2 += zl2; rl2 -= zl2;
                 }
               }
             }
        """,
        "Call hook may be called twice when count hook yields": """
            ldebug.c:
            @@ -865,6 +865,28 @@ static int changedline (const Proto *p, int oldpc, int newpc) {
             }


            +/*
            +** Traces Lua calls. If code is running the first instruction of a function,
            +** and function is not vararg, and it is not coming from an yield,
            +** calls 'luaD_hookcall'. (Vararg functions will call 'luaD_hookcall'
            +** after adjusting its variable arguments; otherwise, they could call
            +** a line/count hook before the call hook. Functions coming from
            +** an yield already called 'luaD_hookcall' before yielding.)
            +*/
            +int luaG_tracecall (lua_State *L) {
            +  CallInfo *ci = L->ci;
            +  Proto *p = ci_func(ci)->p;
            +  ci->u.l.trap = 1;  /* ensure hooks will be checked */
            +  if (ci->u.l.savedpc == p->code) {  /* first instruction (not resuming)? */
            +    if (p->is_vararg)
            +      return 0;  /* hooks will start at VARARGPREP instruction */
            +    else if (!(ci->callstatus & CIST_HOOKYIELD))  /* not yieded? */
            +      luaD_hookcall(L, ci);  /* check 'call' hook */
            +  }
            +  return 1;  /* keep 'trap' on */
            +}
            +
            +
             /*
             ** Traces the execution of a Lua function. Called before the execution
             ** of each opcode, when debug is on. 'L->oldpc' stores the last
            ldebug.h:
            @@ -58,6 +58,7 @@ LUAI_FUNC const char *luaG_addinfo (lua_State *L, const char *msg,
                                                               TString *src, int line);
             LUAI_FUNC l_noret luaG_errormsg (lua_State *L);
             LUAI_FUNC int luaG_traceexec (lua_State *L, const Instruction *pc);
            +LUAI_FUNC int luaG_tracecall (lua_State *L);


             #endif
            lstate.h:
            @@ -181,7 +181,7 @@ struct CallInfo {
               union {
                 struct {  /* only for Lua functions */
                   const Instruction *savedpc;
            -      volatile l_signalT trap;
            +      volatile l_signalT trap;  /* function is tracing lines/counts */
                   int nextraargs;  /* # of extra arguments in vararg functions */
                 } l;
                 struct {  /* only for C functions */
            lvm.c:
            @@ -1157,18 +1157,11 @@ void luaV_execute (lua_State *L, CallInfo *ci) {
              startfunc:
               trap = L->hookmask;
              returning:  /* trap already set */
            -  cl = clLvalue(s2v(ci->func.p));
            +  cl = ci_func(ci);
               k = cl->p->k;
               pc = ci->u.l.savedpc;
            -  if (l_unlikely(trap)) {
            -    if (pc == cl->p->code) {  /* first instruction (not resuming)? */
            -      if (cl->p->is_vararg)
            -        trap = 0;  /* hooks will start after VARARGPREP instruction */
            -      else  /* check 'call' hook */
            -        luaD_hookcall(L, ci);
            -    }
            -    ci->u.l.trap = 1;  /* assume trap is on, for now */
            -  }
            +  if (l_unlikely(trap))
            +    trap = luaG_tracecall(L);
               base = ci->func.p + 1;
               /* main loop of interpreter */
               for (;;) {
        """,
        "Wrong line number for function calls": """
            lparser.c:
            @@ -1022,10 +1022,11 @@ static int explist (LexState *ls, expdesc *v) {
             }


            -static void funcargs (LexState *ls, expdesc *f, int line) {
            +static void funcargs (LexState *ls, expdesc *f) {
               FuncState *fs = ls->fs;
               expdesc args;
               int base, nparams;
            +  int line = ls->linenumber;
               switch (ls->t.token) {
                 case '(': {  /* funcargs -> '(' [ explist ] ')' */
                   luaX_next(ls);
            @@ -1063,8 +1064,8 @@ static void funcargs (LexState *ls, expdesc *f, int line) {
               }
               init_exp(f, VCALL, luaK_codeABC(fs, OP_CALL, base, nparams+1, 2));
               luaK_fixline(fs, line);
            -  fs->freereg = base+1;  /* call remove function and arguments and leaves
            -                            (unless changed) one result */
            +  fs->freereg = base+1;  /* call removes function and arguments and leaves
            +                            one result (unless changed later) */
             }


            @@ -1103,7 +1104,6 @@ static void suffixedexp (LexState *ls, expdesc *v) {
               /* suffixedexp ->
                    primaryexp { '.' NAME | '[' exp ']' | ':' NAME funcargs | funcargs } */
               FuncState *fs = ls->fs;
            -  int line = ls->linenumber;
               primaryexp(ls, v);
               for (;;) {
                 switch (ls->t.token) {
            @@ -1123,12 +1123,12 @@ static void suffixedexp (LexState *ls, expdesc *v) {
                     luaX_next(ls);
                     codename(ls, &key);
                     luaK_self(fs, v, &key);
            -        funcargs(ls, v, line);
            +        funcargs(ls, v);
                     break;
                   }
                   case '(': case TK_STRING: case '{': {  /* funcargs */
                     luaK_exp2nextreg(fs, v);
            -        funcargs(ls, v, line);
            +        funcargs(ls, v);
                     break;
                   }
                   default: return;
        """,
        "Wrong code generation for indices with comparisons": """
            lcode.c:
            @@ -985,7 +985,7 @@
             ** or it is a constant.
             */
             void luaK_exp2val (FuncState *fs, expdesc *e) {
            -  if (hasjumps(e))
            +  if (e->k == VJMP || hasjumps(e))
                 luaK_exp2anyreg(fs, e);
               else
                 luaK_dischargevars(fs, e);
        """,
        "Wrong limit for local variables in 16-bit systems": """
            lparser.c:
            @@ -198,7 +198,7 @@ static int new_localvar (LexState *ls, TString *name) {
               checklimit(fs, dyd->actvar.n + 1 - fs->firstlocal,
                              MAXVARS, "local variables");
               luaM_growvector(L, dyd->actvar.arr, dyd->actvar.n + 1,
            -                  dyd->actvar.size, Vardesc, USHRT_MAX, "local variables");
            +                  dyd->actvar.size, Vardesc, SHRT_MAX, "local variables");
               var = &dyd->actvar.arr[dyd->actvar.n++];
               var->vd.kind = VDKREG;  /* default */
               var->vd.name = name;
        """,
        "An emergency GC can collect the __newindex of a metatable (if the metatable is a weak table) while that field is being used in a table update": """
            lvm.c:
            @@ -339,7 +339,10 @@ void luaV_finishset (lua_State *L, const TValue *t, TValue *key,
                   lua_assert(isempty(slot));  /* slot must be empty */
                   tm = fasttm(L, h->metatable, TM_NEWINDEX);  /* get metamethod */
                   if (tm == NULL) {  /* no metamethod? */
            +        sethvalue2s(L, L->top.p, h);  /* anchor 't' */
            +        L->top.p++;  /* assume EXTRA_STACK */
                     luaH_finishset(L, h, key, slot, val);  /* set new value */
            +        L->top.p--;
                     invalidateTMcache(h);
                     luaC_barrierback(L, obj2gco(h), val);
                     return;
        """,
        "'luaD_seterrorobj' should not raise errors, because it is called unprotected": """
            ldo.c:
            @@ -94,10 +94,6 @@ void luaD_seterrorobj (lua_State *L, int errcode, StkId oldtop) {
                   setsvalue2s(L, oldtop, G(L)->memerrmsg); /* reuse preregistered msg. */
                   break;
                 }
            -    case LUA_ERRERR: {
            -      setsvalue2s(L, oldtop, luaS_newliteral(L, "error in error handling"));
            -      break;
            -    }
                 case LUA_OK: {  /* special case only for closing upvalues */
                   setnilvalue(s2v(oldtop));  /* no error message */
                   break;
            @@ -198,6 +194,16 @@ static void correctstack (lua_State *L) {
             /* some space for error handling */
             #define ERRORSTACKSIZE	(LUAI_MAXSTACK + 200)

            +
            +/* raise an error while running the message handler */
            +l_noret luaD_errerr (lua_State *L) {
            +  TString *msg = luaS_newliteral(L, "error in error handling");
            +  setsvalue2s(L, L->top.p, msg);
            +  L->top.p++;  /* assume EXTRA_STACK */
            +  luaD_throw(L, LUA_ERRERR);
            +}
            +
            +
             /*
             ** Reallocate the stack to a new size, correcting all pointers into it.
             ** In ISO C, any pointer use after the pointer has been deallocated is
            @@ -247,7 +253,7 @@ int luaD_growstack (lua_State *L, int n, int raiseerror) {
                    a stack error; cannot grow further than that. */
                 lua_assert(stacksize(L) == ERRORSTACKSIZE);
                 if (raiseerror)
            -      luaD_throw(L, LUA_ERRERR);  /* error inside message handler */
            +      luaD_errerr(L);  /* error inside message handler */
                 return 0;  /* if not 'raiseerror', just signal it */
               }
               else if (n < LUAI_MAXSTACK) {  /* avoids arithmetic overflows */
            ldo.h:
            @@ -60,6 +60,7 @@
             /* type of protected functions, to be ran by 'runprotected' */
             typedef void (*Pfunc) (lua_State *L, void *ud);

            +LUAI_FUNC l_noret luaD_errerr (lua_State *L);
             LUAI_FUNC void luaD_seterrorobj (lua_State *L, int errcode, StkId oldtop);
             LUAI_FUNC int luaD_protectedparser (lua_State *L, ZIO *z, const char *name,
                                                               const char *mode);
            lstate.c:
            @@ -166,7 +166,7 @@ void luaE_checkcstack (lua_State *L) {
               if (getCcalls(L) == LUAI_MAXCCALLS)
                 luaG_runerror(L, "C stack overflow");
               else if (getCcalls(L) >= (LUAI_MAXCCALLS / 10 * 11))
            -    luaD_throw(L, LUA_ERRERR);  /* error while handling stack error */
            +    luaD_errerr(L);  /* error while handling stack error */
             }


        """,
        "message handler can be overwritten by a closing variable when closing a thread": """
            lstate.c:
            @@ -272,7 +272,9 @@ static void close_state (lua_State *L) {
                 luaC_freeallobjects(L);  /* just collect its objects */
               else {  /* closing a fully built state */
                 L->ci = &L->base_ci;  /* unwind CallInfo list */
            +    L->errfunc = 0;   /* stack unwind can "throw away" the error function */
                 luaD_closeprotected(L, 1, LUA_OK);  /* close all upvalues */
            +    L->top.p = L->stack.p + 1;  /* empty the stack to run finalizers */
                 luaC_freeallobjects(L);  /* collect all objects */
                 luai_userstateclose(L);
               }
            @@ -328,6 +330,7 @@ int luaE_resetthread (lua_State *L, int status) {
               if (status == LUA_YIELD)
                 status = LUA_OK;
               L->status = LUA_OK;  /* so it can run __close metamethods */
            +  L->errfunc = 0;   /* stack unwind can "throw away" the error function */
               status = luaD_closeprotected(L, 1, status);
               if (status != LUA_OK)  /* errors? */
                 luaD_seterrorobj(L, status, L->stack.p + 1);
        """,
        "New metatable in an all-weak table can fool the GC": """
            lgc.c:
            @@ -553,8 +553,12 @@ static lu_mem traversetable (global_State *g, Table *h) {
                   traverseweakvalue(g, h);
                 else if (!weakvalue)  /* strong values? */
                   traverseephemeron(g, h, 0);
            -    else  /* all weak */
            -      linkgclist(h, g->allweak);  /* nothing to traverse now */
            +    else {  /* all weak */
            +      if (g->gcstate == GCSpropagate)
            +        linkgclist(h, g->grayagain);  /* must visit again its metatable */
            +      else
            +        linkgclist(h, g->allweak);  /* must clear collected entries */
            +    }
               }
               else  /* not weak */
                 traversestrongtable(g, h);
        """,
        "Lua raises an error when given the option '--' without a script": """
            lua.c:
            @@ -302,7 +302,8 @@ static int collectargs (char **argv, int *first) {
                   case '-':  /* '--' */
                     if (argv[i][2] != '\\0')  /* extra characters after '--'? */
                       return has_error;  /* invalid option */
            -        *first = i + 1;
            +        /* if there is a script name, it comes after '--' */
            +        *first = (argv[i + 1] != NULL) ? i + 1 : 0;
                     return args;
                   case '\\0':  /* '-' */
                     return args;  /* script "name" is '-' */
        """,
        "Constructors with nils can overflow counters during parsing": """
            lparser.c:
            @@ -940,6 +940,8 @@ static void constructor (LexState *ls, expdesc *t) {
                 if (ls->t.token == '}') break;
                 closelistfield(fs, &cc);
                 field(ls, &cc);
            +    checklimit(fs, cc.tostore + cc.na + cc.nh, INT_MAX/2,
            +               "items in a constructor");
               } while (testnext(ls, ',') || testnext(ls, ';'));
               check_match(ls, '}', '{', line);
               lastlistfield(fs, &cc);
        """
    }
    patches_per_version = {
        "5.1": {
            "5": [
                "When loading a file, Lua may call the reader function again after it returned end of input"
            ]
        },
        "5.3": {
            "2": [
                "Metatable may access its own deallocated field when it has a self reference in __newindex",
                "Label between local definitions can mix-up their initializations",
                "gmatch iterator fails when called from a coroutine different from the one that created it"
            ],
            "3": [
                "Expression list with four or more expressions in a 'for' loop can crash the interpreter",
                "Checking a format for os.date may read past the format string",
                "Lua can generate wrong code in functions with too many constants"
            ],
            "4": [
                "Wrong code generated for a 'goto' followed by a label inside an 'if'",
                "Lua crashes when building sequences with more than 2^30 elements",
                "Table length computation overflows for sequences larger than 2^31 elements",
                "Lua does not check GC when creating error messages",
                "Dead keys with nil values can stay in weak tables",
                "lua_pushcclosure should not call the garbage collector when n is zero",
                "Memory-allocation error when resizing a table can leave it in an inconsistent state"
            ],
            "5": [
                "Joining an upvalue with itself can cause a use-after-free crash"
            ]
        },
        "5.4": {
            "0": [
                "Old finalized object may not be visited by GC",
                "Computation of stack limit when entering a coroutine is wrong",
                "An emergency collection when handling an error while loading the upvalues of a function can cause a segfault",
                "'checkstackp' can run a GC step and destroy a preallocated CallInfo",
                "GC after resizing stack can shrink it again",
                "Errors in finalizers need a valid 'pc' to produce an error message",
                "'popen' can crash if called with an invalid mode",
                "Field 'L->oldpc' is not always updated when returning to a function"
            ],
            "2": [
                "Parameter 'what' of 'debug.getinfo' cannot start with '>'",
                "Error message in 'string.concat' uses wrong format"
            ],
            "3": [
                "C99 comments are not compatible with C89",
                "Yielding in a __close metamethod called when returning vararg results mess up the returned values",
                "'luaL_tolstring' may get confused with negative indices",
                "negation in macro 'luaV_shiftr' may overflow"
            ],
            "4": [
                "Lua can generate wrong code when _ENV is <const>",
                "Wrong code generation for constants in bitwise operations",
                "Lua-stack overflow when C stack overflows while handling an error",
                "'lua_settop' may use a pointer to stack invalidated by 'luaF_close'"
            ],
            "6": [
                "read overflow in 'l_strcmp'",
                "Call hook may be called twice when count hook yields",
                "Wrong line number for function calls"
            ],
            "7": [
                "Wrong code generation for indices with comparisons",
                "Wrong limit for local variables in 16-bit systems",
                "An emergency GC can collect the __newindex of a metatable (if the metatable is a weak table) while that field is being used in a table update",
                "'luaD_seterrorobj' should not raise errors, because it is called unprotected",
                "message handler can be overwritten by a closing variable when closing a thread"
            ],
            "8": [
                "New metatable in an all-weak table can fool the GC",
                "Lua raises an error when given the option '--' without a script",
                "Constructors with nils can overflow counters during parsing"
            ]
        },
    }

    def __init__(self, version):
        super(RioLua, self).__init__(version)

        self.lua_file = exe("lua")
        self.luac_file = exe("luac")

        if using_cl():
            self.arch_file = "lua5" + self.major_version[2] + ".lib"
        else:
            self.arch_file = "liblua5" + self.major_version[2] + ".a"

        if opts.target == "mingw" or using_cl():
            self.dll_file = "lua5" + self.major_version[2] + ".dll"
        else:
            self.dll_file = None

    def get_download_name(self):
        return "{}-{}.tar.gz".format(self.name, self.version)

    def get_download_urls(self):
        if "work" in self.version or "alpha" in self.version or "beta" in self.version or "rc" in self.version:
            return ["{}/{}".format(self.work_base_download_url, self.get_download_name())]
        else:
            return ["{}/{}".format(base_download_url, self.get_download_name()) for base_download_url in self.base_download_urls]

    def get_source_files_prefix(self):
        # When installing PUC-Rio Lua from a git repo or local sources,
        # use directory structure of its GitHub mirror, where
        # source files are direcly in project root instead of `src`.
        if self.source == "release":
            return "src"

    def set_identifiers(self):
        super(RioLua, self).set_identifiers()

        self.identifiers["readline"] = str(not opts.no_readline).lower()
        self.identifiers["patched"] = str(opts.patch).lower()

    def major_version_from_version(self):
        return self.version[:3]

    def set_version_suffix(self):
        self.version_suffix = " " + self.major_version

    def set_compat(self):
        if self.major_version == "5.1":
            self.compat = "none" if opts.compat == "none" else "default"
        elif self.major_version == "5.2":
            self.compat = "none" if opts.compat in ["none", "5.2"] else "default"
        elif self.major_version == "5.3":
            self.compat = "default" if opts.compat in ["default", "5.2"] else opts.compat
        else:
            self.compat = "default" if opts.compat in ["default", "5.3"] else opts.compat

    def add_compat_cflags_and_redefines(self):
        if self.major_version == "5.1":
            if self.compat == "none":
                self.redefines.extend([
                    "#undef LUA_COMPAT_VARARG", "#undef LUA_COMPAT_MOD",
                    "#undef LUA_COMPAT_LSTR", "#undef LUA_COMPAT_GFIND",
                    "#undef LUA_COMPAT_OPENLIB"
                ])
        elif self.major_version == "5.2":
            if self.compat == "default":
                self.compat_cflags.append("-DLUA_COMPAT_ALL")
        elif self.major_version == "5.3":
            if self.compat in ["5.1", "all"]:
                self.compat_cflags.append("-DLUA_COMPAT_5_1")

            if self.compat in ["default", "5.2", "all"]:
                self.compat_cflags.append("-DLUA_COMPAT_5_2")
        else:
            if self.compat in ["default", "5.3", "all"]:
                self.compat_cflags.append("-DLUA_COMPAT_5_3")

    def apply_patch(self, patch_name):
        patch = self.all_patches[patch_name]
        err = Patch(patch).apply()
        status = "OK" if err is None else "fail - {}".format(err)
        print('Patch for "{}": {}'.format(patch_name, status))
        return err is None

    @staticmethod
    def minor_version_from_source():
        regexps = [
            # Lua 5.1.x, but not Lua 5.1(.0)
            r'^\s*#define\s+LUA_RELEASE\s+"Lua 5\.1\.(\d)"\s*$',
            # Lua 5.2.x+
            r'^\s*#define LUA_VERSION_RELEASE\s+"(\d)"\s*$'
        ]

        with open(os.path.join("lua.h")) as lua_h:
            for line in lua_h:
                for regexp in regexps:
                    match = re.match(regexp, line)

                    if match:
                        return match.group(1)

        # Reachable only for Lua 5.1(.0) or if lua.h is strange.
        return "0"

    def get_minor_version(self):
        if self.source == "release":
            return "0" if self.version == "5.1" else self.version[-1:]
        else:
            return self.minor_version_from_source()

    def handle_patches(self):
        patches = self.patches_per_version.get(self.major_version, {})

        if not patches:
            print("No patches available for Lua {}".format(self.major_version))
            return

        minor_version = self.get_minor_version()
        patches = patches.get(minor_version, [])

        if not patches:
            print("No patches available for Lua {}.{}".format(self.major_version, minor_version))
            return

        if not opts.patch:
            print("Skipping {} patch{}, use --patch to apply {}".format(
                len(patches), "" if len(patches) == 1 else "es",
                "it" if len(patches) == 1 else "them"))
            return

        applied = sum(map(self.apply_patch, patches))
        print("Applied {} patch{} ({} available for this version)".format(
            applied, "" if applied == 1 else "es", len(patches)))

    def make(self):
        if self.major_version == "5.3" or self.major_version == "5.4":
            cc = ["gcc", "-std=gnu99"]
        else:
            cc = "gcc"

        if opts.target in ["linux", "freebsd", "macosx"]:
            cflags = ["-DLUA_USE_POSIX", "-DLUA_USE_DLOPEN"]

            if self.major_version == "5.2":
                cflags.extend(["-DLUA_USE_STRTODHEX", "-DLUA_USE_AFORMAT", "-DLUA_USE_LONGLONG"])

            if not opts.no_readline:
                cflags.append("-DLUA_USE_READLINE")

            if opts.target == "linux":
                lflags = ["-Wl,-E", "-ldl"]

                if not opts.no_readline:
                    if self.major_version == "5.1":
                        lflags.extend(["-lreadline", "-lhistory", "-lncurses"])
                    else:
                        lflags.append("-lreadline")
            elif opts.target == "freebsd":
                lflags = []

                if not opts.no_readline:
                    lflags.extend(["-Wl,-E", "-lreadline"])
            else:
                lflags = []
                cc = "cc"

                if not opts.no_readline:
                    lflags.append("-lreadline")
        else:
            lflags = []

            if opts.target == "posix":
                cflags = ["-DLUA_USE_POSIX"]
            else:
                cflags = []

        cflags.extend(self.compat_cflags)

        if opts.cflags is not None:
            cflags.extend(opts.cflags.split())

        if using_cl():
            cc = ["cl", "/nologo", "/MD", "/O2", "/W3", "/c", "/D_CRT_SECURE_NO_DEPRECATE"]
        else:
            cflags = ["-O2", "-Wall", "-Wextra"] + cflags

        lflags.append("-lm")
        static_cflags = list(cflags)

        if opts.target == "mingw":
            cflags.insert(3, "-DLUA_BUILD_AS_DLL")
        elif using_cl():
            cflags.insert(0, "-DLUA_BUILD_AS_DLL")

        with self.in_source_files_prefix():
            self.handle_patches()
            objs = []
            luac_objs = ["luac" + objext(), "print" + objext()]

            for src in sorted(os.listdir(".")):
                base, ext = os.path.splitext(src)

                if ext == ".c" and base != "onelua":
                    obj = base + objext()
                    objs.append(obj)

                    cmd_suffix = src if using_cl() else ["-c", "-o", obj, src]
                    run(cc, static_cflags if obj in luac_objs else cflags, cmd_suffix)

            lib_objs = [obj_ for obj_ in objs if obj_ not in luac_objs and (obj_ != "lua" + objext())]

            if not using_cl():
                run("ar", "rcu", self.arch_file, lib_objs)
                run("ranlib", self.arch_file)

            built_luac_objs = [obj_ for obj_ in luac_objs if obj_ in objs]

            # Handle the case when there are no source files for `luac`, likely because installing
            # from a git repo that does not have them, like the default one.
            if len(built_luac_objs) > 0:
                if using_cl():
                    run("link", "/nologo", "/out:luac.exe", built_luac_objs, lib_objs)

                    if os.path.exists("luac.exe.manifest"):
                        run("mt", "/nologo", "-manifest", "luac.exe.manifest", "-outputresource:luac.exe")
                else:
                    run(cc, "-o", self.luac_file, built_luac_objs, self.arch_file, lflags)

            if opts.target == "mingw":
                run(cc, "-shared", "-o", self.dll_file, lib_objs)
                run("strip", "--strip-unneeded", self.dll_file)
                run(cc, "-o", self.lua_file, "-s", "lua.o", self.dll_file)
            elif using_cl():
                run("link", "/nologo", "/DLL", "/out:" + self.dll_file, lib_objs)

                if os.path.exists(self.dll_file + ".manifest"):
                    run("mt", "/nologo", "-manifest", self.dll_file + ".manifest",
                        "-outputresource:" + self.dll_file)

                run("link", "/nologo", "/out:lua.exe", "lua.obj", self.arch_file)

                if os.path.exists("lua.exe.manifest"):
                    run("mt", "/nologo", "-manifest", "lua.exe.manifest", "-outputresource:lua.exe")
            else:
                run(cc, "-o", self.lua_file, "lua.o", self.arch_file, lflags)

    def make_install(self):
        with self.in_source_files_prefix():
            luac = self.luac_file

            if not os.path.exists(luac):
                luac = None

            copy_files(os.path.join(opts.location, "bin"),
                       self.lua_file, luac, self.dll_file)

            lua_hpp = "lua.hpp"

            if not os.path.exists(lua_hpp):
                if self.source_files_prefix is None:
                    lua_hpp = None
                else:
                    lua_hpp = "../etc/lua.hpp"

            copy_files(os.path.join(opts.location, "include"),
                       "lua.h", "luaconf.h", "lualib.h", "lauxlib.h", lua_hpp)

            copy_files(os.path.join(opts.location, "lib"), self.arch_file)

class BaseJIT(Lua):
    @staticmethod
    def major_version_from_version():
        return "5.1"

    @staticmethod
    def set_version_suffix():
        pass

    def set_compat(self):
        self.compat = "5.2" if opts.compat in ["all", "5.2"] else "default"

    def add_compat_cflags_and_redefines(self):
        if self.compat == "5.2":
            self.compat_cflags.append("-DLUAJIT_ENABLE_LUA52COMPAT")

    @staticmethod
    def add_cflags_to_msvcbuild(cflags):
        with open("msvcbuild.bat", "rb") as msvcbuild_file:
            msvcbuild_src = msvcbuild_file.read()

        start, assignment, value_and_rest = msvcbuild_src.partition(b"@set LJCOMPILE")
        value_and_rest = value_and_rest.decode("UTF-8")

        with open("msvcbuild.bat", "wb") as msvcbuild_file:
            msvcbuild_file.write(start)
            msvcbuild_file.write(assignment)
            msvcbuild_file.write(value_and_rest.replace("\r\n", " {}\r\n".format(cflags), 1).encode("UTF-8"))

    def make(self):
        if sys.platform == "darwin" and "MACOSX_DEPLOYMENT_TARGET" not in os.environ:
            # X.Y[.Z] -> X.Y
            os.environ["MACOSX_DEPLOYMENT_TARGET"] = ".".join(platform.mac_ver()[0].split(".")[:2])

        cflags = list(self.compat_cflags)

        if opts.cflags is not None:
            cflags.extend(opts.cflags.split())

        if using_cl():
            with self.in_source_files_prefix():
                if cflags:
                    self.add_cflags_to_msvcbuild(" ".join(cflags))

                run("msvcbuild.bat")
        else:
            make_args = []
            if opts.target == "mingw" and program_exists("mingw32-make"):
                make = "mingw32-make"
                make_args.append("SHELL=cmd")
            elif opts.target == "freebsd":
                make = "gmake"
            else:
                make = "make"

            if cflags:
                make_args.append("XCFLAGS=" + " ".join(cflags))

            run(make, make_args)

    def make_install(self):
        luajit_file = exe("luajit")
        lua_file = exe("lua")
        arch_file = "libluajit.a"
        target_arch_file = "libluajit-5.1.a"
        so_file = "libluajit.so"
        target_so_file = "libluajit-5.1.so.2"
        dll_file = None

        if os.name == "nt":
            arch_file = "lua51.lib"
            target_arch_file = "lua51.lib"
            dll_file = "lua51.dll"

        with self.in_source_files_prefix():
            copy_files(os.path.join(opts.location, "bin"), dll_file)
            shutil.copy(luajit_file, os.path.join(opts.location, "bin", lua_file))

            copy_files(os.path.join(opts.location, "include"),
                       "lua.h", "luaconf.h", "lualib.h", "lauxlib.h", "lua.hpp", "luajit.h")

            copy_files(os.path.join(opts.location, "lib"))

            if opts.target != "mingw":
                shutil.copy(arch_file, os.path.join(opts.location, "lib", target_arch_file))

            if os.name != "nt":
                shutil.copy(so_file, os.path.join(opts.location, "lib", target_so_file))

            jitlib_path = os.path.join(
                opts.location, "share", "lua", self.major_version, "jit")

            if os.path.exists(jitlib_path):
                remove_dir(jitlib_path)

            copy_dir("jit", jitlib_path)

class LuaJIT(BaseJIT):
    name = "LuaJIT"
    title = "LuaJIT"
    base_download_url = "https://github.com/LuaJIT/LuaJIT/archive"
    default_repo = "https://github.com/LuaJIT/LuaJIT"
    versions = [
        "2.0.0", "2.0.1", "2.0.2", "2.0.3", "2.0.4", "2.0.5",
        "2.1.0-beta1", "2.1.0-beta2", "2.1.0-beta3"
    ]
    translations = {
        "2": "2.0.5",
        "2.0": "2.0.5",
        "2.1": "2.1.0-beta3",
        "^": "2.0.5",
        "latest": "2.0.5"
    }
    checksums = {
        "LuaJIT-2.0.0.tar.gz"      : "778650811bdd9fc55bbb6a0e845e4c0101001ce5ca1ab95001f0d289c61760ab",
        "LuaJIT-2.0.1-fixed.tar.gz": "d33e91f347c0d79aa4fb1bd835df282a25f7ef9c3395928a1183947667c2d6b2",
        "LuaJIT-2.0.2.tar.gz"      : "7cf1bdcd89452f64ed994cff85ae32613a876543a81a88939155266558a669bc",
        "LuaJIT-2.0.3.tar.gz"      : "8da3d984495a11ba1bce9a833ba60e18b532ca0641e7d90d97fafe85ff014baa",
        "LuaJIT-2.0.4.tar.gz"      : "d2abdf16bd3556c41c0aaedad76b6c227ca667be8350111d037a4c54fd43abad",
        "LuaJIT-2.0.5.tar.gz"      : "8bb29d84f06eb23c7ea4aa4794dbb248ede9fcb23b6989cbef81dc79352afc97",
        "LuaJIT-2.1.0-beta1.tar.gz": "3d10de34d8020d7035193013f07c93fc7f16fcf0bb28fc03f572a21a368a5f2a",
        "LuaJIT-2.1.0-beta2.tar.gz": "82e115b21aa74634b2d9f3cb3164c21f3cde7750ba3258d8820f500f6a36b651",
        "LuaJIT-2.1.0-beta3.tar.gz": "409f7fe570d3c16558e594421c47bdd130238323c9d6fd6c83dedd2aaeb082a8",
    }
    # https://github.com/LuaJIT/LuaJIT/commit/50e0fa03c48cb9af03c3efdc3100f12687651a2e \
    # #diff-3e2513390df543315686d7c85bd901ca9256268970032298815d2f893a9f0685R449
    needs_git_dir_for_build = True

    def get_download_name(self):
        # v2.0.1 tag is broken, use v2.0.1-fixed.
        return "{}-{}.tar.gz".format(self.name, "2.0.1-fixed" if self.version == "2.0.1" else self.version)

    def get_download_urls(self):
        return ["{}/v{}.tar.gz".format(self.base_download_url, "2.0.1-fixed" if self.version == "2.0.1" else self.version)]

class MoonJIT(BaseJIT):
    name = "moonjit"
    title = "moonjit"
    base_download_url = "https://github.com/moonjit/moonjit/archive"
    default_repo = "https://github.com/moonjit/moonjit"
    versions = [
        "2.1.1", "2.1.2",
        "2.2.0"
    ]
    translations = {
        "2.1": "2.1.2",
        "2.2": "2.2.0",
        "^": "2.1.2",
        "latest": "2.1.2"
    }
    checksums = {
        "moonjit-2.1.1.tar.gz": "aa04d47f23bf24173e58dff0a727e8061fb88c07966a956bd86b13dae5542616",
        "moonjit-2.1.2.tar.gz": "c3de8e29aa617fc594c043f57636ab9ad71af2b4a3a513932b05f5cdaa4320b2",
        "moonjit-2.2.0.tar.gz": "83deb2c880488dfe7dd8ebf09e3b1e7613ef4b8420de53de6f712f01aabca2b6",
    }

    def get_download_name(self):
        return "{}-{}.tar.gz".format(self.name, self.version)

    def get_download_urls(self):
        return ["{}/{}.tar.gz".format(self.base_download_url, self.version)]

class RaptorJIT(BaseJIT):
    name = "raptorjit"
    title = "RaptorJIT"
    base_download_url = "https://github.com/raptorjit/raptorjit/archive"
    default_repo = "https://github.com/raptorjit/raptorjit"
    versions = [
        "1.0.0", "1.0.1", "1.0.2", "1.0.3"
    ]
    translations = {
        "1": "1.0.3",
        "1.0": "1.0.3",
        "^": "1.0.3",
        "latest": "1.0.3"
    }
    checksums = {
        "raptorjit-1.0.0.tar.gz": "886bbe6b9b282260d76af45993857254ad11d280be3c1c147058b7bc544d77a0",
        "raptorjit-1.0.1.tar.gz": "4666d51d24040176b2b5d6ab58f1d4452db4b6cb09a4f8dc38d9559377b07d73",
        "raptorjit-1.0.2.tar.gz": "83495dbbfa503593fc3390f9772e5f1e109de5cc1590686f4da3445189ee7f80",
        "raptorjit-1.0.3.tar.gz": "d921eb544e64eaefb30656f53eee7e71beb7fc590269fec06a90a4e66cb0d6e5",
    }

    def get_download_name(self):
        return "{}-{}.tar.gz".format(self.name, self.version)

    def get_download_urls(self):
        return ["{}/v{}.tar.gz".format(self.base_download_url, self.version)]

    def make_install(self):
        luajit_file = exe("raptorjit")
        lua_file = exe("lua")
        arch_file = "raptorjit.a"
        target_arch_file = "libluajit-5.1.a"
        so_file = "libraptorjit.so"
        target_so_file = "libluajit-5.1.so.2"
        dll_file = None

        with self.in_source_files_prefix():
            copy_files(os.path.join(opts.location, "bin"), dll_file)
            shutil.copy(luajit_file, os.path.join(opts.location, "bin", lua_file))

            copy_files(os.path.join(opts.location, "include"),
                       "lua.h", "luaconf.h", "lualib.h", "lauxlib.h", "lua.hpp", "luajit.h")

            copy_files(os.path.join(opts.location, "lib"))

            if opts.target != "mingw":
                shutil.copy(arch_file, os.path.join(opts.location, "lib", target_arch_file))

            shutil.copy(so_file, os.path.join(opts.location, "lib", target_so_file))

            jitlib_path = os.path.join(
                opts.location, "share", "lua", self.major_version, "jit")

            if os.path.exists(jitlib_path):
                remove_dir(jitlib_path)

            copy_dir("jit", jitlib_path)

class LuaRocks(Program):
    name = "luarocks"
    title = "LuaRocks"
    base_download_url = "https://luarocks.github.io/luarocks/releases"
    default_repo = "https://github.com/luarocks/luarocks"
    versions = [
        "2.0.8", "2.0.9", "2.0.10", "2.0.11", "2.0.12", "2.0.13",
        "2.1.0", "2.1.1", "2.1.2",
        "2.2.0", "2.2.1", "2.2.2",
        "2.3.0",
        "2.4.0", "2.4.1", "2.4.2", "2.4.3", "2.4.4",
        "3.0.0", "3.0.1", "3.0.2", "3.0.3", "3.0.4",
        "3.1.0", "3.1.1", "3.1.2", "3.1.3",
        "3.2.0", "3.2.1",
        "3.3.0", "3.3.1",
        "3.4.0",
        "3.5.0",
        "3.6.0",
        "3.7.0",
        "3.8.0",
        "3.9.0", "3.9.1", "3.9.2",
        "3.10.0",
        "3.11.0", "3.11.1",
        "3.12.0", "3.12.1", "3.12.2",
    ]
    translations = {
        "2": "2.4.4",
        "2.0": "2.0.13",
        "2.1": "2.1.2",
        "2.2": "2.2.2",
        "2.3": "2.3.0",
        "2.4": "2.4.4",
        "3": "3.12.2",
        "3.0": "3.0.4",
        "3.1": "3.1.3",
        "3.2": "3.2.1",
        "3.3": "3.3.1",
        "3.4": "3.4.0",
        "3.5": "3.5.0",
        "3.6": "3.6.0",
        "3.7": "3.7.0",
        "3.8": "3.8.0",
        "3.9": "3.9.2",
        "3.10": "3.10.0",
        "3.11": "3.11.1",
        "3.12": "3.12.2",
        "^": "3.12.2",
        "latest": "3.12.2"
    }
    checksums = {
        "luarocks-2.0.10.tar.gz"   : "11731dfe6e210a962cb2a857b8b2f14a9ab1043e13af09a1b9455b486401b46e",
        "luarocks-2.0.10-win32.zip": "bc00dbc80da6939f372bace50ea68d1746111280862858ecef9fcaaa3d70661f",
        "luarocks-2.0.11.tar.gz"   : "feee5a606938604f4fef1fdadc29692b9b7cdfb76fa537908d772adfb927741e",
        "luarocks-2.0.11-win32.zip": "b0c2c149da49d70972178e3aec0a92a678b3daa2993dd6d6cdd56269730f8e12",
        "luarocks-2.0.12.tar.gz"   : "ad4b465c5dfbdce436ef746a434317110d79f18ff79202a2697e215f4ac407ed",
        "luarocks-2.0.12-win32.zip": "dfb7c7429541628903ec811f151ea19435d2182a9515db57542f6825802a1ae7",
        "luarocks-2.0.13.tar.gz"   : "17db43664b555a467af74c91778d7e70937398da4325e3f88740621204a559a6",
        "luarocks-2.0.13-win32.zip": "8d867ced0f47ee1d5a9c4c3ef7f4969ae91f4a817b8755bb9595168b20398740",
        "luarocks-2.0.8.tar.gz"    : "f8abf1ab03b744a817721a0ff4a0ee454e068735efaa8d1aadcfcd0f07cdaa88",
        "luarocks-2.0.8-win32.zip" : "109e2dd91c66a7fd69471fcd56b3276f57aef334a4a8f53776b94b1ebd58334e",
        "luarocks-2.0.9.tar.gz"    : "4e25a8052c6abe1685da1093e1adb59aa034106c9d335aa932f7b3b51297c63d",
        "luarocks-2.0.9-win32.zip" : "c9389c288bac2c276e363ffbaaa6356119adefed243f0c47bf74611f9296bd94",
        "luarocks-2.1.0.tar.gz"    : "69bf4cb40c8010a5d434f70d26c9885f4260ac265fdaa848c0edb50cc8e53f88",
        "luarocks-2.1.0-win32.zip" : "363ecc0d09b70179735eef0dae158f98733e6d34226d6b5243bcbdc50d5987ca",
        "luarocks-2.1.1.tar.gz"    : "995ba1b9c982b503fd6fc61c905dc07c3a7533c06587616d9f00d9f62bd318ac",
        "luarocks-2.1.1-win32.zip" : "5fa8eccc91c7c1431480257cb1cf99fff902cf762576e1cd208762f01003e780",
        "luarocks-2.1.2.tar.gz"    : "62625c7609c886bae23f8db55dba45dbb083bae0d19bf12fe29ec95f7d389ff3",
        "luarocks-2.1.2-win32.zip" : "66beb4318261bc3e91544ba8672f04f3057137d32b2c33275ab6a355a7b5a546",
        "luarocks-2.2.0.tar.gz"    : "9b1a4ec7b103e2fb90a7ba8589d7e0c8523a3d6d54ac469b0bbc144292b9279c",
        "luarocks-2.2.0-win32.zip" : "0fb56f40f09352567c66318018b52b9fa9e055f318b8589abed24eb1e76a3def",
        "luarocks-2.2.1.tar.gz"    : "713f8a7e33f1e6dc77ba2eec849a80a95f24f82382e0abc4523c2b8d435f7c55",
        "luarocks-2.2.1-win32.zip" : "01b0410eb19f6e31342cbc12524f2e00eddfdf0bd9edcc325def7bcd93e331be",
        "luarocks-2.2.2.tar.gz"    : "4f0427706873f30d898aeb1dfb6001b8a3478e46a5249d015c061fe675a1f022",
        "luarocks-2.2.2-win32.zip" : "576721fb6fe224bbf5f60bd4c94c7c6f686889bb452ae1923a46d56f02df6588",
        "luarocks-2.3.0.tar.gz"    : "68e38feeb66052e29ad1935a71b875194ed8b9c67c2223af5f4d4e3e2464ed97",
        "luarocks-2.3.0-win32.zip" : "7aa02e7249906563a7ab8bb9db497cdeab0506328e4c8d45ffba120526dfec2a",
        "luarocks-2.4.0.tar.gz"    : "44381c9128d036247d428531291d1ff9405ae1daa238581d3c15f96d899497c3",
        "luarocks-2.4.0-win32.zip" : "13f92b46abc5d0362e2c3507f675b6d125b7c915680d48b62afa97b6b3e0f47a",
        "luarocks-2.4.1.tar.gz"    : "e429e0af9764bfd5cb640cac40f9d4ed1023fa17c052dff82ed0a41c05f3dcf9",
        "luarocks-2.4.1-win32.zip" : "c6cf36ca2e03b1a910e4dde9ac5c9360dc16f3f7afe50a978213d26728f4c667",
        "luarocks-2.4.2.tar.gz"    : "0e1ec34583e1b265e0fbafb64c8bd348705ad403fe85967fd05d3a659f74d2e5",
        "luarocks-2.4.2-win32.zip" : "63abc6f1240e0774f94bfe4150eaa5be06979c245db1dd5c8ddc4fb4570f7204",
        "luarocks-2.4.3.tar.gz"    : "4d414d32fed5bb121c72d3ff1280b7f2dc9027a9bc012e41dfbffd5b519b362e",
        "luarocks-2.4.3-win32.zip" : "08821ec39e7c3ad20f5b3d3e118ba8f1f5a7db6e6ad22e11eb5e8a2bdc95cbfb",
        "luarocks-2.4.4.tar.gz"    : "3938df33de33752ff2c526e604410af3dceb4b7ff06a770bc4a240de80a1f934",
        "luarocks-2.4.4-win32.zip" : "763d2fbe301b5f941dd5ea4aea485fb35e75cbbdceca8cc2f18726b75f9895c1",
        "luarocks-3.0.0.tar.gz"    : "a43fffb997100f11cccb529a3db5456ce8dab18171a5cb3645f948147b6f64a1",
        "luarocks-3.0.0-win32.zip" : "f5c6070f49f78ef61a2e5d6de353b34ef691ad4a6b45e065d5c85701a4a3a981",
        "luarocks-3.0.1.tar.gz"    : "b989c4b60d6c9edcd65169e5e42fcffbd39cdbebe6b138fa5aea45102f8d9ec0",
        "luarocks-3.0.1-win32.zip" : "af54263b8f71406d79556c880f3e2674e6690934a69cefbbdfd18710f05eeeaf",
        "luarocks-3.0.2.tar.gz"    : "3836267eff2f85fb552234e966602b1e649c58f81f47c7de3785e071c8127f5a",
        "luarocks-3.0.2-win32.zip" : "c9e93d7198f9ae7add331675d3d84fa1b61feb851814ee2a89b9930bd651bfb9",
        "luarocks-3.0.3.tar.gz"    : "f9a3fca236c87db55bc128a182ff605731ca15b43b1c4942d98f5e34acc88a6e",
        "luarocks-3.0.3-win32.zip" : "4fca0d87b9df7128a7d832027a5cda236569c9e5a2b037b6898f6b817c44028c",
        "luarocks-3.0.4.tar.gz"    : "1236a307ca5c556c4fed9fdbd35a7e0e80ccf063024becc8c3bf212f37ff0edf",
        "luarocks-3.0.4-win32.zip" : "ccd2313aff38fba5cf9704be6deafa552586c025c387f47679443d20fa89ba82",
        "luarocks-3.1.0.tar.gz"    : "865eae1e49b0f701c955c1c8f7b6fae99287c9cef32227d64177509224908921",
        "luarocks-3.1.0-win32.zip" : "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "luarocks-3.1.1.tar.gz"    : "3c26c102f8e69f81e12ea39037c770a00b6244e115a4c832e7a92feffdfad1aa",
        "luarocks-3.1.1-win32.zip" : "8001e4755647da9e37c06c932c42ef846140cb59a36b780b75d00448967994a2",
        "luarocks-3.1.2.tar.gz"    : "72a3b74f05b7fd011eed894dc34193ee80b3235fe58016ac9ffdbfceecc88950",
        "luarocks-3.1.2-win32.zip" : "b9aa7dba9bf1658c7103425c8b0252f09c17eafbd8fd77343ecba0d5172ef879",
        "luarocks-3.1.3.tar.gz"    : "c573435f495aac159e34eaa0a3847172a2298eb6295fcdc35d565f9f9b990513",
        "luarocks-3.1.3-win32.zip" : "1f78f8bfff82b2cdf57ccd853f21a4918927e5f5ba49731e3e406b3867769782",
        "luarocks-3.2.0.tar.gz"    : "66c1848a25924917ddc1901e865add8f19f2585360c44a001a03a8c234d3e796",
        "luarocks-3.2.0-win32.zip" : "aac1072ee867f62b23b278f7c6c60d7c6b2acf522633395c1f626a7c62bb44ac",
        "luarocks-3.2.1.tar.gz"    : "f27e20c9cdb3ffb991ccdb85796c36a0690566676f8e1a59b0d0ee6598907d04",
        "luarocks-3.2.1-win32.zip" : "8c2f3ea808759345d01fcf4a1108727e905c201768c458e82c88fd12c769cf60",
        "luarocks-3.3.0.tar.gz"    : "8de54eb851f5245ed3708d94d8872e825b9704049d3ad4febe8e219f419b427d",
        "luarocks-3.3.0-win32.zip" : "0c60d7e187b8547cfa4f44372a7d8cc50bafa48cdcea6eb6889094e4681718b4",
        "luarocks-3.3.1.tar.gz"    : "eb20cd9814df05535d9aae98da532217c590fc07d48d90ca237e2a7cdcf284fe",
        "luarocks-3.3.1-win32.zip" : "5ba3f6034e26e0bf93145354dca6aadb0bd7ad85c85e603a323787273db508bb",
        "luarocks-3.4.0.tar.gz"    : "62ce5826f0eeeb760d884ea8330cd1552b5d432138b8bade0fa72f35badd02d0",
        "luarocks-3.4.0-win32.zip" : "cd2b26d34f36cfbf69e3d80a8d25c46529e367f0866c41ac96049081a5653fc0",
        "luarocks-3.5.0.tar.gz"    : "701d0cc0c7e97cc2cf2c2f4068fce45e52a8854f5dc6c9e49e2014202eec9a4f",
        "luarocks-3.5.0-win32.zip" : "69a225f339a96cdbfe8eb5fcfb108c2eef270dab172bc09e78190a8a0932ffc8",
        "luarocks-3.6.0.tar.gz"    : "b0eaf59e7711ca2a886722c0423dabe22ccbdcdf3a042c3f2615596879f8252f",
        "luarocks-3.6.0-win32.zip" : "ac9cc1626c4c6171e2cc9259e788934737387c76578775769986924c21b6c590",
        "luarocks-3.7.0.tar.gz"    : "9255d97fee95cec5b54fc6ac718b11bf5029e45bed7873e053314919cd448551",
        "luarocks-3.7.0-win32.zip" : "bfd88072a9f8496839e9256735cd0b6a5a439ac9088e4dfbdc87e21658115504",
        "luarocks-3.8.0.tar.gz"    : "56ab9b90f5acbc42eb7a94cf482e6c058a63e8a1effdf572b8b2a6323a06d923",
        "luarocks-3.8.0-win32.zip" : "9cae5f3f61d49be2a8ee8eee124830c3d8012a9e3de57b119de3de626d3017fd",
        "luarocks-3.9.0.tar.gz"    : "5e840f0224891de96be4139e9475d3b1de7af3a32b95c1bdf05394563c60175f",
        "luarocks-3.9.0-win32.zip" : "3588107bddd9dee26dfb5b44ebc358a96ea52ec29cf3c46853680209192b3cb2",
        "luarocks-3.9.1.tar.gz"    : "ffafd83b1c42aa38042166a59ac3b618c838ce4e63f4ace9d961a5679ef58253",
        "luarocks-3.9.1-win32.zip" : "af6abff86c034a09d07308b4d352a3d2ea41c62e8b3715bc2697eb05e609e932",
        "luarocks-3.9.2.tar.gz"    : "bca6e4ecc02c203e070acdb5f586045d45c078896f6236eb46aa33ccd9b94edb",
        "luarocks-3.9.2-win32.zip" : "f4b644a63d88ce1869d7c5d7f442b7154824a84da83c40d3848243984d68a26e",
        "luarocks-3.10.0.tar.gz"   : "e9bf06d5ec6b8ecc6dbd1530d2d77bdb3377d814a197c46388e9f148548c1c89",
        "luarocks-3.10.0-win32.zip": "6f29d578b0ed607d225cff9decce8cd3ee09a04fe6ceabf8a8eed05e786c928b",
        "luarocks-3.11.0.tar.gz"   : "25f56b3c7272fb35b869049371d649a1bbe668a56d24df0a66e3712e35dd44a6",
        "luarocks-3.11.0-win32.zip": "85ddc54c57f6e5a66abb1d913f055a11ab320c701b3781957bc380ae34b4f652",
        "luarocks-3.11.1.tar.gz"   : "c3fb3d960dffb2b2fe9de7e3cb004dc4d0b34bb3d342578af84f84325c669102",
        "luarocks-3.11.1-win32.zip": "d5fb16455ad58c0f22621876d9c9f618216997e582f018a39008dd804daa0a85",
        "luarocks-3.12.0.tar.gz"   : "3d4c8acddf9b975e77da68cbf748d5baf483d0b6e9d703a844882db25dd61cdf",
        "luarocks-3.12.0-win32.zip": "e2ebc63dd1b731057a1453c49519a8bee0332ee2084318826a9dc02b690b351b",
        "luarocks-3.12.1.tar.gz"   : "f56b85a2a7a481f0321845807b79a05237860b04e4a9d186da632770029b3290",
        "luarocks-3.12.1-win32.zip": "9a2e65167e8887dd5e7225eb9caa5fc3958d587e9e000554e32a8449678b36b2 ",
        "luarocks-3.12.2.tar.gz"   : "b0e0c85205841ddd7be485f53d6125766d18a81d226588d2366931e9a1484492",
        "luarocks-3.12.2-win32.zip": "735f478c529aca5c52f16913b47b5a25dff8c1fb399cd3dbe179a73841d1bad7",
    }

    def get_download_name(self):
        return "{}-{}{}".format(self.name, self.version, "-win32.zip" if os.name == "nt" else ".tar.gz")

    def get_download_urls(self):
        return ["{}/{}".format(self.base_download_url, self.get_download_name())]

    def is_luarocks_2_0(self):
        if self.source == "release":
            return self.versions.index(self.version) < self.versions.index("2.1.0")

        with open("Makefile") as makefile:
            for line in makefile:
                if re.match(r"^\s*all:\s+built\s*$", line):
                    return True

        return False

    def get_cmake_generator(self):
        lua_target = self.lua_identifiers["target"]

        if lua_target == "mingw":
            return "MinGW Makefiles"
        elif lua_target.startswith("vs"):
            vs_year = self.lua_identifiers["vs year"]
            vs_arch = self.lua_identifiers["vs arch"]
            vs_short_version = vs_year_to_version[vs_year][:-2]
            return "Visual Studio {} 20{}{}".format(
                vs_short_version, vs_year, " Win64" if vs_arch == "x64" else "")

    @staticmethod
    def get_default_cflags():
        if using_cl():
            return "/nologo /MD /O2"
        elif opts.target == "mingw":
            return "-O2"
        else:
            return "-O2 -fPIC"

    def get_config_path(self):
        if os.name == "nt":
            return os.path.join(
                opts.location, "luarocks", "config-{}.lua".format(self.lua_identifiers["major version"]))
        else:
            return os.path.join(
                opts.location, "etc", "luarocks", "config-{}.lua".format(self.lua_identifiers["major version"]))

    def build(self):
        self.lua_identifiers = self.all_identifiers.get("lua",
                                                        self.all_identifiers.get("LuaJIT",
                                                                                 self.all_identifiers.get("moonjit",
                                                                                                          self.all_identifiers.get("raptorjit"))))

        if self.lua_identifiers is None:
            sys.exit("Error: can't install LuaRocks: Lua is not present in {}".format(opts.location))

        self.fetch()

        if os.name == "nt":
            print("Building and installing LuaRocks" + self.version_suffix)
            help_text = get_output("install.bat", "/?")
            args = [
                "install.bat",
                "/P", os.path.join(opts.location, "luarocks"),
                "/LUA", opts.location,
                "/F"
            ]
            if self.lua_identifiers["target"] == "mingw":
                args += ["/MW"]
            # Since LuaRocks 2.0.13
            if "/LV" in help_text:
                args += ["/LV", self.lua_identifiers["major version"]]
            # Since LuaRocks 2.1.2
            if "/NOREG" in help_text:
                args += ["/NOREG", "/Q"]
            if "/NOADMIN" in help_text:
                args += ["/NOADMIN"]

            run(args)

            for script in ["luarocks.bat", "luarocks-admin.bat"]:
                for subdir in [".", "2.2", "2.1", "2.0"]:
                    script_path = os.path.join(opts.location, "luarocks", subdir, script)

                    if os.path.exists(script_path):
                        shutil.copy(script_path, os.path.join(opts.location, "bin"))
                        break
                else:
                    sys.exit("Error: can't find {} in {}".format(script, os.path.join(opts.location, "luarocks")))

            cmake_generator = self.get_cmake_generator()

            if cmake_generator is not None:
                with open(self.get_config_path(), "a") as config_h:
                    config_h.write('\ncmake_generator = "{}"\n'.format(cmake_generator))

        else:
            print("Building LuaRocks" + self.version_suffix)
            run("./configure", "--prefix=" + opts.location,
                "--with-lua=" + opts.location)

            if self.is_luarocks_2_0():
                run("make")
            else:
                run("make", "build")

    def install(self):
        if os.name != "nt":
            print("Installing LuaRocks" + self.version_suffix)
            run("make", "install")

        if self.lua_identifiers["c flags"] != "":
            with open(self.get_config_path(), "a") as config_h:
                config_h.write('\nvariables = {{CFLAGS = "{} {}"}}\n'.format(self.get_default_cflags(), self.lua_identifiers["c flags"]))

def get_manifest_name():
    return os.path.join(opts.location, "hererocks.manifest")

manifest_version = 3

def get_installed_identifiers():
    if not os.path.exists(get_manifest_name()):
        return {}

    with open(get_manifest_name()) as manifest_h:
        try:
            identifiers = json.load(manifest_h)
        except ValueError:
            return {}

        if identifiers.get("version") == manifest_version:
            return identifiers
        else:
            return {}

def save_installed_identifiers(all_identifiers):
    all_identifiers["version"] = manifest_version

    with open(get_manifest_name(), "w") as manifest_h:
        json.dump(all_identifiers, manifest_h)

cl_version_to_vs_year = {
    "15": "08",
    "16": "10",
    "17": "12",
    "18": "13",
    "19": "15"
}

vs_year_to_version = {
    "08": "9.0",
    "10": "10.0",
    "12": "11.0",
    "13": "12.0",
    "15": "14.0"
}

@memoize
def get_vs_directory(vs_version):
    keys = [
        "Software\\Microsoft\\VisualStudio\\{}\\Setup\\VC".format(vs_version),
        "Software\\Microsoft\\VCExpress\\{}\\Setup\\VS".format(vs_version)
    ]

    for key in keys:
        vs_directory = query_registry(key, "ProductDir")

        if vs_directory is not None:
            return vs_directory

@memoize
def get_wsdk_directory(vs_version):
    if vs_version == "9.0":
        wsdk_version = "v6.1"
    elif vs_version == "10.0":
        wsdk_version = "v7.1"
    else:
        return

    return query_registry(
        "Software\\Microsoft\\Microsoft SDKs\\Windows\\{}".format(wsdk_version), "InstallationFolder")

vs_setup_scripts = {
    "x86": ["vcvars32.bat"],
    "x64": ["amd64\\vcvars64.bat", "x86_amd64\\vcvarsx86_amd64.bat"]
}

def get_vs_setup_cmd(vs_version, arch):
    vs_directory = get_vs_directory(vs_version)

    if vs_directory is not None:
        for script_path in vs_setup_scripts[arch]:
            full_script_path = os.path.join(vs_directory, "bin", script_path)

            if check_existence(full_script_path):
                return 'call "{}"'.format(full_script_path)

        vcvars_all_path = os.path.join(vs_directory, "vcvarsall.bat")

        if check_existence(vcvars_all_path):
            return 'call "{}"{}'.format(vcvars_all_path, " amd64" if arch == "x64" else "")

    wsdk_directory = get_wsdk_directory(vs_version)

    if wsdk_directory is not None:
        setenv_path = os.path.join(wsdk_directory, "bin", "setenv.cmd")

        if check_existence(setenv_path):
            return 'call "{}" /{}'.format(setenv_path, arch)

def setup_vs_and_rerun(vs_version, arch):
    vs_setup_cmd = get_vs_setup_cmd(vs_version, arch)

    if vs_setup_cmd is None:
        return

    print("Setting up VS {} ({})".format(vs_version, arch))
    bat_name = os.path.join(temp_dir, "hererocks.bat")
    argv_name = os.path.join(temp_dir, "argv")
    setup_output_name = os.path.join(temp_dir, "setup_out")

    script_arg = '"{}"'.format(inspect.getsourcefile(main))

    if sys.executable:
        script_arg = '"{}" {}'.format(sys.executable, script_arg)

    recursive_call = '{} --actual-argv-file "{}"'.format(script_arg, argv_name)

    bat_lines = [
        "@echo off",
        "setlocal enabledelayedexpansion enableextensions"
    ]

    if opts.verbose:
        bat_lines.extend([
            "echo Running {}".format(vs_setup_cmd),
            vs_setup_cmd
        ])
    else:
        bat_lines.append('{} > "{}" 2>&1'.format(vs_setup_cmd, setup_output_name))

    bat_lines.extend([
        "set exitcode=%errorlevel%",
        "if %exitcode% equ 0 (",
        "    {}".format(recursive_call),
        ") else ("
    ])

    if not opts.verbose:
        bat_lines.append('    type "{}"'.format(setup_output_name))

    bat_lines.extend([
        "    echo Error: got exitcode %exitcode% from command {}".format(vs_setup_cmd),
        "    exit /b 1",
        ")"
    ])

    with open(bat_name, "wb") as bat_h:
        bat_h.write("\r\n".join(bat_lines).encode("UTF-8"))

    with open(argv_name, "wb") as argv_h:
        argv_h.write("\r\n".join(sys.argv).encode("UTF-8"))

    exit_code = subprocess.call([bat_name])
    remove_dir(temp_dir)
    sys.exit(exit_code)

def setup_vs_by_vswhere(target):
    '''
    vswhere: https://github.com/Microsoft/vswhere
    detect Visual Studio 2017 versin 15.2 or later
    '''
    if target != "vs":
        return

    if program_exists("cl"):
        # already setup
        return

    vswhere = os.environ[
        'ProgramFiles(x86)'] + '\\Microsoft Visual Studio\\Installer\\vswhere.exe'
    if not os.path.exists(vswhere):
        return

    install_dir = run(vswhere, '-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath', get_output=True)
    vcvars = install_dir + '\\VC\\Auxiliary\\Build\\vcvars64.bat'
    if not os.path.exists(vcvars):
        return

    for line in run(os.environ['COMSPEC'], '/C', vcvars, '&', 'set', get_output=True).splitlines():
        try:
            k, v = line.split('=', 1)
            k = k.upper()
            if k == 'PATH':
                path = os.environ['PATH']
                for p in v.split(';'):
                    if p not in path:
                        path = p + ';' + path
                os.environ['PATH'] = path
            elif k == 'INCLUDE':
                os.environ['INCLUDE'] = v
            elif k == 'LIB':
                os.environ['LIB'] = v
        except ValueError:
            pass

def setup_vs(target):
    try:
        setup_vs_by_vswhere(target)
    except Exception:
        pass

    # If using vsXX_YY or vs_XX target, set VS up by writing a .bat file calling corresponding vcvarsall.bat
    # before recursively calling hererocks, passing arguments through a temporary file using
    # --actual-argv-file because passing special characters like '^' as an argument to a batch file is not fun.
    # If using vs target, do nothing if cl.exe is in PATH or setup latest possible VS, preferably with host arch.
    if target == "vs" and program_exists("cl"):
        print("Using cl.exe found in PATH.")
        return

    preferred_arch = "x64" if (platform.machine() if target == "vs" else target).endswith("64") else "x86"

    possible_arches = [preferred_arch]

    if target == "vs" and preferred_arch == "x64":
        possible_arches.append("x86")

    if target in ["vs", "vs_32", "vs_64"]:
        possible_versions = ["14.0", "12.0", "11.0", "10.0", "9.0"]
    else:
        possible_versions = [vs_year_to_version[target[2:4]]]

    for arch in possible_arches:
        for vs_version in possible_versions:
            setup_vs_and_rerun(vs_version, arch)

    sys.exit("Error: couldn't set up MSVC toolchain")

class UseActualArgsFileAction(argparse.Action):
    def __call__(self, parser, namespace, fname, option_string=None):
        with open(fname, "rb") as args_h:
            args_content = args_h.read().decode("UTF-8")

        main(args_content.split("\r\n")[1:])

def install_programs(vs_already_set_up):
    global temp_dir
    temp_dir = tempfile.mkdtemp()

    if (opts.lua or opts.luajit) and os.name == "nt" and not vs_already_set_up and using_cl():
        setup_vs(opts.target)

    start_dir = os.getcwd()
    opts.location = os.path.abspath(opts.location)

    if opts.downloads is not None:
        opts.downloads = os.path.abspath(opts.downloads)

    if opts.builds is not None:
        opts.builds = os.path.abspath(opts.builds)

    identifiers = get_installed_identifiers()

    if not os.path.exists(os.path.join(opts.location, "bin")):
        os.makedirs(os.path.join(opts.location, "bin"))

    write_activation_scripts()

    if opts.lua:
        if "LuaJIT" in identifiers:
            del identifiers["LuaJIT"]

        if RioLua(opts.lua).update_identifiers(identifiers):
            save_installed_identifiers(identifiers)

        os.chdir(start_dir)

    if opts.luajit:
        if "lua" in identifiers:
            del identifiers["lua"]

        if LuaJIT(opts.luajit).update_identifiers(identifiers):
            save_installed_identifiers(identifiers)

        os.chdir(start_dir)

    if opts.moonjit:
        if "lua" in identifiers:
            del identifiers["lua"]

        if MoonJIT(opts.moonjit).update_identifiers(identifiers):
            save_installed_identifiers(identifiers)

        os.chdir(start_dir)

    if opts.raptorjit:
        if "lua" in identifiers:
            del identifiers["lua"]

        if RaptorJIT(opts.raptorjit).update_identifiers(identifiers):
            save_installed_identifiers(identifiers)

        os.chdir(start_dir)

    if opts.luarocks:
        if LuaRocks(opts.luarocks).update_identifiers(identifiers):
            save_installed_identifiers(identifiers)

        os.chdir(start_dir)

    remove_dir(temp_dir)
    print("Done.")

def show_location():
    if os.path.exists(opts.location):
        all_identifiers = get_installed_identifiers()

        if all_identifiers:
            print("Programs installed in {}:".format(opts.location))

            for program in [RioLua, LuaJIT, LuaRocks]:
                if program.name in all_identifiers:
                    show_identifiers(all_identifiers[program.name])
        else:
            print("No programs installed in {}.".format(opts.location))
    else:
        print("{} does not exist.".format(opts.location))

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=hererocks_version + ", a tool for installing Lua and/or LuaRocks locally.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter, add_help=False)
    parser.add_argument(
        "location", help="Path to directory in which Lua and/or LuaRocks will be installed. "
        "Their binaries and activation scripts will be found in its 'bin' subdirectory. "
        "Scripts from modules installed using LuaRocks will also turn up there. "
        "If an incompatible version of Lua is already installed there it should be "
        "removed before installing the new one.")
    parser.add_argument(
        "-l", "--lua", help="Version of standard PUC-Rio Lua to install. "
        "Version can be specified as a version number, e.g. 5.2 or 5.3.1. "
        "Versions 5.1.0 - 5.4.8 and 5.5.0-rc1 are supported. "
        "'latest' and '^' are aliases for 5.4.8. "
        "If the argument contains '@', sources will be downloaded "
        "from a git repo using URI before '@' and using part after '@' as git reference "
        "to checkout, 'master' by default. "
        "Default git repo is https://github.com/lua/lua. "
        "The argument can also be a path to local directory. "
        "When installing PUC-Rio Lua from a git repo or a local directory, "
        "source files are expected to be in the root directory instead of 'src'. ")
    parser.add_argument(
        "-j", "--luajit", help="Version of LuaJIT to install. "
        "Version can be specified in the same way as for standard Lua. "
        "Versions 2.0.0 - 2.1.0-beta3 are supported. "
        "'latest' and '^' are aliases for to 2.0.5. "
        "Default git repo is https://github.com/luajit/luajit. ")
    parser.add_argument(
        "-m", "--moonjit", help="Version of moonjit to install. "
        "Version can be specified in the same way as for standard Lua. "
        "Versions 2.1.1 - 2.2.0 are supported. "
        "'latest' and '^' are aliases for to 2.1.2. "
        "Default git repo is https://github.com/moonjit/moonjit. ")
    parser.add_argument(
        "--raptorjit", help="Version of RaptorJIT to install. "
        "Version can be specified in the same way as for standard Lua. "
        "Versions 1.0.0 - 1.0.3 are supported. "
        "'latest' and '^' are aliases for to 1.0.3. "
        "Default git repo is https://github.com/raptorjit/raptorjit. ")
    parser.add_argument(
        "-r", "--luarocks", help="Version of LuaRocks to install. "
        "Version can be specified in the same way as for standard Lua. "
        "Versions 2.0.8 - 3.12.2 are supported. "
        "'latest' and '^' are aliases for 3.12.2. "
        "Default git repo is https://github.com/luarocks/luarocks. "
        "Note that Lua 5.2 is not supported in LuaRocks 2.0.8, "
        "Lua 5.3 is supported only since LuaRocks 2.2.0, Lua 5.4 is supported only since "
        "LuaRocks 3.0.0, and RaptorJIT is supported only since LuaRocks 3.2.0.")
    parser.add_argument("--show", default=False, action="store_true",
                        help="Show programs installed in <location>, possibly after installing new ones.")
    parser.add_argument("-i", "--ignore-installed", default=False, action="store_true",
                        help="Install even if requested version is already present.")
    parser.add_argument(
        "--compat", default="default", choices=["default", "none", "all", "5.1", "5.2"],
        help="Select compatibility flags for Lua.")
    parser.add_argument(
        "--patch", default=False, action="store_true",
        help="Apply latest PUC-Rio Lua patches from https://www.lua.org/bugs.html when available.")
    parser.add_argument(
        "--cflags", default=None,
        help="Pass additional options to C compiler when building Lua or LuaJIT.")
    parser.add_argument(
        "--target", help="Select how to build Lua. "
        "Windows-specific targets (mingw, vs, vs_XX and vsXX_YY) also affect LuaJIT. "
        "vs, vs_XX and vsXX_YY targets compile using cl.exe. "
        "vsXX_YY targets (such as vs15_32) always set up Visual Studio 20XX (YYbit). "
        "vs_32 and vs_64 pick latest version supporting selected architecture. "
        "vs target uses cl.exe that's already in PATH or sets up "
        "latest available Visual Studio, preferring tools for host architecture. "
        "It's the default target on Windows unless cl.exe is not in PATH but gcc is, "
        "in which case mingw target is used. "
        "macosx target uses cc and the remaining targets use gcc, passing compiler "
        "and linker flags the same way Lua's Makefile does when running make <target>.",
        choices=[
            "linux", "macosx", "freebsd", "mingw", "posix", "generic", "vs", "vs_32", "vs_64",
            "vs08_32", "vs08_64", "vs10_32", "vs10_64", "vs12_32", "vs12_64",
            "vs13_32", "vs13_64", "vs15_32", "vs15_64"
        ], metavar="{linux,macosx,freebsd,mingw,posix,generic,vs,vs_XX,vsXX_YY}",
        default=get_default_lua_target())
    parser.add_argument("--no-readline", help="Don't use readline library when building standard Lua.",
                        action="store_true", default=False)
    parser.add_argument("--timeout",
                        help="Download timeout in seconds.",
                        type=int, default=60)
    parser.add_argument("--downloads",
                        help="Cache downloads and default git repos in 'DOWNLOADS' directory.",
                        default=get_default_cache())
    parser.add_argument("--no-git-cache",
                        help="Do not cache default git repos.",
                        action="store_true", default=False)
    parser.add_argument("--ignore-checksums",
                        help="Ignore checksum mismatches for downloads.",
                        action="store_true", default=False)
    parser.add_argument("--builds",
                        help="Cache Lua and LuaJIT builds in 'BUILDS' directory. "
                        "A cached build is used when installing same program into "
                        "same location with same options.", default=None)
    parser.add_argument("--verbose", default=False, action="store_true",
                        help="Show executed commands and their output.")
    parser.add_argument("-v", "--version", help="Show program's version number and exit.",
                        action="version", version=hererocks_version)
    parser.add_argument("-h", "--help", help="Show this help message and exit.", action="help")

    if os.name == "nt" and argv is None:
        parser.add_argument("--actual-argv-file", action=UseActualArgsFileAction,
                            # help="Load argv from a file, used when setting up cl toolchain."
                            help=argparse.SUPPRESS)

    global opts
    opts = parser.parse_args(argv)
    nb_lua = 0
    if opts.lua:
        nb_lua += 1
    if opts.luajit:
        nb_lua += 1
    if opts.moonjit:
        nb_lua += 1
    if opts.raptorjit:
        nb_lua += 1

    if nb_lua == 0 and not opts.luarocks and not opts.show:
        parser.error("a version of Lua, LuaJIT, moonjit, RaptorJIT or LuaRocks needs to be specified unless --show is used")

    if nb_lua > 1:
        parser.error("can't install more than one Lua interpreter")

    if nb_lua == 1 or opts.luarocks:
        install_programs(argv is not None)

    if opts.show:
        show_location()

    sys.exit(0)

if __name__ == "__main__":
    main()
