# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""
The pex.bin.pex utility builds PEX environments and .pex files specified by
sources, requirements and their dependencies.
"""

from __future__ import absolute_import, print_function

import json
import os
import sys
import tempfile
from argparse import Action, ArgumentDefaultsHelpFormatter, ArgumentParser, ArgumentTypeError
from textwrap import TextWrapper

from pex import pex_warnings
from pex.argparse import HandleBoolAction
from pex.common import die, safe_mkdtemp
from pex.enum import Enum
from pex.inherit_path import InheritPath
from pex.interpreter import PythonInterpreter
from pex.interpreter_constraints import (
    UnsatisfiableInterpreterConstraintsError,
    validate_constraints,
)
from pex.layout import Layout, maybe_install
from pex.orderedset import OrderedSet
from pex.pex import PEX
from pex.pex_bootstrapper import ensure_venv, iter_compatible_interpreters
from pex.pex_builder import CopyMode, PEXBuilder
from pex.pex_info import PexInfo
from pex.platforms import Platform
from pex.resolve import resolve_options
from pex.resolve.resolve_configuration import PexRepository, ResolveConfiguration
from pex.resolver import Unsatisfiable, parsed_platform, resolve, resolve_from_pex
from pex.tracer import TRACER
from pex.typing import TYPE_CHECKING, cast
from pex.variables import ENV, Variables
from pex.venv_bin_path import BinPath
from pex.version import __version__

if TYPE_CHECKING:
    from typing import Dict, Iterable, Optional
    from argparse import Namespace


CANNOT_SETUP_INTERPRETER = 102
INVALID_OPTIONS = 103


def log(msg, V=0):
    if V != 0:
        print(msg, file=sys.stderr)


class HandleVenvAction(Action):
    def __init__(self, *args, **kwargs):
        kwargs["nargs"] = "?"
        kwargs["choices"] = (BinPath.PREPEND.value, BinPath.APPEND.value)
        super(HandleVenvAction, self).__init__(*args, **kwargs)

    def __call__(self, parser, namespace, value, option_str=None):
        bin_path = BinPath.FALSE if value is None else BinPath.for_value(value)
        setattr(namespace, self.dest, bin_path)


class PrintVariableHelpAction(Action):
    def __call__(self, parser, namespace, values, option_str=None):
        for variable_name, variable_type, variable_help in Variables.iter_help():
            print("\n%s: %s\n" % (variable_name, variable_type))
            for line in TextWrapper(initial_indent=" " * 4, subsequent_indent=" " * 4).wrap(
                variable_help
            ):
                print(line)
        sys.exit(0)


def process_platform(option_str):
    try:
        return parsed_platform(option_str)
    except Platform.InvalidPlatformError as e:
        raise ArgumentTypeError("{} is an invalid platform:\n{}".format(option_str, e))


def configure_clp_pex_resolution(parser):
    # type: (ArgumentParser) -> None
    group = resolve_options.register(parser)

    group.add_argument(
        "--pex-path",
        dest="pex_path",
        type=str,
        default=None,
        help="A colon separated list of other pex files to merge into the runtime environment.",
    )
    group.add_argument(
        "--disable-cache",
        dest="disable_cache",
        default=False,
        action="store_true",
        help="Disable caching in the pex tool entirely.",
    )

    group.add_argument(
        "--cache-dir",
        dest="cache_dir",
        default=None,
        help=(
            "DEPRECATED: Use --pex-root instead. The local cache directory to use for speeding up "
            "requirement lookups."
        ),
    )


def configure_clp_pex_options(parser):
    # type: (ArgumentParser) -> None
    group = parser.add_argument_group(
        "PEX output options",
        "Tailor the behavior of the emitted .pex file if -o is specified.",
    )

    group.add_argument(
        "--include-tools",
        dest="include_tools",
        default=False,
        action=HandleBoolAction,
        help="Whether to include runtime tools in the pex file. If included, these can be run by "
        "exporting PEX_TOOLS=1 and following the usage and --help information.",
    )

    group.add_argument(
        "--zip-safe",
        "--not-zip-safe",
        dest="zip_safe",
        metavar="DEPRECATED",
        default=None,
        action=HandleBoolAction,
        help=(
            "Deprecated: This option is no longer used since user code is now always unzipped "
            "before execution."
        ),
    )
    group.add_argument(
        "--layout",
        dest="layout",
        default=Layout.ZIPAPP,
        choices=Layout.values(),
        type=Layout.for_value,
        help=(
            "By default, a PEX is created as a single file zipapp when `-o` is specified, but "
            "either a packed or loose directory tree based layout can be chosen instead. A packed "
            "layout PEX is an executable directory structure designed to have cache-friendly "
            "characteristics for syncing incremental updates to PEXed applications over a network. "
            "At the top level of the packed directory tree there is an executable `__main__.py`"
            "script. The directory can also be executed by passing its path to a Python "
            "executable; e.g: `python packed-pex-dir/`. The Pex bootstrap code and all dependency "
            "code are packed into individual zip files for efficient caching and syncing. A loose "
            "layout PEX is similar to a packed PEX, except that neither the Pex bootstrap code nor "
            "the dependency code are packed into zip files, but are instead present as collections "
            "of loose files in the directory tree providing different caching and syncing "
            "tradeoffs. Both zipapp and packed layouts install themselves in the PEX_ROOT as loose "
            "apps by default before executing, but these layouts compose with `--venv` execution "
            "mode as well and support `--seed`ing."
        ),
    )

    runtime_mode = group.add_mutually_exclusive_group()
    runtime_mode.add_argument(
        "--unzip",
        "--no-unzip",
        dest="unzip",
        metavar="DEPRECATED",
        default=None,
        action=HandleBoolAction,
        help=(
            "Deprecated: This option is no longer used since unzipping PEX zip files before "
            "execution is now the default."
        ),
    )
    runtime_mode.add_argument(
        "--venv",
        dest="venv",
        metavar="{prepend,append}",
        default=False,
        action=HandleVenvAction,
        help="Convert the pex file to a venv before executing it. If 'prepend' or 'append' is "
        "specified, then all scripts and console scripts provided by distributions in the pex file "
        "will be added to the PATH in the corresponding position. If the the pex file will be run "
        "multiple times under a stable runtime PEX_ROOT, the venv creation will only be done once "
        "and subsequent runs will enjoy lower startup latency.",
    )
    group.add_argument(
        "--venv-copies",
        "--no-venv-copies",
        dest="venv_copies",
        default=False,
        action=HandleBoolAction,
        help=(
            "If --venv is specified, create the venv using copies of base interpreter files "
            "instead of symlinks."
        ),
    )

    group.add_argument(
        "--always-write-cache",
        dest="always_write_cache",
        default=None,
        action="store_true",
        help=(
            "Deprecated: This option is no longer used; all internally cached distributions in a "
            "PEX are always installed into the local Pex dependency cache."
        ),
    )

    group.add_argument(
        "--ignore-errors",
        dest="ignore_errors",
        default=False,
        action="store_true",
        help="Ignore requirement resolution solver errors when building pexes and later invoking "
        "them.",
    )

    group.add_argument(
        "--inherit-path",
        dest="inherit_path",
        default=InheritPath.FALSE,
        choices=InheritPath.values(),
        type=InheritPath.for_value,
        help="Inherit the contents of sys.path (including site-packages, user site-packages and "
        "PYTHONPATH) running the pex. Possible values: {false} (does not inherit sys.path), "
        "{fallback} (inherits sys.path after packaged dependencies), {prefer} (inherits sys.path "
        "before packaged dependencies), No value (alias for prefer, for backwards "
        "compatibility).".format(
            false=InheritPath.FALSE, fallback=InheritPath.FALLBACK, prefer=InheritPath.PREFER
        ),
    )

    group.add_argument(
        "--compile",
        "--no-compile",
        dest="compile",
        default=False,
        action=HandleBoolAction,
        help="Compiling means that the built pex will include .pyc files, which will result in "
        "slightly faster startup performance. However, compiling means that the generated pex "
        "likely will not be reproducible, meaning that if you were to run `./pex -o` with the "
        "same inputs then the new pex would not be byte-for-byte identical to the original.",
    )

    group.add_argument(
        "--use-system-time",
        "--no-use-system-time",
        dest="use_system_time",
        default=False,
        action=HandleBoolAction,
        help="Use the current system time to generate timestamps for the new pex. Otherwise, Pex "
        "will use midnight on January 1, 1980. By using system time, the generated pex "
        "will not be reproducible, meaning that if you were to run `./pex -o` with the "
        "same inputs then the new pex would not be byte-for-byte identical to the original.",
    )

    group.add_argument(
        "--runtime-pex-root",
        dest="runtime_pex_root",
        default=None,
        help="Specify the pex root to be used in the generated .pex file (if unspecified, "
        "uses ~/.pex).",
    )

    group.add_argument(
        "--strip-pex-env",
        "--no-strip-pex-env",
        dest="strip_pex_env",
        default=True,
        action=HandleBoolAction,
        help="Strip all `PEX_*` environment variables used to control the pex runtime before handing "
        "off control to the pex entrypoint. You might want to set this to `False` if the new "
        "pex executes other pexes (or the Pex CLI itself) and you want the executed pex to be "
        "controllable via `PEX_*` environment variables.",
    )


def configure_clp_pex_environment(parser):
    # type: (ArgumentParser) -> None
    group = parser.add_argument_group(
        "PEX environment options",
        "Tailor the interpreter and platform targets for the PEX environment.",
    )

    group.add_argument(
        "--python",
        dest="python",
        default=[],
        type=str,
        action="append",
        help=(
            "The Python interpreter to use to build the PEX (default: current interpreter). This "
            "cannot be used with `--interpreter-constraint`, which will instead cause PEX to "
            "search for valid interpreters. Either specify an absolute path to an interpreter, or "
            "specify a binary accessible on $PATH like `python3.7`. This option can be passed "
            "multiple times to create a multi-interpreter compatible PEX."
        ),
    )
    group.add_argument(
        "--python-path",
        dest="python_path",
        default=None,
        type=str,
        help=(
            "Colon-separated paths to search for interpreters when `--interpreter-constraint` "
            "and/or `--resolve-local-platforms` are specified (default: $PATH). Each element "
            "can be the absolute path of an interpreter binary or a directory containing "
            "interpreter binaries."
        ),
    )

    current_interpreter = PythonInterpreter.get()
    program = sys.argv[0]
    singe_interpreter_info_cmd = (
        "PEX_TOOLS=1 {current_interpreter} {program} interpreter --verbose --indent 4".format(
            current_interpreter=current_interpreter.binary, program=program
        )
    )
    all_interpreters_info_cmd = (
        "PEX_TOOLS=1 {program} interpreter --all --verbose --indent 4".format(program=program)
    )

    group.add_argument(
        "--interpreter-constraint",
        dest="interpreter_constraint",
        default=[],
        type=str,
        action="append",
        help=(
            "Constrain the selected Python interpreter. Specify with Requirement-style syntax, "
            'e.g. "CPython>=2.7,<3" (A CPython interpreter with version >=2.7 AND version <3), '
            '">=2.7,<3" (Any Python interpreter with version >=2.7 AND version <3) or "PyPy" (A '
            "PyPy interpreter of any version). This argument may be repeated multiple times to OR "
            "the constraints. Try `{singe_interpreter_info_cmd}` to find the exact interpreter "
            "constraints of {current_interpreter} and `{all_interpreters_info_cmd}` to find out "
            "the interpreter constraints of all Python interpreters on the $PATH.".format(
                current_interpreter=current_interpreter.binary,
                singe_interpreter_info_cmd=singe_interpreter_info_cmd,
                all_interpreters_info_cmd=all_interpreters_info_cmd,
            )
        ),
    )

    group.add_argument(
        "--rcfile",
        dest="rc_file",
        default=None,
        help=(
            "An additional path to a pexrc file to read during configuration parsing, in addition "
            "to reading `/etc/pexrc` and `~/.pexrc`. If `PEX_IGNORE_RCFILES=true`, then all rc "
            "files will be ignored."
        ),
    )

    group.add_argument(
        "--python-shebang",
        dest="python_shebang",
        default=None,
        help="The exact shebang (#!...) line to add at the top of the PEX file minus the "
        "#!. This overrides the default behavior, which picks an environment Python "
        "interpreter compatible with the one used to build the PEX file.",
    )

    group.add_argument(
        "--platform",
        dest="platforms",
        default=[],
        type=process_platform,
        action="append",
        help=(
            "The platform for which to build the PEX. This option can be passed multiple times "
            "to create a multi-platform pex. To use the platform corresponding to the current "
            "interpreter you can pass `current`. To target any other platform you pass a string "
            "composed of fields: <platform>-<python impl abbr>-<python version>-<abi>. "
            "These fields stem from wheel name conventions as outlined in "
            "https://www.python.org/dev/peps/pep-0427#file-name-convention and influenced by "
            "https://www.python.org/dev/peps/pep-0425. For the current interpreter at "
            "{current_interpreter} the full platform string is {current_platform}. To find out "
            "more, try `{all_interpreters_info_cmd}` to print out the platform for all "
            "interpreters on the $PATH or `{singe_interpreter_info_cmd}` to inspect the single "
            "interpreter {current_interpreter}.".format(
                current_interpreter=current_interpreter.binary,
                current_platform=current_interpreter.platform,
                singe_interpreter_info_cmd=singe_interpreter_info_cmd,
                all_interpreters_info_cmd=all_interpreters_info_cmd,
            )
        ),
    )

    group.add_argument(
        "--resolve-local-platforms",
        dest="resolve_local_platforms",
        default=False,
        action=HandleBoolAction,
        help="When --platforms are specified, attempt to resolve a local interpreter that matches "
        "each platform specified. If found, use the interpreter to resolve distributions; if "
        "not (or if this option is not specified), resolve for each platform only allowing "
        "matching binary distributions and failing if only sdists or non-matching binary "
        "distributions can be found.",
    )


def configure_clp_pex_entry_points(parser):
    # type: (ArgumentParser) -> None
    group = parser.add_argument_group(
        "PEX entry point options",
        "Specify what target/module the PEX should invoke if any.",
    )

    group.add_argument(
        "-m",
        "-e",
        "--entry-point",
        dest="entry_point",
        metavar="MODULE[:SYMBOL]",
        default=None,
        help="Set the entry point to module or module:symbol.  If just specifying module, pex "
        "behaves like python -m, e.g. python -m SimpleHTTPServer.  If specifying "
        "module:symbol, pex assume symbol is a n0-arg callable and imports that symbol and invokes "
        "it as if via `sys.exit(symbol())`.",
    )

    group.add_argument(
        "-c",
        "--script",
        "--console-script",
        dest="script",
        default=None,
        metavar="SCRIPT_NAME",
        help="Set the entry point as to the script or console_script as defined by a any of the "
        'distributions in the pex.  For example: "pex -c fab fabric" or "pex -c mturk boto".',
    )

    group.add_argument(
        "--validate-entry-point",
        dest="validate_ep",
        default=False,
        action="store_true",
        help="Validate the entry point by importing it in separate process. Warning: this could have "
        "side effects. For example, entry point `a.b.c:m` will translate to "
        "`from a.b.c import m` during validation.",
    )


class Seed(Enum["Seed.Value"]):
    class Value(Enum.Value):
        pass

    NONE = Value("none")
    ARGS = Value("args")
    VERBOSE = Value("verbose")

    @classmethod
    def values(cls):
        # type: () -> Iterable[Seed.Value]
        return cls.NONE, cls.ARGS, cls.VERBOSE


class HandleSeedAction(Action):
    def __init__(self, *args, **kwargs):
        kwargs["nargs"] = "?"
        kwargs["choices"] = [seed.value for seed in Seed.values()]
        super(HandleSeedAction, self).__init__(*args, **kwargs)

    def __call__(self, parser, namespace, value, option_str=None):
        seed = Seed.ARGS if value is None else Seed.for_value(value)
        setattr(namespace, self.dest, seed)


def configure_clp():
    # type: () -> ArgumentParser
    usage = (
        "%(prog)s [-o OUTPUT.PEX] [options] [-- arg1 arg2 ...]\n\n"
        "%(prog)s builds a PEX (Python Executable) file based on the given specifications: "
        "sources, requirements, their dependencies and other options."
        "\n"
        "Command-line options can be provided in one or more files by prefixing the filenames "
        "with an @ symbol. These files must contain one argument per line."
    )

    parser = ArgumentParser(
        usage=usage,
        formatter_class=ArgumentDefaultsHelpFormatter,
        fromfile_prefix_chars="@",
    )

    parser.add_argument("-V", "--version", action="version", version=__version__)
    parser.add_argument("requirements", nargs="*", help="Requirements to add to the pex")

    configure_clp_pex_resolution(parser)
    configure_clp_pex_options(parser)
    configure_clp_pex_environment(parser)
    configure_clp_pex_entry_points(parser)

    parser.add_argument(
        "-o",
        "--output-file",
        dest="pex_name",
        default=None,
        help="The name of the generated .pex file: Omitting this will run PEX "
        "immediately and not save it to a file.",
    )

    parser.add_argument(
        "-p",
        "--preamble-file",
        dest="preamble_file",
        metavar="FILE",
        default=None,
        type=str,
        help="The name of a file to be included as the preamble for the generated .pex file",
    )

    parser.add_argument(
        "-D",
        "--sources-directory",
        dest="sources_directory",
        metavar="DIR",
        default=[],
        type=str,
        action="append",
        help=(
            "Add a directory containing sources and/or resources to be packaged into the generated "
            ".pex file. This option can be used multiple times."
        ),
    )

    parser.add_argument(
        "-R",
        "--resources-directory",
        dest="resources_directory",
        metavar="DIR",
        default=[],
        type=str,
        action="append",
        help=(
            "Add resources directory to be packaged into the generated .pex file."
            " This option can be used multiple times. DEPRECATED: Use -D/--sources-directory "
            "instead."
        ),
    )

    parser.add_argument(
        "-r",
        "--requirement",
        dest="requirement_files",
        metavar="FILE or URL",
        default=[],
        type=str,
        action="append",
        help="Add requirements from the given requirements file.  This option can be used multiple "
        "times.",
    )

    parser.add_argument(
        "--constraints",
        dest="constraint_files",
        metavar="FILE or URL",
        default=[],
        type=str,
        action="append",
        help="Add constraints from the given constraints file.  This option can be used multiple "
        "times.",
    )

    parser.add_argument(
        "--requirements-pex",
        dest="requirements_pexes",
        metavar="FILE",
        default=[],
        type=str,
        action="append",
        help="Add requirements from the given .pex file.  This option can be used multiple times.",
    )

    parser.add_argument(
        "-v",
        dest="verbosity",
        action="count",
        default=0,
        help="Turn on logging verbosity, may be specified multiple times.",
    )

    parser.add_argument(
        "--emit-warnings",
        "--no-emit-warnings",
        dest="emit_warnings",
        action=HandleBoolAction,
        default=True,
        help="Emit runtime UserWarnings on stderr. If false, only emit them when PEX_VERBOSE is set.",
    )

    parser.add_argument(
        "--pex-root",
        dest="pex_root",
        default=None,
        help="Specify the pex root used in this invocation of pex "
        "(if unspecified, uses {}).".format(ENV.PEX_ROOT),
    )

    parser.add_argument(
        "--tmpdir",
        dest="tmpdir",
        default=tempfile.gettempdir(),
        help="Specify the temporary directory Pex and its subprocesses should use.",
    )

    parser.add_argument(
        "--seed",
        dest="seed",
        action=HandleSeedAction,
        default=Seed.NONE,
        help=(
            "Seed local Pex caches for the generated PEX and print out the command line to run "
            "directly from the seed with ({args}) or else a json object including the 'pex_root' "
            "path, the 'python' binary path and the seeded 'pex' path ({seed}).".format(
                args=Seed.ARGS, seed=Seed.VERBOSE
            )
        ),
    )
    parser.add_argument(
        "--no-seed",
        dest="seed",
        action="store_const",
        const=Seed.NONE,
        metavar="DEPRECATED",
        help="Deprecated: Use --seed=none instead.",
    )

    parser.add_argument(
        "--help-variables",
        action=PrintVariableHelpAction,
        nargs=0,
        help="Print out help about the various environment variables used to change the behavior of "
        "a running PEX file.",
    )

    return parser


def build_pex(
    reqs,  # type: Optional[Iterable[str]]
    resolve_configuration,  # type: ResolveConfiguration
    options,  # type: Namespace
    cache=None,  # type: Optional[str]
):
    # type: (...) -> PEXBuilder

    interpreters = None  # Default to the current interpreter.

    pex_python_path = options.python_path  # If None, this will result in using $PATH.
    # TODO(#1075): stop looking at PEX_PYTHON_PATH and solely consult the `--python-path` flag.
    if pex_python_path is None and (options.rc_file or not ENV.PEX_IGNORE_RCFILES):
        rc_variables = Variables(rc=options.rc_file)
        pex_python_path = rc_variables.PEX_PYTHON_PATH

    # NB: options.python and interpreter constraints cannot be used together.
    if options.python:
        with TRACER.timed("Resolving interpreters", V=2):

            def to_python_interpreter(full_path_or_basename):
                if os.path.isfile(full_path_or_basename):
                    return PythonInterpreter.from_binary(full_path_or_basename)
                else:
                    interp = PythonInterpreter.from_env(full_path_or_basename)
                    if interp is None:
                        die("Failed to find interpreter: %s" % full_path_or_basename)
                    return interp

            interpreters = [to_python_interpreter(interp) for interp in options.python]
    elif options.interpreter_constraint:
        with TRACER.timed("Resolving interpreters", V=2):
            constraints = options.interpreter_constraint
            validate_constraints(constraints)
            try:
                interpreters = list(
                    iter_compatible_interpreters(
                        path=pex_python_path, interpreter_constraints=constraints
                    )
                )
            except UnsatisfiableInterpreterConstraintsError as e:
                die(
                    e.create_message("Could not find a compatible interpreter."),
                    CANNOT_SETUP_INTERPRETER,
                )

    platforms = OrderedSet(options.platforms)  # type: OrderedSet[Platform]
    interpreters = interpreters or []
    if options.platforms and options.resolve_local_platforms:
        with TRACER.timed(
            "Searching for local interpreters matching {}".format(", ".join(map(str, platforms)))
        ):
            candidate_interpreters = OrderedSet(iter_compatible_interpreters(path=pex_python_path))
            candidate_interpreters.add(PythonInterpreter.get())
            for candidate_interpreter in candidate_interpreters:
                resolved_platforms = candidate_interpreter.supported_platforms.intersection(
                    platforms
                )
                if resolved_platforms:
                    for resolved_platform in resolved_platforms:
                        TRACER.log(
                            "Resolved {} for platform {}".format(
                                candidate_interpreter, resolved_platform
                            )
                        )
                        platforms.remove(resolved_platform)
                    interpreters.append(candidate_interpreter)
        if platforms:
            TRACER.log(
                "Could not resolve a local interpreter for {}, will resolve only binary distributions "
                "for {}.".format(
                    ", ".join(map(str, platforms)),
                    "this platform" if len(platforms) == 1 else "these platforms",
                )
            )

    interpreter = (
        PythonInterpreter.latest_release_of_min_compatible_version(interpreters)
        if interpreters
        else None
    )

    preamble = None  # type: Optional[str]
    if options.preamble_file:
        with open(options.preamble_file) as preamble_fd:
            preamble = preamble_fd.read()

    pex_builder = PEXBuilder(
        path=safe_mkdtemp(),
        interpreter=interpreter,
        preamble=preamble,
        copy_mode=CopyMode.SYMLINK,
    )

    if options.resources_directory:
        pex_warnings.warn(
            "The `-R/--resources-directory` option is deprecated. Resources should be added via "
            "`-D/--sources-directory` instead."
        )

    if options.zip_safe is not None:
        pex_warnings.warn(
            "The `--zip-safe/--not-zip-safe` option is deprecated. This option is no longer used "
            "since user code is now always unzipped before execution."
        )

    if options.unzip is not None:
        pex_warnings.warn(
            "The `--unzip/--no-unzip` option is deprecated. This option is no longer used since "
            "unzipping PEX zip files before execution is now the default."
        )

    if options.always_write_cache is not None:
        pex_warnings.warn(
            "The `--always-write-cache` option is deprecated. This option is no longer used; all "
            "internally cached distributions in a PEX are always installed into the local Pex "
            "dependency cache."
        )

    directories = OrderedSet(
        options.sources_directory + options.resources_directory
    )  # type: OrderedSet[str]
    for directory in directories:
        src_dir = os.path.normpath(directory)
        for root, _, files in os.walk(src_dir):
            for f in files:
                src_file_path = os.path.join(root, f)
                dst_path = os.path.relpath(src_file_path, src_dir)
                pex_builder.add_source(src_file_path, dst_path)

    pex_info = pex_builder.info
    pex_info.venv = bool(options.venv)
    pex_info.venv_bin_path = options.venv or BinPath.FALSE
    pex_info.venv_copies = options.venv_copies
    pex_info.includes_tools = options.include_tools or options.venv
    pex_info.pex_path = options.pex_path
    pex_info.ignore_errors = options.ignore_errors
    pex_info.emit_warnings = options.emit_warnings
    pex_info.inherit_path = options.inherit_path
    pex_info.pex_root = options.runtime_pex_root
    pex_info.strip_pex_env = options.strip_pex_env

    if options.interpreter_constraint:
        for ic in options.interpreter_constraint:
            pex_builder.add_interpreter_constraint(ic)

    for requirements_pex in options.requirements_pexes:
        pex_builder.add_from_requirements_pex(requirements_pex)

    with TRACER.timed("Resolving distributions ({})".format(reqs + options.requirement_files)):
        try:
            if isinstance(resolve_configuration.repository, PexRepository):
                with TRACER.timed(
                    "Resolving requirements from PEX {}.".format(options.pex_repository)
                ):
                    result = resolve_from_pex(
                        pex=resolve_configuration.repository,
                        requirements=reqs,
                        requirement_files=options.requirement_files,
                        constraint_files=options.constraint_files,
                        network_configuration=resolve_configuration.network_configuration,
                        transitive=resolve_configuration.transitive,
                        interpreters=interpreters,
                        platforms=list(platforms),
                        manylinux=resolve_configuration.assume_manylinux,
                        ignore_errors=options.ignore_errors,
                    )
            else:
                with TRACER.timed("Resolving requirements."):
                    result = resolve(
                        requirements=reqs,
                        requirement_files=options.requirement_files,
                        constraint_files=options.constraint_files,
                        allow_prereleases=resolve_configuration.allow_prereleases,
                        transitive=resolve_configuration.transitive,
                        interpreters=interpreters,
                        platforms=list(platforms),
                        indexes=resolve_configuration.repository.indexes,
                        find_links=resolve_configuration.repository.find_links,
                        resolver_version=resolve_configuration.repository.resolver_version,
                        network_configuration=resolve_configuration.network_configuration,
                        cache=cache,
                        build=resolve_configuration.allow_builds,
                        use_wheel=resolve_configuration.allow_wheels,
                        compile=options.compile,
                        manylinux=resolve_configuration.assume_manylinux,
                        max_parallel_jobs=resolve_configuration.max_jobs,
                        ignore_errors=options.ignore_errors,
                    )

            for installed_dist in result.installed_distributions:
                pex_builder.add_distribution(installed_dist.distribution)
                if installed_dist.direct_requirement:
                    pex_builder.add_requirement(installed_dist.direct_requirement)
        except Unsatisfiable as e:
            die(str(e))

    if options.entry_point and options.script:
        die("Must specify at most one entry point or script.", INVALID_OPTIONS)

    if options.entry_point:
        pex_builder.set_entry_point(options.entry_point)
    elif options.script:
        pex_builder.set_script(options.script)

    if options.python_shebang:
        pex_builder.set_shebang(options.python_shebang)

    return pex_builder


def transform_legacy_arg(arg):
    # type: (str) -> str
    # inherit-path used to be a boolean arg (so either was absent, or --inherit-path)
    # Now it takes a string argument, so --inherit-path is invalid.
    # Fix up the args we're about to parse to preserve backwards compatibility.
    if arg == "--inherit-path":
        return "--inherit-path={}".format(InheritPath.PREFER.value)
    return arg


def _compatible_with_current_platform(interpreter, platforms):
    if not platforms:
        return True
    current_platforms = set(interpreter.supported_platforms)
    current_platforms.add(None)
    return current_platforms.intersection(platforms)


def main(args=None):
    args = args[:] if args else sys.argv[1:]
    args = [transform_legacy_arg(arg) for arg in args]
    parser = configure_clp()

    try:
        separator = args.index("--")
        args, cmdline = args[:separator], args[separator + 1 :]
    except ValueError:
        args, cmdline = args, []

    options = parser.parse_args(args=args)

    # Ensure the TMPDIR is an absolute path (So subprocesses that change CWD can find it) and
    # that it exists.
    tmpdir = os.path.realpath(options.tmpdir)
    if not os.path.exists(tmpdir):
        die("The specified --tmpdir does not exist: {}".format(tmpdir))
    if not os.path.isdir(tmpdir):
        die("The specified --tmpdir is not a directory: {}".format(tmpdir))
    tempfile.tempdir = os.environ["TMPDIR"] = tmpdir

    if options.cache_dir:
        pex_warnings.warn("The --cache-dir option is deprecated, use --pex-root instead.")
        if options.pex_root and options.cache_dir != options.pex_root:
            die(
                "Both --cache-dir and --pex-root were passed with conflicting values. "
                "Just set --pex-root."
            )

    if options.disable_cache:

        def warn_ignore_pex_root(set_via):
            pex_warnings.warn(
                "The pex root has been set via {via} but --disable-cache is also set. "
                "Ignoring {via} and disabling caches.".format(via=set_via)
            )

        if options.cache_dir:
            warn_ignore_pex_root("--cache-dir")
        elif options.pex_root:
            warn_ignore_pex_root("--pex-root")
        elif os.environ.get("PEX_ROOT"):
            warn_ignore_pex_root("PEX_ROOT")

        pex_root = safe_mkdtemp()
    else:
        pex_root = options.cache_dir or options.pex_root or ENV.PEX_ROOT

    if options.python and options.interpreter_constraint:
        die('The "--python" and "--interpreter-constraint" options cannot be used together.')

    try:
        resolve_configuration = resolve_options.create_resolve_configuration(options)
    except resolve_options.InvalidConfigurationError as e:
        die(str(e))

    with ENV.patch(
        PEX_VERBOSE=str(options.verbosity), PEX_ROOT=pex_root, TMPDIR=tmpdir
    ) as patched_env:
        with TRACER.timed("Building pex"):
            pex_builder = build_pex(
                options.requirements, resolve_configuration, options, cache=ENV.PEX_ROOT
            )

        pex_builder.freeze(bytecode_compile=options.compile)
        interpreter = pex_builder.interpreter
        pex = PEX(
            pex_builder.path(), interpreter=interpreter, verify_entry_point=options.validate_ep
        )

        if options.pex_name is not None:
            log("Saving PEX file to %s" % options.pex_name, V=options.verbosity)
            pex_builder.build(
                options.pex_name,
                bytecode_compile=options.compile,
                deterministic_timestamp=not options.use_system_time,
                layout=options.layout,
            )
            if options.seed != Seed.NONE:
                seed_info = seed_cache(options, pex, verbose=options.seed == Seed.VERBOSE)
                print(seed_info)
        else:
            if not _compatible_with_current_platform(interpreter, options.platforms):
                log("WARNING: attempting to run PEX with incompatible platforms!", V=1)
                log(
                    "Running on platform {} but built for {}".format(
                        interpreter.platform, ", ".join(map(str, options.platforms))
                    ),
                    V=1,
                )

            log(
                "Running PEX file at %s with args %s" % (pex_builder.path(), cmdline),
                V=options.verbosity,
            )
            sys.exit(pex.run(args=list(cmdline), env=patched_env))


def seed_cache(
    options,  # type: Namespace
    pex,  # type: PEX
    verbose=False,  # type : bool
):
    # type: (...) -> str

    pex_path = cast(str, options.pex_name)
    with TRACER.timed("Seeding local caches for {}".format(pex_path)):
        pex_info = pex.pex_info()
        pex_root = pex_info.pex_root

        def create_verbose_info(final_pex_path):
            # type: (str) -> Dict[str, str]
            return dict(pex_root=pex_root, python=pex.interpreter.binary, pex=final_pex_path)

        if options.venv:
            with TRACER.timed("Creating venv from {}".format(pex_path)):
                venv_pex = ensure_venv(pex)
                if verbose:
                    return json.dumps(create_verbose_info(final_pex_path=venv_pex))
                else:
                    return venv_pex

        pex_hash = pex_info.pex_hash
        if pex_hash is None:
            raise AssertionError(
                "There was no pex_hash stored in {} for {}.".format(PexInfo.PATH, pex_path)
            )

        with TRACER.timed("Seeding caches for {}".format(pex_path)):
            final_pex_path = os.path.join(
                maybe_install(pex=pex_path, pex_root=pex_root, pex_hash=pex_hash)
                or os.path.abspath(pex_path),
                "__main__.py",
            )
            if verbose:
                return json.dumps(create_verbose_info(final_pex_path=final_pex_path))
            else:
                return final_pex_path


if __name__ == "__main__":
    main()
