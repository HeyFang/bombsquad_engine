# Released under the MIT License. See LICENSE for details.
#
"""General project related functionality."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from dataclasses import dataclass

from efrotools.project import getprojectconfig, getlocalconfig
from efro.error import CleanError
from efro.terminal import Clr

if TYPE_CHECKING:
    from batools.featureset import FeatureSet


def project_centric_path(projroot: str, path: str) -> str:
    """Convert a CWD-relative path to a project-relative one."""
    abspath = os.path.abspath(path)
    if abspath == projroot:
        return '.'
    projprefix = f'{projroot}/'
    if not abspath.startswith(projprefix):
        raise RuntimeError(
            f'Path "{abspath}" is not under project root "{projprefix}"'
        )
    return abspath[len(projprefix) :]


@dataclass
class _LineChange:
    """A change applying to a particular line in a file."""

    line_number: int
    expected: str
    can_auto_update: bool


class ProjectUpdater:
    """Context for an project-updater run."""

    def __init__(
        self,
        projroot: str,
        *,
        check: bool,
        fix: bool,
        empty: bool = False,
        projname: str = 'BallisticaKit',
    ) -> None:
        self.projname = projname
        self.projroot = os.path.abspath(projroot)
        self.check = check
        self.fix = fix

        # 'fix' implies making changes and 'check' implies no changes.
        if fix and check:
            raise RuntimeError('fix and check cannot both be enabled')

        # We behave a bit differently in the public repo.
        self.public: bool = getprojectconfig(Path(projroot)).get(
            'public', False
        )
        assert isinstance(self.public, bool)

        self._source_files: list[str] | None = None
        self._header_files: list[str] | None = None

        # Set of files this updater instance will update.
        # Add stuff here as desired before calling run().
        # The associated value can be input data for the file.
        # If None, the existing file will be read from disk.
        self._enqueued_updates: dict[str, str | None] = {}

        # Individual line corrections made in _fix mode.
        self._line_corrections: dict[str, list[_LineChange]] = {}

        self._can_generate_files = False

        # All files generated by going through updates. Note that
        # this can include files not explicitly requested in
        # updates (manifest files or other side-effect files).
        self._generated_files: dict[str, str] = {}

        # Cached feature-set list for any functionality/tools that might
        # need it.
        self._feature_sets: dict[str, FeatureSet] | None = None

        self.license_line_checks = bool(
            getlocalconfig(Path(projroot)).get('license_line_checks', True)
        )

        self._internal_source_dirs: set[str] | None = None
        self._internal_source_files: set[str] | None = None

        # Whether to run various checks across project files. This can
        # be turned off to speed things up when updating a focused set
        # of files.
        self.run_file_checks = True

        # For 'empty' mode we disable all default stuff and only do
        # exactly what is requested of us.
        if empty:
            self.run_file_checks = False
        else:
            # Schedule updates for all the things in normal mode.
            self._update_meta_makefile()
            self._update_resources_makefile()
            self._update_assets_makefile()
            self._update_top_level_makefile()
            self._update_cmake_files()
            self._update_visual_studio_projects()
            self._update_xcode_projects()
            self._update_app_module()

    @property
    def source_files(self) -> list[str]:
        """Return project source files."""
        assert self._source_files is not None
        return self._source_files

    @property
    def header_files(self) -> list[str]:
        """Return project header files."""
        assert self._header_files is not None
        return self._header_files

    @property
    def feature_sets(self) -> dict[str, FeatureSet]:
        """Cached list of project feature-sets."""
        if self._feature_sets is None:
            from batools.featureset import FeatureSet

            self._feature_sets = {
                f.name: f for f in FeatureSet.get_all_for_project(self.projroot)
            }
        return self._feature_sets

    def enqueue_update(self, path: str, data: str | None = None) -> None:
        """Add an update to the queue."""
        self._enqueued_updates[path] = data

    def run(self) -> None:
        """Do the thing."""
        self.prepare_to_generate()
        start_updates = self._enqueued_updates.copy()

        # Generate all files we've been asked to.
        for path in self._enqueued_updates:
            self.generate_file(path)

        # Run some lovely checks.
        if self.run_file_checks:
            from batools.project import _checks

            _checks.check_makefiles(self)
            _checks.check_python_files(self)
            _checks.check_sync_states(self)
            _checks.check_misc(self)
            _checks.check_source_files(self)
            _checks.check_headers(self)

        # Make sure nobody is changing this while processing.
        self._can_generate_files = False
        assert start_updates == self._enqueued_updates

        # If we're all good to here, do the actual writes we set up above.
        self._apply_line_changes()
        self._apply_file_changes()

    def prepare_to_generate(self) -> None:
        """Prepare"""
        # Make sure we're operating from a project root.
        if not os.path.isdir(
            os.path.join(self.projroot, 'config')
        ) or not os.path.isdir(os.path.join(self.projroot, 'tools')):
            raise RuntimeError(
                f"ProjectUpdater projroot '{self.projroot}' is not valid."
            )

        self._find_sources_and_headers(
            os.path.join(self.projroot, 'src/ballistica')
        )

        self._can_generate_files = True

    def _get_internal_source_files(self) -> set[str]:
        # Fetch/calc just once and cache results.
        if self._internal_source_files is None:
            sources: list[str]
            if self.public:
                sources = []
            else:
                sources = getprojectconfig(Path(self.projroot)).get(
                    'internal_source_files', []
                )
            if not isinstance(sources, list):
                raise CleanError(
                    f'Expected list for internal_source_files;'
                    f' got {type(sources)}'
                )
            self._internal_source_files = set(sources)
        return self._internal_source_files

    def _get_internal_source_dirs(self) -> set[str]:
        # Fetch/calc just once and cache results.
        if self._internal_source_dirs is None:
            sources: list[str]
            if self.public:
                sources = []
            else:
                sources = getprojectconfig(Path(self.projroot)).get(
                    'internal_source_dirs', []
                )
            if not isinstance(sources, list):
                raise CleanError(
                    f'Expected list for internal_source_dirs;'
                    f' got {type(sources)}'
                )
            self._internal_source_dirs = set(sources)
        return self._internal_source_dirs

    def _apply_file_changes(self) -> None:
        # Now write out any project files that have changed
        # (or error if we're in check mode).
        unchanged_file_count = 0
        for fname, fcode in self._generated_files.items():
            f_path_abs = os.path.join(self.projroot, fname)
            # Allow for line ending changes by git?...
            fcodefin = fcode.replace('\r\n', '\n')
            f_orig: str | None
            if os.path.exists(f_path_abs):
                with open(f_path_abs, 'r', encoding='utf-8') as infile:
                    f_orig = infile.read()
            else:
                f_orig = None
            if f_orig == fcodefin:
                unchanged_file_count += 1
            else:
                if self.check:
                    # Dump the generated and print a command to diff it
                    # against the original. This can be useful to
                    # diagnose non-deterministic generation issues.
                    errfile = os.path.join(
                        self.projroot, 'build/project_check_error_file'
                    )
                    os.makedirs(os.path.dirname(errfile), exist_ok=True)
                    with open(errfile, 'w', encoding='utf-8') as outfile:
                        outfile.write(fcodefin)
                    path1 = f_path_abs
                    path2 = errfile
                    raise CleanError(
                        f"Found out-of-date project file: '{fname}'.\n"
                        'To see what would change, run:\n'
                        f"  diff '{path1}' '{path2}'\n"
                    )

                print(f'{Clr.BLU}Writing project file: {fname}{Clr.RST}')
                with open(f_path_abs, 'w', encoding='utf-8') as outfile:
                    outfile.write(fcode)
        if unchanged_file_count > 0:
            print(f'{unchanged_file_count} project files are up to date.')

    def _apply_line_changes(self) -> None:
        # Build a flat list of entries that can and can-not be auto
        # applied.
        manual_changes: list[tuple[str, _LineChange]] = []
        auto_changes: list[tuple[str, _LineChange]] = []
        for fname, entries in self._line_corrections.items():
            for entry in entries:
                if entry.can_auto_update:
                    auto_changes.append((fname, entry))
                else:
                    manual_changes.append((fname, entry))

        # If there are any manual-only entries, list then and bail.
        # (Don't wanna allow auto-apply unless it fixes everything)
        if manual_changes:
            print(
                f'{Clr.RED}Found erroneous lines '
                f'requiring manual correction:{Clr.RST}'
            )
            for change in manual_changes:
                print(
                    f'{Clr.RED}{change[0]}:{change[1].line_number + 1}:'
                    f' Expected line to be:\n  {change[1].expected}{Clr.RST}'
                )

            raise CleanError()

        # Now, if we've got auto entries, either list or auto-correct them.
        if auto_changes:
            if not self.fix:
                for i, change in enumerate(auto_changes):
                    print(
                        f'{Clr.RED}#{i}:'
                        f' {change[0]}:{change[1].line_number+1}:'
                        f'{Clr.RST}'
                    )
                    print(
                        f'{Clr.RED}  Expected "{change[1].expected}"{Clr.RST}'
                    )
                    with open(
                        os.path.join(self.projroot, change[0]), encoding='utf-8'
                    ) as infile:
                        lines = infile.read().splitlines()
                    line = lines[change[1].line_number]
                    print(f'{Clr.RED}  Found "{line}"{Clr.RST}')
                raise CleanError(
                    f'All {len(auto_changes)} errors are'
                    f' auto-fixable; run tools/pcommand update_project'
                    f' --fix to apply corrections.'
                )

            for i, change in enumerate(auto_changes):
                print(
                    f'{Clr.BLU}{Clr.BLD}Correcting'
                    f' {change[0]} line {change[1].line_number+1}{Clr.RST}'
                )
                with open(
                    os.path.join(self.projroot, change[0]), encoding='utf-8'
                ) as infile:
                    lines = infile.read().splitlines()
                lines[change[1].line_number] = change[1].expected
                with open(
                    os.path.join(self.projroot, change[0]),
                    'w',
                    encoding='utf-8',
                ) as outfile:
                    outfile.write('\n'.join(lines) + '\n')

        # If there were no issues whatsoever, note that.
        if not manual_changes and not auto_changes:
            fcount = len(self.header_files) + len(self.source_files)
            print(f'No issues found in {fcount} source files.')

    def add_line_correction(
        self,
        filename: str,
        line_number: int,
        expected: str,
        can_auto_update: bool,
    ) -> None:
        """Add a correction that the updater can optionally perform."""
        # No longer allowing negatives here since they don't show up
        # nicely in correction list.
        assert line_number >= 0
        self._line_corrections.setdefault(filename, []).append(
            _LineChange(
                line_number=line_number,
                expected=expected,
                can_auto_update=can_auto_update,
            )
        )

    def generate_file(self, path: str) -> str:
        """Generate/return the contents for the file at the given path."""
        # pylint: disable=too-many-branches

        if not self._can_generate_files:
            raise RuntimeError('Generate cannot be called right now.')

        if path not in self._generated_files:
            # First we need input data. If the user provided it explicitly,
            # go with theirs. Otherwise load the existing file from disk.
            existing_data = self._enqueued_updates.get(path)
            if existing_data is None:
                with open(
                    os.path.join(self.projroot, path), encoding='utf-8'
                ) as infile:
                    existing_data = infile.read()
            # Dispatch to generator methods depending on extension/etc.
            if path.endswith('/project.pbxproj'):
                self._generate_xcode_project(path, existing_data)
            elif path.endswith('.vcxproj.filters'):
                self._generate_visual_studio_project_filters(
                    path, existing_data
                )
            elif path.endswith('.vcxproj'):
                self._generate_visual_studio_project(path, existing_data)
            elif path.endswith('CMakeLists.txt'):
                self._generate_cmake_file(path, existing_data)
            elif path == 'Makefile':
                self._generate_top_level_makefile(path, existing_data)
            elif path == 'src/assets/Makefile':
                self._generate_assets_makefile(path, existing_data)
            elif path.startswith('src/assets/.asset_manifest_public'):
                # These are always generated as a side-effect of the
                # assets Makefile.
                self.generate_file('src/assets/Makefile')
            elif path.startswith('src/assets/.asset_manifest_private'):
                if self.public:
                    # In public repos these are just pulled through as-is
                    # from the source project.
                    self._generate_passthrough_file(path, existing_data)
                else:
                    # In private repos, these are generated as a side-effect
                    # of the assets Makefile.
                    self.generate_file('src/assets/Makefile')

            elif path == 'src/resources/Makefile':
                self._generate_resources_makefile(path, existing_data)
            elif path == 'src/meta/Makefile':
                self._generate_meta_makefile(existing_data)
            elif path == 'src/assets/ba_data/python/babase/_app.py':
                self._generate_app_module(path, existing_data)
            elif path.startswith('src/meta/.meta_manifest_'):
                # These are always generated as a side-effect of the
                # meta Makefile.
                self.generate_file('src/meta/Makefile')
                assert path in self._generated_files
            else:
                raise RuntimeError(
                    f"No known formula to create project file: '{path}'."
                )

        assert path in self._generated_files
        return self._generated_files[path]

    def _update_app_module(self) -> None:
        self.enqueue_update('src/assets/ba_data/python/babase/_app.py')

    def _update_xcode_projects(self) -> None:
        # from batools.xcode import update_xcode_project

        for projpath in [
            'ballisticakit-xcode/BallisticaKit.xcodeproj/project.pbxproj'
        ]:
            # These currently aren't bundled in public.
            if self.public:
                assert not os.path.exists(projpath)
                continue

            self.enqueue_update(projpath)

    def _generate_xcode_project(self, path: str, existing_data: str) -> None:
        from batools.xcodeproject import update_xcode_project

        all_files = sorted(
            [f'ballistica{p}' for p in (self.source_files + self.header_files)]
        )

        # We have .pbxproj; this wants .xcodeproj above it.
        # Should probably change that as its confusing...
        assert path.endswith('.pbxproj')
        self._generated_files[path] = update_xcode_project(
            self.projroot,
            os.path.dirname(path),
            existing_data,
            all_files,
            projname=self.projname,
        )

    def _update_visual_studio_project(self, basename: str) -> None:
        fname = (
            f'ballisticakit-windows/{basename}/'
            f'BallisticaKit{basename}.vcxproj'
        )
        self.enqueue_update(f'{fname}.filters')
        self.enqueue_update(fname)

    def _generate_visual_studio_project(
        self, fname: str, existing_data: str
    ) -> None:
        lines = existing_data.splitlines()

        src_root = '..\\..\\src'

        public_project = 'Plus' not in os.path.basename(fname)

        all_files = sorted(
            [
                f
                for f in (self.source_files + self.header_files)
                if not f.endswith('.m')
                and not f.endswith('.mm')
                and not f.endswith('.c')
                and not f.endswith('.swift')
                and self._is_public_source_file(f) == public_project
            ]
        )

        # Find the ItemGroup containing stdafx.cpp. This is where we'll
        # dump our stuff.
        index = lines.index('    <ClCompile Include="stdafx.cpp">')
        begin_index = end_index = index
        while lines[begin_index] != '  <ItemGroup>':
            begin_index -= 1
        while lines[end_index] != '  </ItemGroup>':
            end_index += 1
        group_lines = lines[begin_index + 1 : end_index]

        # Strip out any existing files from src/ballistica.
        group_lines = [
            l for l in group_lines if src_root + '\\ballistica\\' not in l
        ]

        # Now add in our own.
        # Note: we can't use C files in this build at the moment; breaks
        # precompiled header stuff. (shouldn't be a problem though).
        group_lines = [
            '    <'
            + ('ClInclude' if src.endswith('.h') else 'ClCompile')
            + ' Include="'
            + src_root
            + '\\ballistica'
            + src.replace('/', '\\')
            + '" />'
            for src in all_files
        ] + group_lines

        filtered = lines[: begin_index + 1] + group_lines + lines[end_index:]
        out = '\r\n'.join(filtered) + '\r\n'
        self._generated_files[fname] = out

    def _generate_visual_studio_project_filters(
        self, fname: str, existing_data: str
    ) -> None:
        del existing_data  # Unused.
        assert fname.endswith('.filters')
        # We rely on the generated project file itself.
        project = self.generate_file(fname.removesuffix('.filters'))
        lines_in = project.splitlines()
        src_root = '..\\..\\src'
        filterpaths: set[str] = set()
        filterlines: list[str] = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<Project ToolsVersion="4.0"'
            ' xmlns="http://schemas.microsoft.com/developer/msbuild/2003">',
            '  <ItemGroup>',
        ]
        sourcelines = [l for l in lines_in if 'Include="' + src_root in l]
        for line in sourcelines:
            entrytype = line.strip().split()[0][1:]
            path = line.split('"')[1]
            filterlines.append('    <' + entrytype + ' Include="' + path + '">')

            # If we have a dir foo/bar/eep we need to create filters for
            # each of foo, foo/bar, and foo/bar/eep
            splits = path[len(src_root) :].split('\\')
            splits = [s for s in splits if s != '']
            splits = splits[:-1]
            for i in range(len(splits)):
                filterpaths.add('\\'.join(splits[: (i + 1)]))
            filterlines.append(
                '      <Filter>' + '\\'.join(splits) + '</Filter>'
            )
            filterlines.append('    </' + entrytype + '>')
        filterlines += [
            '  </ItemGroup>',
            '  <ItemGroup>',
        ]
        for filterpath in sorted(filterpaths):
            filterlines.append('    <Filter Include="' + filterpath + '" />')
        filterlines += [
            '  </ItemGroup>',
            '</Project>',
        ]
        self._generated_files[fname] = '\r\n'.join(filterlines) + '\r\n'

    def _update_visual_studio_projects(self) -> None:
        self._update_visual_studio_project('Generic')
        self._update_visual_studio_project('Headless')
        if not self.public:
            self._update_visual_studio_project('GenericPlus')
            self._update_visual_studio_project('HeadlessPlus')
            self._update_visual_studio_project('Oculus')
            self._update_visual_studio_project('OculusPlus')

    def _is_public_source_file(self, filename: str) -> bool:
        assert filename.startswith('/')
        filename = f'src/ballistica{filename}'

        # If its under any of our internal source dirs, make it internal.
        for srcdir in self._get_internal_source_dirs():
            assert not srcdir.startswith('/')
            assert not srcdir.endswith('/')
            if filename.startswith(f'{srcdir}/'):
                return False

        # If its specifically listed as an internal file, make it internal.
        return filename not in self._get_internal_source_files()

    def _generate_cmake_file(self, fname: str, existing_data: str) -> None:
        lines = existing_data.splitlines()

        for section in ['PUBLIC', 'PRIVATE']:
            # Public repo has no private section.
            if self.public and section == 'PRIVATE':
                continue

            auto_start = lines.index(
                f'  # AUTOGENERATED_{section}_BEGIN (this section'
                f' is managed by the "update_project" tool)'
            )
            auto_end = lines.index(f'  # AUTOGENERATED_{section}_END')
            our_lines = [
                '  ${BA_SRC_ROOT}/ballistica' + f
                for f in sorted(self.source_files + self.header_files)
                if not f.endswith('.mm')
                and not f.endswith('.m')
                and not f.endswith('.swift')
                and self._is_public_source_file(f) == (section == 'PUBLIC')
            ]
            lines = lines[: auto_start + 1] + our_lines + lines[auto_end:]

        self._generated_files[fname] = '\n'.join(lines) + '\n'

    def _update_cmake_files(self) -> None:
        # Our regular cmake build.
        self.enqueue_update('ballisticakit-cmake/CMakeLists.txt')

        # Our Android cmake build (Currently not included in public).
        fname = (
            'ballisticakit-android/BallisticaKit/src/main/cpp/CMakeLists.txt'
        )
        if not self.public:
            self.enqueue_update(fname)
        else:
            # So we don't forget to turn this on once added.
            assert not os.path.exists(fname)

    def _find_sources_and_headers(self, scan_dir: str) -> None:
        src_files = set()
        header_files = set()
        exts = ['.c', '.cc', '.cpp', '.cxx', '.m', '.mm', '.swift']
        header_exts = ['.h']

        # Gather all sources and headers.
        # HMMM: Ideally we should use
        # efrotools.code.get_code_filenames() here (though we return
        # things relative to the scan-dir which could throw things off).
        for root, _dirs, files in os.walk(scan_dir):
            for ftst in files:
                if any(ftst.endswith(ext) for ext in exts):
                    src_files.add(os.path.join(root, ftst)[len(scan_dir) :])
                if any(ftst.endswith(ext) for ext in header_exts):
                    header_files.add(os.path.join(root, ftst)[len(scan_dir) :])

        # IMPORTANT - exclude generated files.
        # For now these just consist of headers so its ok to completely
        # ignore their existence here, but at some point if we start
        # generating .cc files that need to be compiled we'll have to
        # ask the meta system which files it *will* be generating and
        # add THAT list (not what we see on disk) to projects.
        self._source_files = sorted(s for s in src_files if '/mgen/' not in s)
        self._header_files = sorted(
            h for h in header_files if '/mgen/' not in h
        )

    def _update_assets_makefile(self) -> None:
        self.enqueue_update('src/assets/Makefile')

    def _generate_assets_makefile(self, path: str, existing_data: str) -> None:
        from batools.assetsmakefile import generate_assets_makefile

        # We need to know what files meta will be creating (since they
        # can be asset sources).
        meta_manifests: dict[str, str] = {}
        for mantype in ['public', 'private']:
            manifest_file_name = f'src/meta/.meta_manifest_{mantype}.json'
            meta_manifests[manifest_file_name] = self.generate_file(
                manifest_file_name
            )

        # Special case; the app module file in the base feature set
        # is created/updated here as a project file. It may or may not
        # exist on disk, but we want to ignore it if it does and add it
        # explicitly similarly to meta-manifests.
        if 'base' in self.feature_sets:
            explicit_sources = {'src/assets/ba_data/python/babase/_app.py'}
        else:
            explicit_sources = set()

        outfiles = generate_assets_makefile(
            self.projroot, path, existing_data, meta_manifests, explicit_sources
        )

        for out_path, out_contents in outfiles.items():
            self._generated_files[out_path] = out_contents

    def _update_top_level_makefile(self) -> None:
        self.enqueue_update('Makefile')

    def _generate_top_level_makefile(
        self, path: str, existing_data: str
    ) -> None:
        from batools.toplevelmakefile import generate_top_level_makefile

        self._generated_files[path] = generate_top_level_makefile(
            self.projroot, existing_data
        )

    def _generate_app_module(self, path: str, existing_data: str) -> None:
        from batools.appmodule import generate_app_module

        self._generated_files[path] = generate_app_module(
            self.projroot, self.feature_sets, existing_data
        )

    def _update_meta_makefile(self) -> None:
        self.enqueue_update('src/meta/Makefile')

    def _generate_passthrough_file(self, path: str, existing_data: str) -> None:
        self._generated_files[path] = existing_data

    def _generate_meta_makefile(self, existing_data: str) -> None:
        from batools.metamakefile import generate_meta_makefile

        outfiles = generate_meta_makefile(self.projroot, existing_data)
        for out_path, out_contents in outfiles.items():
            self._generated_files[out_path] = out_contents

    def _update_resources_makefile(self) -> None:
        self.enqueue_update('src/resources/Makefile')

    def _generate_resources_makefile(
        self, path: str, existing_data: str
    ) -> None:
        from batools.resourcesmakefile import ResourcesMakefileGenerator

        self._generated_files[path] = ResourcesMakefileGenerator(
            self.projroot,
            existing_data,
            projname=self.projname,
        ).run()
