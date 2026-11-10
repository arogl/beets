"""
List all files in the library folder which are not listed in the
 beets library database, including art files
"""

import os

from beets import util
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand, print_

__author__ = "https://github.com/MrNuggelz"


class Unimported(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add({"ignore_extensions": [], "ignore_subdirectories": []})

    def commands(self):
        def print_unimported(lib, opts, args):
            ignore_exts = [
                f".{x}".encode() for x in self.config["ignore_extensions"].as_str_seq()
            ]
            ignore_dirs = [
                os.path.join(lib.directory, x.encode())
                for x in self.config["ignore_subdirectories"].as_str_seq()
            ]

            # Case-insensitive path normalization (NTFS/Linux safe)
            def path_norm(p):
                """Normalize path for case-insensitive comparison"""
                if isinstance(p, bytes):
                    p = p.decode("utf-8")
                return os.path.normcase(os.path.normpath(p))

            # Collect normalized library paths BEFORE walking FS
            lib_paths_norm = {path_norm(x.path) for x in lib.items()}
            art_paths_norm = {
                path_norm(x)
                for x in {x.artpath for x in lib.albums()}
                if x  # Skip None
            }

            in_folder = set()
            for root, _, files in os.walk(lib.directory):
                # do not traverse if root is a child of an ignored directory
                if any(root.startswith(ignored) for ignored in ignore_dirs):
                    continue
                for file in files:
                    # ignore files with ignored extensions
                    if any(file.lower().endswith(ext.lower()) for ext in ignore_exts):
                        continue
                    fs_path = os.path.join(root, file)
                    fs_norm = path_norm(fs_path)

                    # Case-insensitive exclusion (fixes Windows NTFS false positives)
                    if fs_norm in lib_paths_norm or fs_norm in art_paths_norm:
                        continue
                    in_folder.add(fs_path)

            for f in sorted(in_folder):
                print_(util.displayable_path(f))

        unimported = Subcommand(
            "unimported",
            help="list all files in the library folder which are not listed"
            " in the beets library database",
        )
        unimported.func = print_unimported
        return [unimported]
