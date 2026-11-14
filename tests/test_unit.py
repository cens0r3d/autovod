#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for autovod.py - config parser, templating, argument building.

Run with:  python -m unittest discover -s tests -t . -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import autovod  # noqa: E402


class TempDirMixin:
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="autovod-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write_config(self, name, text):
        path = os.path.join(self.dir, name + ".config")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def load_config(self, text, name="tester"):
        return autovod.Config(self.write_config(name, text))

    def make_app(self, extra="", name="tester", **kwargs):
        text = (
            'STREAM_SOURCE="twitch"\n'
            'UPLOAD_SERVICE="local"\n'
            'LOCAL_FILENAME="rec_$STREAMER_NAME"\n'
            'VIDEO_TITLE="$STREAMER_NAME - TIME_DATE"\n'
            'STATE_DIR=".autovod"\n'
            'TEMP_DIR="tmp"\n'
        ) + extra
        cfg = self.load_config(text, name=name)
        return autovod.AutoVod(cfg, autovod.Log("error"), **kwargs)


class ConfigTest(TempDirMixin, unittest.TestCase):
    def test_scalars_quotes_and_comments(self):
        cfg = self.load_config(
            'A="hello world"   #? trailing comment\n'
            "B=unquoted\n"
            "C='single $NOT_EXPANDED'\n"
            'D="a # b"\n'
            'E="$A suffix"\n'
            'F=("x" "y z")\n'
            "set -a\n"
        )
        self.assertEqual(cfg.get("A"), "hello world")
        self.assertEqual(cfg.get("B"), "unquoted")
        self.assertEqual(cfg.get("C"), "single $NOT_EXPANDED")
        self.assertEqual(cfg.get("D"), "a # b")
        self.assertEqual(cfg.get("E"), "hello world suffix")
        self.assertEqual(cfg.get_list("F"), ["x", "y z"])
        self.assertFalse(cfg.warnings)

    def test_streamer_name_expansion(self):
        cfg = self.load_config('VIDEO_TITLE="$STREAMER_NAME - TIME_DATE"', name="asmongold")
        self.assertEqual(cfg.get("VIDEO_TITLE"), "asmongold - TIME_DATE")
        self.assertEqual(cfg.streamer, "asmongold")

    def test_undefined_variable_warns_instead_of_crashing(self):
        cfg = self.load_config('A="x $MISSING y"')
        self.assertEqual(cfg.get("A"), "x  y")
        self.assertTrue(any("MISSING" in warning for warning in cfg.warnings))

    def test_aliases(self):
        cfg = self.load_config('TITLE="my title"\nVISIBILITY="public"\n')
        self.assertEqual(cfg.get("VIDEO_TITLE"), "my title")
        self.assertEqual(cfg.get("VIDEO_VISIBILITY"), "public")

    def test_types_and_defaults(self):
        cfg = self.load_config('B="true"\nI="7"\nF="2.5"\n')
        self.assertTrue(cfg.get_bool("B"))
        self.assertFalse(cfg.get_bool("MISSING"))
        self.assertTrue(cfg.get_bool("MISSING", True))
        self.assertEqual(cfg.get_int("I"), 7)
        self.assertEqual(cfg.get_int("MISSING", 3), 3)
        self.assertAlmostEqual(cfg.get_float("F"), 2.5)
        with self.assertRaises(autovod.ConfigError):
            cfg.get_int("B")

    def test_env_override(self):
        os.environ["AUTOVOD_POLL_INTERVAL"] = "7"
        try:
            cfg = self.load_config('POLL_INTERVAL="60"\n')
            self.assertEqual(cfg.get("POLL_INTERVAL"), "7")
        finally:
            del os.environ["AUTOVOD_POLL_INTERVAL"]

    def test_missing_file(self):
        with self.assertRaises(autovod.ConfigError):
            autovod.Config(os.path.join(self.dir, "nope.config"))


class DurationTest(unittest.TestCase):
    def test_formats(self):
        for value, expected in (("12:00:00", 43200), ("06:00:00", 21600), ("0:30", 30),
                                ("1:30", 90), ("45", 45), ("45s", 45), ("30m", 1800),
                                ("1h30m", 5400), ("2h", 7200), ("", None)):
            self.assertEqual(autovod.parse_duration(value, None), expected, value)

    def test_invalid(self):
        for value in ("abc", "1:2:3:4", "12:xx"):
            with self.assertRaises(autovod.ConfigError):
                autovod.parse_duration(value)

    def test_format_roundtrip(self):
        self.assertEqual(autovod.format_duration(43200), "12:00:00")
        self.assertEqual(autovod.format_duration(0), "00:00:00")


class ProbeTest(unittest.TestCase):
    def test_live(self):
        probe = autovod.parse_probe_output(json.dumps({
            "plugin": "twitch",
            "metadata": {"id": "1", "title": "Hello", "category": "Music"},
            "streams": {"best": {"type": "hls"}, "720p": {"type": "hls"}},
        }))
        self.assertTrue(probe.live)
        self.assertEqual(probe.title, "Hello")
        self.assertEqual(probe.game, "Music")
        self.assertEqual(probe.stream_id, "1")
        self.assertIn("720p", probe.streams)

    def test_offline_error_object(self):
        probe = autovod.parse_probe_output('{"error": "No playable streams found on this URL: twitch.tv/x"}')
        self.assertFalse(probe.live)
        self.assertIn("No playable streams", probe.error)

    def test_garbage_and_empty_streams(self):
        self.assertFalse(autovod.parse_probe_output("streamlink is confused").live)
        self.assertFalse(autovod.parse_probe_output("").live)
        self.assertFalse(autovod.parse_probe_output('{"streams": {}}').live)
        self.assertIn("no output", autovod.parse_probe_output("").error)

    def test_leading_noise_is_tolerated(self):
        probe = autovod.parse_probe_output('warning: something\n{"streams": {"best": {}}, "metadata": {}}')
        self.assertTrue(probe.live)


class YoutubeTitleTest(TempDirMixin, unittest.TestCase):
    def test_short_title_is_untouched(self):
        self.assertEqual(autovod.clamp_youtube_title("streamer - title", "title"), "streamer - title")

    def test_long_title_keeps_date_and_part_suffix(self):
        stream = "Nächste Runde Carsten Stahl vs. ZDF"
        raw = ("errungenschaftensammler - %s - Just Chatting | 20-10-26 22_39_55 Part: 3" % stream)
        self.assertGreater(len(raw), 100)
        out = autovod.clamp_youtube_title(raw, stream)
        self.assertEqual(len(out), 100)
        self.assertTrue(out.endswith("Part: 3"))
        self.assertIn("20-10-26 22_39_55", out)
        self.assertIn("…", out)

    def test_angle_brackets_are_stripped(self):
        self.assertEqual(autovod.clamp_youtube_title("a <b> c"), "a b c")

    def test_hard_limit_without_a_known_stream_title(self):
        self.assertEqual(len(autovod.clamp_youtube_title("x" * 150)), 100)

    def test_youtube_metadata_clamps_a_rendered_part(self):
        import datetime
        app = self.make_app(extra='UPLOAD_SERVICE="youtube"\n'
                                  'VIDEO_TITLE="$STREAMER_NAME - {{TITLE}} - {{GAME}} | TIME_DATE TIME_CLOCK"\n',
                            name="errungenschaftensammler")
        stream = "Nächste Runde: Carsten Stahl vs. ZDF und danach die grosse Diskussion"
        part = autovod.Part(1, "", "", "", "", 0, datetime.datetime.now(),
                            {"title": stream, "category": "Just Chatting"})
        part.title = app.render(app.cfg.get("VIDEO_TITLE"), part=part, metadata=part.metadata)
        self.assertGreater(len(part.title), 100)
        meta = app.youtube_metadata(part)
        self.assertLessEqual(len(meta["title"]), 100)
        self.assertIn(part.now.strftime("%d-%m-%y"), meta["title"])


class RenderTest(TempDirMixin, unittest.TestCase):
    def test_legacy_placeholders(self):
        app = self.make_app()
        out = app.render("$STREAMER_NAME - TIME_DATE TIME_CLOCK", metadata={"title": "T"})
        self.assertTrue(out.startswith("tester - "))
        self.assertNotIn("TIME_DATE", out)
        self.assertNotIn("TIME_CLOCK", out)

    def test_modern_placeholders(self):
        app = self.make_app()
        out = app.render("{{STREAMER}}|{{TITLE}}|{{GAME}}|{{ID}}|{{PART}}",
                         metadata={"title": "Stream", "category": "Games", "id": "42"})
        self.assertEqual(out, "tester|Stream|Games|42|")

    def test_two_x_sentinel_values(self):
        app = self.make_app()
        self.assertEqual(app.render("x initial_title y", metadata={"title": "Real"}), "x Real y")
        self.assertEqual(app.render("x inital_game y", metadata={"category": "Chess"}), "x Chess y")

    def test_metadata_is_not_executed(self):
        """The 2.x eval based substitution ran code embedded in a stream title."""
        app = self.make_app()
        nasty = '$(touch %s) `id` "; rm -rf /' % os.path.join(self.dir, "pwned")
        out = app.render("{{TITLE}}", metadata={"title": nasty})
        self.assertEqual(out, nasty)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "pwned")))

    def test_good_filename(self):
        self.assertEqual(autovod.good_filename('a/b:c*d?"e<f>g|h'), "a-b-c-d-e-f-g-h")
        self.assertEqual(autovod.good_filename("  a   b  "), "a b")


class StateAndLockTest(TempDirMixin, unittest.TestCase):
    def test_part_numbering_per_day(self):
        app = self.make_app(extra='VIDEO_DURATION="00:10:00"\n')
        now = autovod.datetime(2026, 10, 15, 12, 0, 0)
        self.assertEqual(app.next_part_index(now), 1)
        app.state.save()
        self.assertEqual(app.next_part_index(now), 2)
        app.state.save()
        later = autovod.datetime(2026, 10, 16, 1, 0, 0)
        self.assertEqual(app.next_part_index(later), 1)

    def test_state_survives_restart(self):
        app = self.make_app()
        now = autovod.datetime(2026, 10, 15, 12, 0, 0)
        app.next_part_index(now)
        app.state.save()
        second = self.make_app()
        self.assertEqual(second.next_part_index(now), 2)

    def test_make_part_suffix(self):
        app = self.make_app(extra='SPLIT_INTO_PARTS="true"\nSPLIT_VIDEO_DURATION="01:00:00"\n')
        now = autovod.datetime(2026, 10, 15, 12, 0, 0)
        part = app.make_part({"title": "T"}, now)
        self.assertEqual(part.index, 1)
        self.assertEqual(part.suffix, "")
        self.assertEqual(part.title, "tester - 15-10-26 Part: 1")
        app.state.save()
        part2 = app.make_part({"title": "T"}, now)
        self.assertEqual(part2.suffix, "-Part_2")
        self.assertTrue(part2.title.endswith("Part: 2"))

    def test_no_suffix_without_split_on_first_part(self):
        app = self.make_app(extra='SPLIT_INTO_PARTS="false"\n')
        part = app.make_part({"title": "T"}, autovod.datetime(2026, 10, 15, 12, 0, 0))
        self.assertEqual(part.suffix, "")
        self.assertEqual(part.title, "tester - 15-10-26")

    def test_unique_path(self):
        app = self.make_app()
        target = os.path.join(self.dir, "file.mkv")
        self.assertEqual(app.unique_path(target), target)
        open(target, "w").close()
        self.assertEqual(app.unique_path(target), os.path.join(self.dir, "file-1.mkv"))

    def test_lock_is_exclusive(self):
        path = os.path.join(self.dir, "t.lock")
        first = autovod.FileLock(path)
        first.acquire()
        try:
            with self.assertRaises(autovod.LockError):
                autovod.FileLock(path).acquire()
        finally:
            first.release()
        second = autovod.FileLock(path)
        second.acquire()
        second.release()


class ArgumentsTest(TempDirMixin, unittest.TestCase):
    def test_streamlink_record_argv(self):
        app = self.make_app(extra='STREAMLINK_QUALITY="1080p60"\nQUALITY_FALLBACK="best"\nVIDEO_DURATION="01:00:00"\n')
        argv = app.streamlink_argv(duration=3600)
        self.assertEqual(argv[0], "streamlink")
        self.assertIn("--stdout", argv)
        self.assertIn("--retry-streams", argv)
        self.assertEqual(argv[argv.index("--hls-duration") + 1], "01:00:00")
        self.assertEqual(argv[-2], "twitch.tv/tester")
        self.assertEqual(argv[-1], "1080p60,best")

    def test_best_quality_has_no_fallback_list(self):
        app = self.make_app(extra='STREAMLINK_QUALITY="best"\n')
        self.assertEqual(app.streamlink_argv()[-1], "best")

    def test_deprecated_flags_are_dropped(self):
        app = self.make_app(extra='STREAMLINK_FLAGS=("--twitch-disable-ads" "--twitch-disable-reruns" "--foo")\n')
        flags = app.streamlink_flags()
        self.assertEqual(flags, ["--foo"])

    def test_array_comment_parentheses_stay_out_of_the_array(self):
        # the trailing comment in real configs mentions parentheses; the array
        # must end at the first ')' and the comment must not leak into a flag
        cfg = self.load_config(
            'STREAMLINK_FLAGS=("" "--foo=bar baz") #? (NB! Array) (https://x.y)\n')
        self.assertEqual(cfg.arrays["STREAMLINK_FLAGS"], ["", "--foo=bar baz"])

    def test_empty_flags_are_dropped(self):
        # an empty argv element makes streamlink 7.x exit 0 without any output
        app = self.make_app(extra='STREAMLINK_FLAGS=("" "--foo")\n')
        self.assertEqual(app.streamlink_flags(), ["--foo"])

    def test_relative_binary_is_resolved_against_the_config_dir(self):
        app = self.make_app(extra='STREAMLINK_BIN=(".venv/bin/streamlink")\n')
        self.assertEqual(app.streamlink_cmd[0],
                         os.path.join(app.cfg.dir, ".venv", "bin", "streamlink"))

    def test_bare_binary_stays_a_path_lookup(self):
        app = self.make_app()
        self.assertEqual(app.streamlink_cmd, ["streamlink"])

    def test_restream_forces_h264_codec_preference(self):
        app = self.make_app(extra='UPLOAD_SERVICE="restream"\nRTMPS_STREAM_KEY="key"\n')
        self.assertIn("--twitch-supported-codecs=h264", app.streamlink_flags())

    def test_kick_and_youtube_urls(self):
        app = self.make_app(extra='STREAM_SOURCE="kick"\n')
        self.assertEqual(app.stream_url, "kick.com/tester")
        app = self.make_app(extra='STREAM_SOURCE="youtube"\n')
        self.assertEqual(app.stream_url, "youtube.com/@tester/live")
        app = self.make_app(extra='STREAM_URL="https://example.com/live.m3u8"\n')
        self.assertEqual(app.stream_url, "https://example.com/live.m3u8")
        with self.assertRaises(autovod.ConfigError):
            self.make_app(extra='STREAM_SOURCE="myspace"\n')

    def test_bin_overrides(self):
        app = self.make_app(extra='STREAMLINK_BIN=("/opt/venv/bin/streamlink" "--loglevel" "debug")\n')
        argv = app.streamlink_argv()
        self.assertEqual(argv[0], "/opt/venv/bin/streamlink")

    def test_audio_settings(self):
        app = self.make_app(extra='AUDIO_BITRATE="192"\n')
        audio = app.audio_settings()
        self.assertFalse(audio["legacy"])
        self.assertEqual(audio["bitrate"], "192k")
        self.assertEqual(audio["samplerate"], 44100)
        legacy = self.make_app(extra='AUDIO_BITRATE="44100"\n').audio_settings()
        self.assertTrue(legacy["legacy"])
        self.assertEqual(legacy["bitrate"], "128k")
        self.assertEqual(legacy["samplerate"], 44100)

    def test_youtube_metadata(self):
        app = self.make_app(extra=(
            'UPLOAD_SERVICE="youtube"\n'
            'VIDEO_TITLE="T"\n'
            'VIDEO_PLAYLIST=""\n'
            'VIDEO_TAGS="a, b,c"\n'
            'VIDEO_LANGUAGE="de"\n'
            'VIDEO_DESCRIPTION="line1\\nline2"\n'
        ), name="tester")
        meta = app.youtube_metadata()
        self.assertEqual(meta["title"], "T")
        self.assertNotIn("playlistTitles", meta)
        self.assertEqual(meta["tags"], ["a", "b", "c"])
        self.assertEqual(meta["language"], "de")
        self.assertEqual(meta["description"], "line1\nline2")

    def test_youtube_metadata_playlist_and_extra_json(self):
        extra = os.path.join(self.dir, "extra.json")
        with open(extra, "w", encoding="utf-8") as handle:
            json.dump({"categoryId": "20", "madeForKids": False}, handle)
        app = self.make_app(extra=(
            'UPLOAD_SERVICE="youtube"\n'
            'VIDEO_PLAYLIST="VODs"\n'
            'VIDEO_META_EXTRA_JSON="extra.json"\n'
        ))
        meta = app.youtube_metadata()
        self.assertEqual(meta["playlistTitles"], ["VODs"])
        self.assertEqual(meta["categoryId"], "20")
        self.assertFalse(meta["madeForKids"])

    def test_preflight_reports_missing_binaries(self):
        app = self.make_app(extra='STREAMLINK_BIN=("definitely-not-installed-xyz")\n')
        problems = app.preflight()
        self.assertTrue(any("definitely-not-installed-xyz" in problem for problem in problems))

    def test_invalid_upload_service(self):
        with self.assertRaises(autovod.ConfigError):
            self.make_app(extra='UPLOAD_SERVICE="dropbox"\n')


class CliTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._cwd)

    def test_no_config(self):
        self.assertEqual(autovod.main([]), 2)

    def test_multiple_configs_need_a_name(self):
        self.write_config("a", 'UPLOAD_SERVICE="local"\nLOCAL_FILENAME="x"\n')
        self.write_config("b", 'UPLOAD_SERVICE="local"\nLOCAL_FILENAME="y"\n')
        self.assertEqual(autovod.main([]), 2)

    def test_exactly_one_config_is_auto_selected(self):
        self.write_config("solo", 'UPLOAD_SERVICE="local"\nLOCAL_FILENAME="x"\n')
        self.assertEqual(autovod.main(["--print-config"]), 0)

    def test_print_config(self):
        self.write_config("solo", 'LOCAL_FILENAME="x"\n')
        self.assertEqual(autovod.main(["-n", "solo", "--print-config"]), 0)

    def test_missing_config_file(self):
        self.assertEqual(autovod.main(["-n", "ghost"]), 2)


class TikTokFlvTest(TempDirMixin, unittest.TestCase):
    """FLV sources (TikTok LIVE): no --hls-duration, ffmpeg cuts and remuxes."""

    def tiktok_app(self, **kwargs):
        return self.make_app(extra='STREAM_SOURCE="tiktok"\n'
                                   'STREAMER_CHANNEL="schattendernacht"\n', **kwargs)

    def test_url_is_https_and_points_at_the_live_page(self):
        app = self.tiktok_app()
        self.assertEqual(app.stream_url, "https://www.tiktok.com/@schattendernacht/live")
        self.assertTrue(app.flv_mode)

    def test_at_prefix_in_the_channel_name_is_dropped(self):
        app = self.make_app(extra='STREAM_SOURCE="tiktok"\nSTREAMER_CHANNEL="@someone"\n')
        self.assertEqual(app.stream_url, "https://www.tiktok.com/@someone/live")

    def test_flv_mode_is_off_for_other_sources_and_can_be_forced(self):
        self.assertFalse(self.make_app().flv_mode)
        self.assertTrue(self.make_app(extra='STREAM_FLV="true"\n').flv_mode)
        app = self.make_app(extra='STREAM_SOURCE="tiktok"\nSTREAM_FLV="false"\n')
        self.assertFalse(app.flv_mode)

    def test_best_never_picks_hevc(self):
        selection = self.tiktok_app()._stream_selection("best")
        self.assertTrue(selection.startswith("h264_origin,"))
        self.assertTrue(selection.endswith(",best"))
        self.assertNotIn("h265", selection)

    def test_explicit_stream_name_is_kept(self):
        self.assertEqual(self.tiktok_app()._stream_selection("h264_hd"), "h264_hd")
        self.assertEqual(self.tiktok_app()._stream_selection("worst"), "h264_ao")

    def test_record_argv_has_no_hls_duration(self):
        argv = self.tiktok_app().streamlink_argv(duration=3600)
        self.assertIn("--stdout", argv)
        self.assertNotIn("--hls-duration", argv)

    def test_remux_argv_copies_and_cuts_at_the_part_length(self):
        argv = self.tiktok_app().remux_argv("/x/part.mkv.part", 40, container="matroska")
        self.assertEqual(argv[argv.index("-i") + 1], "pipe:0")
        self.assertEqual(argv[argv.index("-t") + 1], "40")
        self.assertEqual(argv[argv.index("-c") + 1], "copy")
        self.assertEqual(argv[argv.index("-f") + 1], "matroska")
        self.assertEqual(argv[-1], "/x/part.mkv.part")

    def test_remux_container_comes_from_the_extension_then_the_hint(self):
        app = self.tiktok_app()
        self.assertEqual(app.remux_container("/x/a.mp4"), "mp4")
        self.assertEqual(app.remux_container("/x/a.flv"), "flv")
        self.assertEqual(app.remux_container("/x/a.part", "matroska"), "matroska")
        self.assertEqual(app.remux_container("/x/a.raw"), "matroska")

    def test_cookie_file_is_passed_to_streamlink(self):
        cookies = os.path.join(self.dir, "cookies.txt")
        with open(cookies, "w", encoding="utf-8") as handle:
            handle.write("# Netscape HTTP Cookie File\n")
        app = self.make_app(extra='HTTP_COOKIES_FILE="cookies.txt"\n')
        self.assertIn("--http-cookies-file=%s" % cookies, app.streamlink_flags())

    def test_missing_cookie_file_only_warns(self):
        app = self.make_app(extra='HTTP_COOKIES_FILE="nope.txt"\n')
        flags = app.streamlink_flags()
        self.assertFalse(any("--http-cookies-file" in flag for flag in flags))


# ---------------------------------------------------------------------------
# COLLECT: Tageszusammenfassung
# ---------------------------------------------------------------------------

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_clip(directory, name, size, seconds):
    """Kurzer Testclip mit Ton (echter ffmpeg-Lauf, kein Stub)."""
    import subprocess
    target = os.path.join(directory, name)
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=%s:rate=24" % size,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(seconds), "-shortest", "-c:a", "aac",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-f", "matroska", target]
    subprocess.run(argv, check=True)
    return target


class CollectorTest(TempDirMixin, unittest.TestCase):
    """Sammeln, Angleichen und Zusammenfuehren - mit echten Dateien."""

    def make_collector_app(self, extra=""):
        text = (
            'STREAM_SOURCE="twitch"\n'
            'UPLOAD_SERVICE="youtube"\n'
            'VIDEO_TITLE="{{STREAMER}} LIVE"\n'
            'VIDEO_PLAYLIST="$STREAMER_NAME VODs"\n'
            'YOUTUBE_FILENAME="$STREAMER_NAME-TIME_DATE"\n'
            'COLLECT="true"\n'
            'COLLECT_DIR="pending"\n'
            'COLLECT_MIN_FREE_GB="0"\n'
            'TEMP_DIR="tmp"\n'
            'STATE_DIR=".autovod"\n'
        ) + extra
        cfg = self.load_config(text)
        app = autovod.AutoVod(cfg, autovod.Log("error"))
        return app, app.collector

    def make_part(self, index, when, base, title=None):
        return autovod.Part(index, title or "Teil %d" % index, "", "", base,
                            60, when, {}, labels=False)

    def test_store_moves_part_into_day_folder(self):
        app, collector = self.make_collector_app()
        source = os.path.join(self.dir, "x.upload.mkv")
        with open(source, "wb") as handle:
            handle.write(b"data")
        when = autovod.datetime(2026, 11, 1, 18, 5, 0)
        result = collector.store(source, self.make_part(1, when, "tester-1"), "tester-1", seconds=60)
        self.assertTrue(result.ok)
        day = "2026-11-01"
        self.assertFalse(os.path.exists(source))
        files = [name for name in os.listdir(collector.day_dir(day)) if name.endswith(".mkv")]
        self.assertEqual(len(files), 1)
        manifest = collector.manifest(day)
        self.assertEqual(len(manifest["parts"]), 1)
        self.assertEqual(manifest["parts"][0]["seconds"], 60.0)
        self.assertEqual(manifest["parts"][0]["title"], "Teil 1")

    def test_store_rebuckets_part_when_day_is_already_uploaded(self):
        """Ein Teil, der nach dem 00:01-Lauf fertig wird, zaehlt zum laufenden Tag."""
        app, collector = self.make_collector_app()
        yesterday = "2026-11-01"
        collector.save_state(yesterday, {"status": "uploaded", "uploads": []})
        source = os.path.join(self.dir, "late.upload.mkv")
        with open(source, "wb") as handle:
            handle.write(b"data")
        collector.store(source, self.make_part(1, autovod.datetime(2026, 11, 1, 23, 55), "late"),
                        "late", seconds=30)
        today = collector.day_of(autovod.datetime.now())
        entries_today = collector.manifest(today)["parts"]
        self.assertEqual(len(entries_today), 1, "Teil muss beim laufenden Tag landen")
        self.assertEqual(collector.manifest(yesterday)["parts"], [])

    def test_pending_days_ignores_today(self):
        app, collector = self.make_collector_app()
        for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
            collector.save_manifest(day, {"parts": [{"file": "x.mkv", "seconds": 1}]})
        pending = collector.pending_days(today="2026-11-03")
        self.assertEqual(pending, ["2026-11-01", "2026-11-02"])

    def test_chapters_are_skipped_for_short_parts(self):
        app, collector = self.make_collector_app()
        short = [{"title": "a", "seconds": 5}, {"title": "b", "seconds": 5}, {"title": "c", "seconds": 5}]
        self.assertEqual(collector._chapters(short), [])
        long_parts = [{"title": "a", "seconds": 60}, {"title": "b", "seconds": 90},
                      {"title": "c", "seconds": 30}]
        chapters = collector._chapters(long_parts)
        self.assertEqual(chapters[0][:8], "00:00:00")
        self.assertTrue(chapters[1].startswith("00:01:00"))
        self.assertTrue(chapters[2].startswith("00:02:30"))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe fehlen")
    def test_collect_day_merges_mixed_resolutions_into_one_upload(self):
        app, collector = self.make_collector_app()
        day = "2026-11-01"
        uploads = []

        def fake_upload(path, base, part):
            uploads.append((path, base, part.title, part.description))
            os.remove(path)  # wie der echte Uploader nach Erfolg
            return autovod.PartResult(True, "uploaded to youtube (https://youtu.be/STUB)",
                                      artifact="https://youtu.be/STUB", uploaded=True)

        app.sink.upload_file = fake_upload

        plan = [("320x240", 3), ("160x120", 2), ("320x240", 4)]
        for index, (size, seconds) in enumerate(plan, start=1):
            clip = make_clip(self.dir, "part%d.mkv" % index, size, seconds)
            when = autovod.datetime(2026, 11, 1, 18 + index, 0, 0)
            collector.store(clip, self.make_part(index, when, "tester-%d" % index),
                            "tester-%d" % index, seconds=seconds)

        result = collector.collect_day(day)
        self.assertEqual(result, 0)
        self.assertEqual(len(uploads), 1, "genau ein Upload pro Tag")
        path, base, title, description = uploads[0]
        self.assertIn("Tageszusammenfassung", title)
        self.assertIn("3 Streams", description)
        state = collector.state(day)
        self.assertEqual(state["status"], "uploaded")
        self.assertEqual(state["parts"], 3)
        self.assertAlmostEqual(state["seconds"], 9, delta=1)
        # Quell-Teile und Zwischendateien sind weg, das Manifest bleibt
        rest = sorted(os.listdir(collector.day_dir(day)))
        self.assertNotIn("part1.mkv", " ".join(rest))
        self.assertTrue(any(name.endswith(".json") for name in rest))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe fehlen")
    def test_collect_day_is_idempotent(self):
        app, collector = self.make_collector_app()
        day = "2026-11-01"
        calls = []
        app.sink.upload_file = lambda path, base, part: (
            calls.append(base), os.remove(path),
            autovod.PartResult(True, "uploaded", artifact="x", uploaded=True))[-1]
        clip = make_clip(self.dir, "only.mkv", "160x120", 2)
        collector.store(clip, self.make_part(1, autovod.datetime(2026, 11, 1, 20, 0), "only"),
                        "only", seconds=2)
        self.assertEqual(collector.collect_day(day), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(collector.collect_day(day), 0)
        self.assertEqual(len(calls), 1, "zweiter Lauf darf nicht erneut hochladen")


if __name__ == "__main__":
    unittest.main(verbosity=2)

