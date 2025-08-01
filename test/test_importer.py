# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.


"""Tests for the general importer functionality."""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import unicodedata
import unittest
from functools import cached_property
from io import StringIO
from pathlib import Path
from tarfile import TarFile
from tempfile import mkstemp
from unittest.mock import Mock, patch
from zipfile import ZipFile

import pytest
from mediafile import MediaFile

from beets import config, importer, logging, util
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.importer.tasks import albums_in_dir
from beets.test import _common
from beets.test.helper import (
    NEEDS_REFLINK,
    AsIsImporterMixin,
    AutotagImportTestCase,
    AutotagStub,
    BeetsTestCase,
    ImportTestCase,
    IOMixin,
    PluginMixin,
    capture_log,
    has_program,
)
from beets.util import bytestring_path, displayable_path, syspath


class PathsMixin:
    import_media: list[MediaFile]

    @cached_property
    def track_import_path(self) -> Path:
        return Path(self.import_media[0].path)

    @cached_property
    def album_path(self) -> Path:
        return self.track_import_path.parent

    @cached_property
    def track_lib_path(self):
        return self.lib_path / "Tag Artist" / "Tag Album" / "Tag Track 1.mp3"


@_common.slow_test()
class NonAutotaggedImportTest(PathsMixin, AsIsImporterMixin, ImportTestCase):
    db_on_disk = True

    def test_album_created_with_track_artist(self):
        self.run_asis_importer()

        albums = self.lib.albums()
        assert len(albums) == 1
        assert albums[0].albumartist == "Tag Artist"

    def test_import_copy_arrives(self):
        self.run_asis_importer()

        assert self.track_lib_path.exists()

    def test_threaded_import_copy_arrives(self):
        config["threaded"] = True

        self.run_asis_importer()
        assert self.track_lib_path.exists()

    def test_import_with_move_deletes_import_files(self):
        assert self.album_path.exists()
        assert self.track_import_path.exists()
        (self.album_path / "alog.log").touch()
        config["clutter"] = ["*.log"]

        self.run_asis_importer(move=True)

        assert not self.track_import_path.exists()
        assert not self.album_path.exists()

    def test_threaded_import_move_arrives(self):
        self.run_asis_importer(move=True, threaded=True)

        assert self.track_lib_path.exists()
        assert not self.track_import_path.exists()

    def test_import_without_delete_retains_files(self):
        self.run_asis_importer(delete=False)

        assert self.track_import_path.exists()

    def test_import_with_delete_removes_files(self):
        self.run_asis_importer(delete=True)

        assert not self.album_path.exists()
        assert not self.track_import_path.exists()

    def test_album_mb_albumartistids(self):
        self.run_asis_importer()
        album = self.lib.albums()[0]
        assert album.mb_albumartistids == album.items()[0].mb_albumartistids

    @unittest.skipUnless(_common.HAVE_SYMLINK, "need symlinks")
    def test_import_link_arrives(self):
        self.run_asis_importer(link=True)

        assert self.track_lib_path.exists()
        assert self.track_lib_path.is_symlink()
        assert self.track_lib_path.resolve() == self.track_import_path

    @unittest.skipUnless(_common.HAVE_HARDLINK, "need hardlinks")
    def test_import_hardlink_arrives(self):
        self.run_asis_importer(hardlink=True)

        assert self.track_lib_path.exists()
        media_stat = self.track_import_path.stat()
        lib_media_stat = self.track_lib_path.stat()
        assert media_stat[stat.ST_INO] == lib_media_stat[stat.ST_INO]
        assert media_stat[stat.ST_DEV] == lib_media_stat[stat.ST_DEV]

    @NEEDS_REFLINK
    def test_import_reflink_arrives(self):
        # Detecting reflinks is currently tricky due to various fs
        # implementations, we'll just check the file exists.
        self.run_asis_importer(reflink=True)

        assert self.track_lib_path.exists()

    def test_import_reflink_auto_arrives(self):
        # Should pass regardless of reflink support due to fallback.
        self.run_asis_importer(reflink="auto")

        assert self.track_lib_path.exists()


def create_archive(session):
    handle, path = mkstemp(dir=session.temp_dir_path)
    path = bytestring_path(path)
    os.close(handle)
    archive = ZipFile(os.fsdecode(path), mode="w")
    archive.write(syspath(os.path.join(_common.RSRC, b"full.mp3")), "full.mp3")
    archive.close()
    path = bytestring_path(path)
    return path


class RmTempTest(BeetsTestCase):
    """Tests that temporarily extracted archives are properly removed
    after usage.
    """

    def setUp(self):
        super().setUp()
        self.want_resume = False
        self.config["incremental"] = False
        self._old_home = None

    def test_rm(self):
        zip_path = create_archive(self)
        archive_task = importer.ArchiveImportTask(zip_path)
        archive_task.extract()
        tmp_path = Path(os.fsdecode(archive_task.toppath))
        assert tmp_path.exists()
        archive_task.finalize(self)
        assert not tmp_path.exists()


class ImportZipTest(AsIsImporterMixin, ImportTestCase):
    def test_import_zip(self):
        zip_path = create_archive(self)
        assert len(self.lib.items()) == 0
        assert len(self.lib.albums()) == 0

        self.run_asis_importer(import_dir=zip_path)
        assert len(self.lib.items()) == 1
        assert len(self.lib.albums()) == 1


class ImportTarTest(ImportZipTest):
    def create_archive(self):
        (handle, path) = mkstemp(dir=syspath(self.temp_dir))
        path = bytestring_path(path)
        os.close(handle)
        archive = TarFile(os.fsdecode(path), mode="w")
        archive.add(
            syspath(os.path.join(_common.RSRC, b"full.mp3")), "full.mp3"
        )
        archive.close()
        return path


@unittest.skipIf(not has_program("unrar"), "unrar program not found")
class ImportRarTest(ImportZipTest):
    def create_archive(self):
        return os.path.join(_common.RSRC, b"archive.rar")


class Import7zTest(ImportZipTest):
    def create_archive(self):
        return os.path.join(_common.RSRC, b"archive.7z")


@unittest.skip("Implement me!")
class ImportPasswordRarTest(ImportZipTest):
    def create_archive(self):
        return os.path.join(_common.RSRC, b"password.rar")


class ImportSingletonTest(AutotagImportTestCase):
    """Test ``APPLY`` and ``ASIS`` choices for an import session with
    singletons config set to True.
    """

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)
        self.importer = self.setup_singleton_importer()

    def test_apply_asis_adds_only_singleton_track(self):
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()

        # album not added
        assert not self.lib.albums()
        assert self.lib.items().get().title == "Tag Track 1"
        assert (self.lib_path / "singletons" / "Tag Track 1.mp3").exists()

    def test_apply_candidate_adds_track(self):
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert not self.lib.albums()
        assert self.lib.items().get().title == "Applied Track 1"
        assert (self.lib_path / "singletons" / "Applied Track 1.mp3").exists()

    def test_skip_does_not_add_track(self):
        self.importer.add_choice(importer.Action.SKIP)
        self.importer.run()

        assert not self.lib.items()

    def test_skip_first_add_second_asis(self):
        self.prepare_album_for_import(2)

        self.importer.add_choice(importer.Action.SKIP)
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()

        assert len(self.lib.items()) == 1

    def test_import_single_files(self):
        resource_path = os.path.join(_common.RSRC, b"empty.mp3")
        single_path = os.path.join(self.import_dir, b"track_2.mp3")

        util.copy(resource_path, single_path)
        import_files = [
            os.path.join(self.import_dir, b"album"),
            single_path,
        ]
        self.setup_importer()
        self.importer.paths = import_files

        self.importer.add_choice(importer.Action.ASIS)
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()

        assert len(self.lib.items()) == 2
        assert len(self.lib.albums()) == 2

    def test_set_fields(self):
        genre = "\U0001f3b7 Jazz"
        collection = "To Listen"
        disc = 0

        config["import"]["set_fields"] = {
            "collection": collection,
            "genre": genre,
            "title": "$title - formatted",
            "disc": disc,
        }

        # As-is item import.
        assert self.lib.albums().get() is None
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()

        for item in self.lib.items():
            item.load()  # TODO: Not sure this is necessary.
            assert item.genre == genre
            assert item.collection == collection
            assert item.title == "Tag Track 1 - formatted"
            assert item.disc == disc
            # Remove item from library to test again with APPLY choice.
            item.remove()

        # Autotagged.
        assert not self.lib.albums()
        self.importer.clear_choices()
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        for item in self.lib.items():
            item.load()
            assert item.genre == genre
            assert item.collection == collection
            assert item.title == "Applied Track 1 - formatted"
            assert item.disc == disc


class ImportTest(PathsMixin, AutotagImportTestCase):
    """Test APPLY, ASIS and SKIP choices."""

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)
        self.setup_importer()

    def test_asis_moves_album_and_track(self):
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()

        assert self.lib.albums().get().album == "Tag Album"
        item = self.lib.items().get()
        assert item.title == "Tag Track 1"
        assert item.filepath.exists()

    def test_apply_moves_album_and_track(self):
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert self.lib.albums().get().album == "Applied Album"
        item = self.lib.items().get()
        assert item.title == "Applied Track 1"
        assert item.filepath.exists()

    def test_apply_from_scratch_removes_other_metadata(self):
        config["import"]["from_scratch"] = True

        for mediafile in self.import_media:
            mediafile.genre = "Tag Genre"
            mediafile.save()

        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert self.lib.items().get().genre == ""

    def test_apply_from_scratch_keeps_format(self):
        config["import"]["from_scratch"] = True

        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert self.lib.items().get().format == "MP3"

    def test_apply_from_scratch_keeps_bitrate(self):
        config["import"]["from_scratch"] = True
        bitrate = 80000

        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert self.lib.items().get().bitrate == bitrate

    def test_apply_with_move_deletes_import(self):
        assert self.track_import_path.exists()

        config["import"]["move"] = True
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert not self.track_import_path.exists()

    def test_apply_with_delete_deletes_import(self):
        assert self.track_import_path.exists()

        config["import"]["delete"] = True
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert not self.track_import_path.exists()

    def test_skip_does_not_add_track(self):
        self.importer.add_choice(importer.Action.SKIP)
        self.importer.run()

        assert not self.lib.items()

    def test_skip_non_album_dirs(self):
        assert (self.import_path / "album").exists()
        self.touch(b"cruft", dir=self.import_dir)
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert len(self.lib.albums()) == 1

    def test_unmatched_tracks_not_added(self):
        self.prepare_album_for_import(2)
        self.matcher.matching = self.matcher.MISSING
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert len(self.lib.items()) == 1

    def test_empty_directory_warning(self):
        import_dir = os.path.join(self.temp_dir, b"empty")
        self.touch(b"non-audio", dir=import_dir)
        self.setup_importer(import_dir=import_dir)
        with capture_log() as logs:
            self.importer.run()

        import_dir = displayable_path(import_dir)
        assert f"No files imported from {import_dir}" in logs

    def test_empty_directory_singleton_warning(self):
        import_dir = os.path.join(self.temp_dir, b"empty")
        self.touch(b"non-audio", dir=import_dir)
        self.setup_singleton_importer(import_dir=import_dir)
        with capture_log() as logs:
            self.importer.run()

        import_dir = displayable_path(import_dir)
        assert f"No files imported from {import_dir}" in logs

    def test_asis_no_data_source(self):
        assert self.lib.items().get() is None

        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()

        with pytest.raises(AttributeError):
            self.lib.items().get().data_source

    def test_set_fields(self):
        genre = "\U0001f3b7 Jazz"
        collection = "To Listen"
        comments = "managed by beets"
        disc = 0

        config["import"]["set_fields"] = {
            "genre": genre,
            "collection": collection,
            "comments": comments,
            "album": "$album - formatted",
            "disc": disc,
        }

        # As-is album import.
        assert self.lib.albums().get() is None
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()

        for album in self.lib.albums():
            album.load()  # TODO: Not sure this is necessary.
            assert album.genre == genre
            assert album.comments == comments
            for item in album.items():
                assert item.get("genre", with_album=False) == genre
                assert item.get("collection", with_album=False) == collection
                assert item.get("comments", with_album=False) == comments
                assert (
                    item.get("album", with_album=False)
                    == "Tag Album - formatted"
                )
                assert item.disc == disc
            # Remove album from library to test again with APPLY choice.
            album.remove()

        # Autotagged.
        assert self.lib.albums().get() is None
        self.importer.clear_choices()
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        for album in self.lib.albums():
            album.load()
            assert album.genre == genre
            assert album.comments == comments
            for item in album.items():
                assert item.get("genre", with_album=False) == genre
                assert item.get("collection", with_album=False) == collection
                assert item.get("comments", with_album=False) == comments
                assert (
                    item.get("album", with_album=False)
                    == "Applied Album - formatted"
                )
                assert item.disc == disc


class ImportTracksTest(AutotagImportTestCase):
    """Test TRACKS and APPLY choice."""

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)
        self.setup_importer()

    def test_apply_tracks_adds_singleton_track(self):
        self.importer.add_choice(importer.Action.TRACKS)
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert self.lib.items().get().title == "Applied Track 1"
        assert not self.lib.albums()

    def test_apply_tracks_adds_singleton_path(self):
        self.importer.add_choice(importer.Action.TRACKS)
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert (self.lib_path / "singletons" / "Applied Track 1.mp3").exists()


class ImportCompilationTest(AutotagImportTestCase):
    """Test ASIS import of a folder containing tracks with different artists."""

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(3)
        self.setup_importer()

    def test_asis_homogenous_sets_albumartist(self):
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()
        assert self.lib.albums().get().albumartist == "Tag Artist"
        for item in self.lib.items():
            assert item.albumartist == "Tag Artist"

    def test_asis_heterogenous_sets_various_albumartist(self):
        self.import_media[0].artist = "Other Artist"
        self.import_media[0].save()
        self.import_media[1].artist = "Another Artist"
        self.import_media[1].save()

        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()
        assert self.lib.albums().get().albumartist == "Various Artists"
        for item in self.lib.items():
            assert item.albumartist == "Various Artists"

    def test_asis_heterogenous_sets_compilation(self):
        self.import_media[0].artist = "Other Artist"
        self.import_media[0].save()
        self.import_media[1].artist = "Another Artist"
        self.import_media[1].save()

        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()
        for item in self.lib.items():
            assert item.comp

    def test_asis_sets_majority_albumartist(self):
        self.import_media[0].artist = "Other Artist"
        self.import_media[0].save()
        self.import_media[1].artist = "Other Artist"
        self.import_media[1].save()

        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()
        assert self.lib.albums().get().albumartist == "Other Artist"
        for item in self.lib.items():
            assert item.albumartist == "Other Artist"

    def test_asis_albumartist_tag_sets_albumartist(self):
        self.import_media[0].artist = "Other Artist"
        self.import_media[1].artist = "Another Artist"
        for mediafile in self.import_media:
            mediafile.albumartist = "Album Artist"
            mediafile.mb_albumartistid = "Album Artist ID"
            mediafile.save()

        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()
        assert self.lib.albums().get().albumartist == "Album Artist"
        assert self.lib.albums().get().mb_albumartistid == "Album Artist ID"
        for item in self.lib.items():
            assert item.albumartist == "Album Artist"
            assert item.mb_albumartistid == "Album Artist ID"

    def test_asis_albumartists_tag_sets_multi_albumartists(self):
        self.import_media[0].artist = "Other Artist"
        self.import_media[0].artists = ["Other Artist", "Other Artist 2"]
        self.import_media[1].artist = "Another Artist"
        self.import_media[1].artists = ["Another Artist", "Another Artist 2"]
        for mediafile in self.import_media:
            mediafile.albumartist = "Album Artist"
            mediafile.albumartists = ["Album Artist 1", "Album Artist 2"]
            mediafile.mb_albumartistid = "Album Artist ID"
            mediafile.save()

        self.importer.add_choice(importer.Action.ASIS)
        self.importer.run()
        assert self.lib.albums().get().albumartist == "Album Artist"
        assert self.lib.albums().get().albumartists == [
            "Album Artist 1",
            "Album Artist 2",
        ]
        assert self.lib.albums().get().mb_albumartistid == "Album Artist ID"

        # Make sure both custom media items get tested
        asserted_multi_artists_0 = False
        asserted_multi_artists_1 = False
        for item in self.lib.items():
            assert item.albumartist == "Album Artist"
            assert item.albumartists == ["Album Artist 1", "Album Artist 2"]
            assert item.mb_albumartistid == "Album Artist ID"

            if item.artist == "Other Artist":
                asserted_multi_artists_0 = True
                assert item.artists == ["Other Artist", "Other Artist 2"]
            if item.artist == "Another Artist":
                asserted_multi_artists_1 = True
                assert item.artists == ["Another Artist", "Another Artist 2"]

        assert asserted_multi_artists_0
        assert asserted_multi_artists_1


class ImportExistingTest(PathsMixin, AutotagImportTestCase):
    """Test importing files that are already in the library directory."""

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)

        self.reimporter = self.setup_importer(import_dir=self.libdir)
        self.importer = self.setup_importer()

    def tearDown(self):
        super().tearDown()
        self.matcher.restore()

    @cached_property
    def applied_track_path(self) -> Path:
        return Path(str(self.track_lib_path).replace("Tag", "Applied"))

    def test_does_not_duplicate_item_nor_album(self):
        self.importer.run()
        assert len(self.lib.items()) == 1
        assert len(self.lib.albums()) == 1

        self.reimporter.add_choice(importer.Action.APPLY)
        self.reimporter.run()

        assert len(self.lib.items()) == 1
        assert len(self.lib.albums()) == 1

    def test_does_not_duplicate_singleton_track(self):
        self.importer.add_choice(importer.Action.TRACKS)
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert len(self.lib.items()) == 1

        self.reimporter.add_choice(importer.Action.TRACKS)
        self.reimporter.add_choice(importer.Action.APPLY)
        self.reimporter.run()
        assert len(self.lib.items()) == 1

    def test_asis_updates_metadata_and_moves_file(self):
        self.importer.run()

        medium = MediaFile(self.lib.items().get().path)
        medium.title = "New Title"
        medium.save()

        self.reimporter.add_choice(importer.Action.ASIS)
        self.reimporter.run()

        assert self.lib.items().get().title == "New Title"
        assert not self.applied_track_path.exists()
        assert self.applied_track_path.with_name("New Title.mp3").exists()

    def test_asis_updated_without_copy_does_not_move_file(self):
        self.importer.run()
        medium = MediaFile(self.lib.items().get().path)
        medium.title = "New Title"
        medium.save()

        config["import"]["copy"] = False
        self.reimporter.add_choice(importer.Action.ASIS)
        self.reimporter.run()

        assert self.applied_track_path.exists()
        assert not self.applied_track_path.with_name("New Title.mp3").exists()

    def test_outside_file_is_copied(self):
        config["import"]["copy"] = False
        self.importer.run()
        assert self.lib.items().get().filepath == self.track_import_path

        self.reimporter = self.setup_importer()
        self.reimporter.add_choice(importer.Action.APPLY)
        self.reimporter.run()

        assert self.applied_track_path.exists()
        assert self.lib.items().get().filepath == self.applied_track_path


class GroupAlbumsImportTest(AutotagImportTestCase):
    matching = AutotagStub.NONE

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(3)
        self.setup_importer()

        # Split tracks into two albums and use both as-is
        self.importer.add_choice(importer.Action.ALBUMS)
        self.importer.add_choice(importer.Action.ASIS)
        self.importer.add_choice(importer.Action.ASIS)

    def test_add_album_for_different_artist_and_different_album(self):
        self.import_media[0].artist = "Artist B"
        self.import_media[0].album = "Album B"
        self.import_media[0].save()

        self.importer.run()
        albums = {album.album for album in self.lib.albums()}
        assert albums == {"Album B", "Tag Album"}

    def test_add_album_for_different_artist_and_same_albumartist(self):
        self.import_media[0].artist = "Artist B"
        self.import_media[0].albumartist = "Album Artist"
        self.import_media[0].save()
        self.import_media[1].artist = "Artist C"
        self.import_media[1].albumartist = "Album Artist"
        self.import_media[1].save()

        self.importer.run()
        artists = {album.albumartist for album in self.lib.albums()}
        assert artists == {"Album Artist", "Tag Artist"}

    def test_add_album_for_same_artist_and_different_album(self):
        self.import_media[0].album = "Album B"
        self.import_media[0].save()

        self.importer.run()
        albums = {album.album for album in self.lib.albums()}
        assert albums == {"Album B", "Tag Album"}

    def test_add_album_for_same_album_and_different_artist(self):
        self.import_media[0].artist = "Artist B"
        self.import_media[0].save()

        self.importer.run()
        artists = {album.albumartist for album in self.lib.albums()}
        assert artists == {"Artist B", "Tag Artist"}

    def test_incremental(self):
        config["import"]["incremental"] = True
        self.import_media[0].album = "Album B"
        self.import_media[0].save()

        self.importer.run()
        albums = {album.album for album in self.lib.albums()}
        assert albums == {"Album B", "Tag Album"}


class GlobalGroupAlbumsImportTest(GroupAlbumsImportTest):
    def setUp(self):
        super().setUp()
        self.importer.clear_choices()
        self.importer.default_choice = importer.Action.ASIS
        config["import"]["group_albums"] = True


class ChooseCandidateTest(AutotagImportTestCase):
    matching = AutotagStub.BAD

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)
        self.setup_importer()

    def test_choose_first_candidate(self):
        self.importer.add_choice(1)
        self.importer.run()
        assert self.lib.albums().get().album == "Applied Album M"

    def test_choose_second_candidate(self):
        self.importer.add_choice(2)
        self.importer.run()
        assert self.lib.albums().get().album == "Applied Album MM"


class InferAlbumDataTest(unittest.TestCase):
    def setUp(self):
        super().setUp()

        i1 = _common.item()
        i2 = _common.item()
        i3 = _common.item()
        i1.title = "first item"
        i2.title = "second item"
        i3.title = "third item"
        i1.comp = i2.comp = i3.comp = False
        i1.albumartist = i2.albumartist = i3.albumartist = ""
        i1.mb_albumartistid = i2.mb_albumartistid = i3.mb_albumartistid = ""
        self.items = [i1, i2, i3]

        self.task = importer.ImportTask(
            paths=["a path"], toppath="top path", items=self.items
        )

    def test_asis_homogenous_single_artist(self):
        self.task.set_choice(importer.Action.ASIS)
        self.task.align_album_level_fields()
        assert not self.items[0].comp
        assert self.items[0].albumartist == self.items[2].artist

    def test_asis_heterogenous_va(self):
        self.items[0].artist = "another artist"
        self.items[1].artist = "some other artist"
        self.task.set_choice(importer.Action.ASIS)

        self.task.align_album_level_fields()

        assert self.items[0].comp
        assert self.items[0].albumartist == "Various Artists"

    def test_asis_comp_applied_to_all_items(self):
        self.items[0].artist = "another artist"
        self.items[1].artist = "some other artist"
        self.task.set_choice(importer.Action.ASIS)

        self.task.align_album_level_fields()

        for item in self.items:
            assert item.comp
            assert item.albumartist == "Various Artists"

    def test_asis_majority_artist_single_artist(self):
        self.items[0].artist = "another artist"
        self.task.set_choice(importer.Action.ASIS)

        self.task.align_album_level_fields()

        assert not self.items[0].comp
        assert self.items[0].albumartist == self.items[2].artist

    def test_asis_track_albumartist_override(self):
        self.items[0].artist = "another artist"
        self.items[1].artist = "some other artist"
        for item in self.items:
            item.albumartist = "some album artist"
            item.mb_albumartistid = "some album artist id"
        self.task.set_choice(importer.Action.ASIS)

        self.task.align_album_level_fields()

        assert self.items[0].albumartist == "some album artist"
        assert self.items[0].mb_albumartistid == "some album artist id"

    def test_apply_gets_artist_and_id(self):
        self.task.set_choice(AlbumMatch(0, None, {}, set(), set()))  # APPLY

        self.task.align_album_level_fields()

        assert self.items[0].albumartist == self.items[0].artist
        assert self.items[0].mb_albumartistid == self.items[0].mb_artistid

    def test_apply_lets_album_values_override(self):
        for item in self.items:
            item.albumartist = "some album artist"
            item.mb_albumartistid = "some album artist id"
        self.task.set_choice(AlbumMatch(0, None, {}, set(), set()))  # APPLY

        self.task.align_album_level_fields()

        assert self.items[0].albumartist == "some album artist"
        assert self.items[0].mb_albumartistid == "some album artist id"

    def test_small_single_artist_album(self):
        self.items = [self.items[0]]
        self.task.items = self.items
        self.task.set_choice(importer.Action.ASIS)
        self.task.align_album_level_fields()
        assert not self.items[0].comp


def album_candidates_mock(*args, **kwargs):
    """Create an AlbumInfo object for testing."""
    yield AlbumInfo(
        artist="artist",
        album="album",
        tracks=[TrackInfo(title="new title", track_id="trackid", index=0)],
        album_id="albumid",
        artist_id="artistid",
        flex="flex",
    )


@patch(
    "beets.metadata_plugins.candidates", Mock(side_effect=album_candidates_mock)
)
class ImportDuplicateAlbumTest(PluginMixin, ImportTestCase):
    plugin = "musicbrainz"

    def setUp(self):
        super().setUp()

        # Original album
        self.add_album_fixture(albumartist="artist", album="album")

        # Create import session
        self.prepare_album_for_import(1)
        self.importer = self.setup_importer(
            duplicate_keys={"album": "albumartist album"}
        )

    def test_remove_duplicate_album(self):
        item = self.lib.items().get()
        assert item.title == "t\xeftle 0"
        assert item.filepath.exists()

        self.importer.default_resolution = self.importer.Resolution.REMOVE
        self.importer.run()

        assert not item.filepath.exists()
        assert len(self.lib.albums()) == 1
        assert len(self.lib.items()) == 1
        item = self.lib.items().get()
        assert item.title == "new title"

    def test_no_autotag_keeps_duplicate_album(self):
        config["import"]["autotag"] = False
        item = self.lib.items().get()
        assert item.title == "t\xeftle 0"
        assert item.filepath.exists()

        # Imported item has the same artist and album as the one in the
        # library.
        import_file = os.path.join(
            self.importer.paths[0], b"album", b"track_1.mp3"
        )
        import_file = MediaFile(import_file)
        import_file.artist = item["artist"]
        import_file.albumartist = item["artist"]
        import_file.album = item["album"]
        import_file.title = "new title"

        self.importer.default_resolution = self.importer.Resolution.REMOVE
        self.importer.run()

        assert item.filepath.exists()
        assert len(self.lib.albums()) == 2
        assert len(self.lib.items()) == 2

    def test_keep_duplicate_album(self):
        self.importer.default_resolution = self.importer.Resolution.KEEPBOTH
        self.importer.run()

        assert len(self.lib.albums()) == 2
        assert len(self.lib.items()) == 2

    def test_skip_duplicate_album(self):
        item = self.lib.items().get()
        assert item.title == "t\xeftle 0"

        self.importer.default_resolution = self.importer.Resolution.SKIP
        self.importer.run()

        assert len(self.lib.albums()) == 1
        assert len(self.lib.items()) == 1
        item = self.lib.items().get()
        assert item.title == "t\xeftle 0"

    def test_merge_duplicate_album(self):
        self.importer.default_resolution = self.importer.Resolution.MERGE
        self.importer.run()

        assert len(self.lib.albums()) == 1

    def test_twice_in_import_dir(self):
        self.skipTest("write me")

    def test_keep_when_extra_key_is_different(self):
        config["import"]["duplicate_keys"]["album"] = "albumartist album flex"

        item = self.lib.items().get()
        import_file = MediaFile(
            os.path.join(self.importer.paths[0], b"album", b"track_1.mp3")
        )
        import_file.artist = item["artist"]
        import_file.albumartist = item["artist"]
        import_file.album = item["album"]
        import_file.title = item["title"]
        import_file.flex = "different"

        self.importer.default_resolution = self.importer.Resolution.SKIP
        self.importer.run()

        assert len(self.lib.albums()) == 2
        assert len(self.lib.items()) == 2

    def add_album_fixture(self, **kwargs):
        # TODO move this into upstream
        album = super().add_album_fixture()
        album.update(kwargs)
        album.store()
        return album


def item_candidates_mock(*args, **kwargs):
    yield TrackInfo(
        artist="artist",
        title="title",
        track_id="new trackid",
        index=0,
    )


@patch(
    "beets.metadata_plugins.item_candidates",
    Mock(side_effect=item_candidates_mock),
)
class ImportDuplicateSingletonTest(ImportTestCase):
    def setUp(self):
        super().setUp()

        # Original file in library
        self.add_item_fixture(
            artist="artist", title="title", mb_trackid="old trackid"
        )

        # Import session
        self.prepare_album_for_import(1)
        self.importer = self.setup_singleton_importer(
            duplicate_keys={"album": "artist title"}
        )

    def test_remove_duplicate(self):
        item = self.lib.items().get()
        assert item.mb_trackid == "old trackid"
        assert item.filepath.exists()

        self.importer.default_resolution = self.importer.Resolution.REMOVE
        self.importer.run()

        assert not item.filepath.exists()
        assert len(self.lib.items()) == 1
        item = self.lib.items().get()
        assert item.mb_trackid == "new trackid"

    def test_keep_duplicate(self):
        assert len(self.lib.items()) == 1

        self.importer.default_resolution = self.importer.Resolution.KEEPBOTH
        self.importer.run()

        assert len(self.lib.items()) == 2

    def test_skip_duplicate(self):
        item = self.lib.items().get()
        assert item.mb_trackid == "old trackid"

        self.importer.default_resolution = self.importer.Resolution.SKIP
        self.importer.run()

        assert len(self.lib.items()) == 1
        item = self.lib.items().get()
        assert item.mb_trackid == "old trackid"

    def test_keep_when_extra_key_is_different(self):
        config["import"]["duplicate_keys"]["item"] = "artist title flex"
        item = self.lib.items().get()
        item.flex = "different"
        item.store()
        assert len(self.lib.items()) == 1

        self.importer.default_resolution = self.importer.Resolution.SKIP
        self.importer.run()

        assert len(self.lib.items()) == 2

    def test_twice_in_import_dir(self):
        self.skipTest("write me")

    def add_item_fixture(self, **kwargs):
        # Move this to TestHelper
        item = self.add_item_fixtures()[0]
        item.update(kwargs)
        item.store()
        return item


class TagLogTest(unittest.TestCase):
    def test_tag_log_line(self):
        sio = StringIO()
        handler = logging.StreamHandler(sio)
        session = _common.import_session(loghandler=handler)
        session.tag_log("status", "path")
        assert "status path" in sio.getvalue()

    def test_tag_log_unicode(self):
        sio = StringIO()
        handler = logging.StreamHandler(sio)
        session = _common.import_session(loghandler=handler)
        session.tag_log("status", "caf\xe9")  # send unicode
        assert "status caf\xe9" in sio.getvalue()


class ResumeImportTest(ImportTestCase):
    @patch("beets.plugins.send")
    def test_resume_album(self, plugins_send):
        self.prepare_albums_for_import(2)
        self.importer = self.setup_importer(autotag=False, resume=True)

        # Aborts import after one album. This also ensures that we skip
        # the first album in the second try.
        def raise_exception(event, **kwargs):
            if event == "album_imported":
                raise importer.ImportAbortError

        plugins_send.side_effect = raise_exception

        self.importer.run()
        assert len(self.lib.albums()) == 1
        assert self.lib.albums("album:'Album 1'").get() is not None

        self.importer.run()
        assert len(self.lib.albums()) == 2
        assert self.lib.albums("album:'Album 2'").get() is not None

    @patch("beets.plugins.send")
    def test_resume_singleton(self, plugins_send):
        self.prepare_album_for_import(2)
        self.importer = self.setup_singleton_importer(
            autotag=False, resume=True
        )

        # Aborts import after one track. This also ensures that we skip
        # the first album in the second try.
        def raise_exception(event, **kwargs):
            if event == "item_imported":
                raise importer.ImportAbortError

        plugins_send.side_effect = raise_exception

        self.importer.run()
        assert len(self.lib.items()) == 1
        assert self.lib.items("title:'Track 1'").get() is not None

        self.importer.run()
        assert len(self.lib.items()) == 2
        assert self.lib.items("title:'Track 1'").get() is not None


class IncrementalImportTest(AsIsImporterMixin, ImportTestCase):
    def test_incremental_album(self):
        importer = self.run_asis_importer(incremental=True)

        # Change album name so the original file would be imported again
        # if incremental was off.
        album = self.lib.albums().get()
        album["album"] = "edited album"
        album.store()

        importer.run()
        assert len(self.lib.albums()) == 2

    def test_incremental_item(self):
        importer = self.run_asis_importer(incremental=True, singletons=True)

        # Change track name so the original file would be imported again
        # if incremental was off.
        item = self.lib.items().get()
        item["artist"] = "edited artist"
        item.store()

        importer.run()
        assert len(self.lib.items()) == 2

    def test_invalid_state_file(self):
        with open(self.config["statefile"].as_filename(), "wb") as f:
            f.write(b"000")
        self.run_asis_importer(incremental=True)
        assert len(self.lib.albums()) == 1


def _mkmp3(path):
    shutil.copyfile(
        syspath(os.path.join(_common.RSRC, b"min.mp3")),
        syspath(path),
    )


class AlbumsInDirTest(BeetsTestCase):
    def setUp(self):
        super().setUp()

        # create a directory structure for testing
        self.base = os.path.abspath(os.path.join(self.temp_dir, b"tempdir"))
        os.mkdir(syspath(self.base))

        os.mkdir(syspath(os.path.join(self.base, b"album1")))
        os.mkdir(syspath(os.path.join(self.base, b"album2")))
        os.mkdir(syspath(os.path.join(self.base, b"more")))
        os.mkdir(syspath(os.path.join(self.base, b"more", b"album3")))
        os.mkdir(syspath(os.path.join(self.base, b"more", b"album4")))

        _mkmp3(os.path.join(self.base, b"album1", b"album1song1.mp3"))
        _mkmp3(os.path.join(self.base, b"album1", b"album1song2.mp3"))
        _mkmp3(os.path.join(self.base, b"album2", b"album2song.mp3"))
        _mkmp3(os.path.join(self.base, b"more", b"album3", b"album3song.mp3"))
        _mkmp3(os.path.join(self.base, b"more", b"album4", b"album4song.mp3"))

    def test_finds_all_albums(self):
        albums = list(albums_in_dir(self.base))
        assert len(albums) == 4

    def test_separates_contents(self):
        found = []
        for _, album in albums_in_dir(self.base):
            found.append(re.search(rb"album(.)song", album[0]).group(1))
        assert b"1" in found
        assert b"2" in found
        assert b"3" in found
        assert b"4" in found

    def test_finds_multiple_songs(self):
        for _, album in albums_in_dir(self.base):
            n = re.search(rb"album(.)song", album[0]).group(1)
            if n == b"1":
                assert len(album) == 2
            else:
                assert len(album) == 1


class MultiDiscAlbumsInDirTest(BeetsTestCase):
    def create_music(self, files=True, ascii=True):
        """Create some music in multiple album directories.

        `files` indicates whether to create the files (otherwise, only
        directories are made). `ascii` indicates ACII-only filenames;
        otherwise, we use Unicode names.
        """
        self.base = os.path.abspath(os.path.join(self.temp_dir, b"tempdir"))
        os.mkdir(syspath(self.base))

        name = b"CAT" if ascii else util.bytestring_path("C\xc1T")
        name_alt_case = b"CAt" if ascii else util.bytestring_path("C\xc1t")

        self.dirs = [
            # Nested album, multiple subdirs.
            # Also, false positive marker in root dir, and subtitle for disc 3.
            os.path.join(self.base, b"ABCD1234"),
            os.path.join(self.base, b"ABCD1234", b"cd 1"),
            os.path.join(self.base, b"ABCD1234", b"cd 3 - bonus"),
            # Nested album, single subdir.
            # Also, punctuation between marker and disc number.
            os.path.join(self.base, b"album"),
            os.path.join(self.base, b"album", b"cd _ 1"),
            # Flattened album, case typo.
            # Also, false positive marker in parent dir.
            os.path.join(self.base, b"artist [CD5]"),
            os.path.join(self.base, b"artist [CD5]", name + b" disc 1"),
            os.path.join(
                self.base, b"artist [CD5]", name_alt_case + b" disc 2"
            ),
            # Single disc album, sorted between CAT discs.
            os.path.join(self.base, b"artist [CD5]", name + b"S"),
        ]
        self.files = [
            os.path.join(self.base, b"ABCD1234", b"cd 1", b"song1.mp3"),
            os.path.join(self.base, b"ABCD1234", b"cd 3 - bonus", b"song2.mp3"),
            os.path.join(self.base, b"ABCD1234", b"cd 3 - bonus", b"song3.mp3"),
            os.path.join(self.base, b"album", b"cd _ 1", b"song4.mp3"),
            os.path.join(
                self.base, b"artist [CD5]", name + b" disc 1", b"song5.mp3"
            ),
            os.path.join(
                self.base,
                b"artist [CD5]",
                name_alt_case + b" disc 2",
                b"song6.mp3",
            ),
            os.path.join(self.base, b"artist [CD5]", name + b"S", b"song7.mp3"),
        ]

        if not ascii:
            self.dirs = [self._normalize_path(p) for p in self.dirs]
            self.files = [self._normalize_path(p) for p in self.files]

        for path in self.dirs:
            os.mkdir(syspath(path))
        if files:
            for path in self.files:
                _mkmp3(util.syspath(path))

    def _normalize_path(self, path):
        """Normalize a path's Unicode combining form according to the
        platform.
        """
        path = path.decode("utf-8")
        norm_form = "NFD" if sys.platform == "darwin" else "NFC"
        path = unicodedata.normalize(norm_form, path)
        return path.encode("utf-8")

    def test_coalesce_nested_album_multiple_subdirs(self):
        self.create_music()
        albums = list(albums_in_dir(self.base))
        assert len(albums) == 4
        root, items = albums[0]
        assert root == self.dirs[0:3]
        assert len(items) == 3

    def test_coalesce_nested_album_single_subdir(self):
        self.create_music()
        albums = list(albums_in_dir(self.base))
        root, items = albums[1]
        assert root == self.dirs[3:5]
        assert len(items) == 1

    def test_coalesce_flattened_album_case_typo(self):
        self.create_music()
        albums = list(albums_in_dir(self.base))
        root, items = albums[2]
        assert root == self.dirs[6:8]
        assert len(items) == 2

    def test_single_disc_album(self):
        self.create_music()
        albums = list(albums_in_dir(self.base))
        root, items = albums[3]
        assert root == self.dirs[8:]
        assert len(items) == 1

    def test_do_not_yield_empty_album(self):
        self.create_music(files=False)
        albums = list(albums_in_dir(self.base))
        assert len(albums) == 0

    def test_single_disc_unicode(self):
        self.create_music(ascii=False)
        albums = list(albums_in_dir(self.base))
        root, items = albums[3]
        assert root == self.dirs[8:]
        assert len(items) == 1

    def test_coalesce_multiple_unicode(self):
        self.create_music(ascii=False)
        albums = list(albums_in_dir(self.base))
        assert len(albums) == 4
        root, items = albums[0]
        assert root == self.dirs[0:3]
        assert len(items) == 3


class ReimportTest(AutotagImportTestCase):
    """Test "re-imports", in which the autotagging machinery is used for
    music that's already in the library.

    This works by importing new database entries for the same files and
    replacing the old data with the new data. We also copy over flexible
    attributes and the added date.
    """

    matching = AutotagStub.GOOD

    def setUp(self):
        super().setUp()

        # The existing album.
        album = self.add_album_fixture()
        album.added = 4242.0
        album.foo = "bar"  # Some flexible attribute.
        album.data_source = "original_source"
        album.store()
        item = album.items().get()
        item.baz = "qux"
        item.added = 4747.0
        item.store()

    def _setup_session(self, singletons=False):
        self.setup_importer(import_dir=self.libdir, singletons=singletons)
        self.importer.add_choice(importer.Action.APPLY)

    def _album(self):
        return self.lib.albums().get()

    def _item(self):
        return self.lib.items().get()

    def test_reimported_album_gets_new_metadata(self):
        self._setup_session()
        assert self._album().album == "\xe4lbum"
        self.importer.run()
        assert self._album().album == "the album"

    def test_reimported_album_preserves_flexattr(self):
        self._setup_session()
        self.importer.run()
        assert self._album().foo == "bar"

    def test_reimported_album_preserves_added(self):
        self._setup_session()
        self.importer.run()
        assert self._album().added == 4242.0

    def test_reimported_album_preserves_item_flexattr(self):
        self._setup_session()
        self.importer.run()
        assert self._item().baz == "qux"

    def test_reimported_album_preserves_item_added(self):
        self._setup_session()
        self.importer.run()
        assert self._item().added == 4747.0

    def test_reimported_item_gets_new_metadata(self):
        self._setup_session(True)
        assert self._item().title == "t\xeftle 0"
        self.importer.run()
        assert self._item().title == "full"

    def test_reimported_item_preserves_flexattr(self):
        self._setup_session(True)
        self.importer.run()
        assert self._item().baz == "qux"

    def test_reimported_item_preserves_added(self):
        self._setup_session(True)
        self.importer.run()
        assert self._item().added == 4747.0

    def test_reimported_item_preserves_art(self):
        self._setup_session()
        art_source = os.path.join(_common.RSRC, b"abbey.jpg")
        replaced_album = self._album()
        replaced_album.set_art(art_source)
        replaced_album.store()
        old_artpath = replaced_album.art_filepath
        self.importer.run()
        new_album = self._album()
        new_artpath = new_album.art_destination(art_source)
        assert new_album.artpath == new_artpath
        assert new_album.art_filepath.exists()
        if new_artpath != old_artpath:
            assert not old_artpath.exists()

    def test_reimported_album_has_new_flexattr(self):
        self._setup_session()
        assert self._album().get("bandcamp_album_id") is None
        self.importer.run()
        assert self._album().bandcamp_album_id == "bc_url"

    def test_reimported_album_not_preserves_flexattr(self):
        self._setup_session()

        self.importer.run()
        assert self._album().data_source == "match_source"


class ImportPretendTest(IOMixin, AutotagImportTestCase):
    """Test the pretend commandline option"""

    def setUp(self):
        super().setUp()
        self.album_track_path = self.prepare_album_for_import(1)[0]
        self.single_path = self.prepare_track_for_import(2, self.import_path)
        self.album_path = self.album_track_path.parent

    def __run(self, importer):
        with capture_log() as logs:
            importer.run()

        assert len(self.lib.items()) == 0
        assert len(self.lib.albums()) == 0

        return [line for line in logs if not line.startswith("Sending event:")]
        assert self._album().data_source == "original_source"

    def test_import_singletons_pretend(self):
        assert self.__run(self.setup_singleton_importer(pretend=True)) == [
            f"Singleton: {self.single_path}",
            f"Singleton: {self.album_track_path}",
        ]

    def test_import_album_pretend(self):
        assert self.__run(self.setup_importer(pretend=True)) == [
            f"Album: {self.import_path}",
            f"  {self.single_path}",
            f"Album: {self.album_path}",
            f"  {self.album_track_path}",
        ]

    def test_import_pretend_empty(self):
        empty_path = self.temp_dir_path / "empty"
        empty_path.mkdir()

        importer = self.setup_importer(pretend=True, import_dir=empty_path)

        assert self.__run(importer) == [f"No files imported from {empty_path}"]


def mocked_get_album_by_id(id_):
    """Return album candidate for the given id.

    The two albums differ only in the release title and artist name, so that
    ID_RELEASE_0 is a closer match to the items created by
    ImportHelper.prepare_album_for_import().
    """
    # Map IDs to (release title, artist), so the distances are different.
    album, artist = {
        ImportIdTest.ID_RELEASE_0: ("VALID_RELEASE_0", "TAG ARTIST"),
        ImportIdTest.ID_RELEASE_1: ("VALID_RELEASE_1", "DISTANT_MATCH"),
    }[id_]

    return AlbumInfo(
        album_id=id_,
        album=album,
        artist_id="some-id",
        artist=artist,
        albumstatus="Official",
        tracks=[
            TrackInfo(
                track_id="bar",
                title="foo",
                artist_id="some-id",
                artist=artist,
                length=59,
                index=9,
                track_allt="A2",
            )
        ],
    )


def mocked_get_track_by_id(id_):
    """Return track candidate for the given id.

    The two tracks differ only in the release title and artist name, so that
    ID_RELEASE_0 is a closer match to the items created by
    ImportHelper.prepare_album_for_import().
    """
    # Map IDs to (recording title, artist), so the distances are different.
    title, artist = {
        ImportIdTest.ID_RECORDING_0: ("VALID_RECORDING_0", "TAG ARTIST"),
        ImportIdTest.ID_RECORDING_1: ("VALID_RECORDING_1", "DISTANT_MATCH"),
    }[id_]

    return TrackInfo(
        track_id=id_,
        title=title,
        artist_id="some-id",
        artist=artist,
        length=59,
    )


@patch(
    "beets.metadata_plugins.track_for_id",
    Mock(side_effect=mocked_get_track_by_id),
)
@patch(
    "beets.metadata_plugins.album_for_id",
    Mock(side_effect=mocked_get_album_by_id),
)
class ImportIdTest(ImportTestCase):
    ID_RELEASE_0 = "00000000-0000-0000-0000-000000000000"
    ID_RELEASE_1 = "11111111-1111-1111-1111-111111111111"
    ID_RECORDING_0 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ID_RECORDING_1 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)

    def test_one_mbid_one_album(self):
        self.setup_importer(search_ids=[self.ID_RELEASE_0])

        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert self.lib.albums().get().album == "VALID_RELEASE_0"

    def test_several_mbid_one_album(self):
        self.setup_importer(search_ids=[self.ID_RELEASE_0, self.ID_RELEASE_1])

        self.importer.add_choice(2)  # Pick the 2nd best match (release 1).
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert self.lib.albums().get().album == "VALID_RELEASE_1"

    def test_one_mbid_one_singleton(self):
        self.setup_singleton_importer(search_ids=[self.ID_RECORDING_0])

        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert self.lib.items().get().title == "VALID_RECORDING_0"

    def test_several_mbid_one_singleton(self):
        self.setup_singleton_importer(
            search_ids=[self.ID_RECORDING_0, self.ID_RECORDING_1]
        )

        self.importer.add_choice(2)  # Pick the 2nd best match (recording 1).
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()
        assert self.lib.items().get().title == "VALID_RECORDING_1"

    def test_candidates_album(self):
        """Test directly ImportTask.lookup_candidates()."""
        task = importer.ImportTask(
            paths=self.import_dir, toppath="top path", items=[_common.item()]
        )

        task.lookup_candidates([self.ID_RELEASE_0, self.ID_RELEASE_1])

        assert {"VALID_RELEASE_0", "VALID_RELEASE_1"} == {
            c.info.album for c in task.candidates
        }

    def test_candidates_singleton(self):
        """Test directly SingletonImportTask.lookup_candidates()."""
        task = importer.SingletonImportTask(
            toppath="top path", item=_common.item()
        )

        task.lookup_candidates([self.ID_RECORDING_0, self.ID_RECORDING_1])

        assert {"VALID_RECORDING_0", "VALID_RECORDING_1"} == {
            c.info.title for c in task.candidates
        }
