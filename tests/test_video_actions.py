import unittest
from unittest import mock

from starlette.requests import Request

import main
from services.overlays import series_badge


class ExportFilterTest(unittest.TestCase):
    def test_default_export_filter_preserves_full_frame_with_blurred_background(self):
        vf = main.build_export_video_filter(preset={"width": 1080, "height": 1920})

        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", vf)
        self.assertIn("boxblur=30:1", vf)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", vf)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", vf)
        self.assertNotIn("subtitles=", vf)

    def test_export_filter_adds_top_series_badge_when_configured(self):
        vf = main.build_export_video_filter(preset={
            "series_badge_enabled": True,
            "series_badge_label": "Funniest Moment",
            "series_badge_text": "India's Got Latent EP1 - Part 10/28",
        })

        self.assertIn("drawbox=", vf)
        self.assertIn("drawtext=", vf)
        self.assertIn("Funniest Moment", vf)
        self.assertIn("India\\'s Got Latent EP1 - Part 10/28", vf)

    def test_export_short_clip_uses_faster_x264_settings(self):
        with mock.patch.object(main, "run_command") as run_command:
            run_command.return_value.returncode = 0

            exported = main.export_short_clip(
                "source.mp4",
                "short.mp4",
                0,
                30,
                preset={"encoder_preset": "veryfast", "crf": 24},
            )

        self.assertTrue(exported)
        command = run_command.call_args.args[0]
        self.assertIn("libx264", command)
        self.assertEqual("veryfast", command[command.index("-preset") + 1])
        self.assertEqual("24", command[command.index("-crf") + 1])


class SeriesBadgeTest(unittest.TestCase):
    def test_series_badge_uses_short_label_and_part_count(self):
        badge = series_badge.build_series_badge(
            "India's Got Latent EP1 full episode with judges",
            part_number=10,
            total_parts=28,
            label="Funniest Moment",
        )

        self.assertTrue(badge["series_badge_enabled"])
        self.assertEqual("Funniest Moment", badge["series_badge_label"])
        self.assertEqual("India's Got Latent EP1 full episode - Part 10/28", badge["series_badge_text"])


class YoutubeDownloadCommandTest(unittest.TestCase):
    def test_extract_youtube_video_id_accepts_common_url_shapes(self):
        cases = {
            "dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30": "dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?si=abc": "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "youtube.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, main.extract_youtube_video_id(raw))

    def test_ytdlp_command_uses_available_node_runtime(self):
        with mock.patch.object(main.shutil, "which") as which:
            which.side_effect = lambda command: {
                "node": "/usr/bin/node",
                "deno": None,
                "yt-dlp": "/usr/bin/yt-dlp",
            }.get(command)

            command = main.build_ytdlp_download_command("abc123", "uploads/out.mp4")

        self.assertIn("--js-runtimes", command)
        self.assertEqual("node:/usr/bin/node", command[command.index("--js-runtimes") + 1])


class ProcessingPipelineTest(unittest.TestCase):
    def test_processing_uses_transcript_selection_and_exports_all_clips(self):
        source_path = main.UPLOADS_DIR / "unit_core_processing.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_core_processing", "Core processing", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_core_processing",),
        )[0]["id"]
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))

        segments = [
            {"start": 0, "end": 8, "text": "First useful selected moment."},
            {"start": 10, "end": 20, "text": "Second useful selected moment."},
        ]
        clips = [
            {
                "start": 0,
                "end": 8,
                "text": segments[0]["text"],
                "title": "First short",
                "virality_score": 70,
                "completion_score": 80,
                "hook_type": "useful_tip",
                "selection_reason": "Strong first moment.",
            },
            {
                "start": 10,
                "end": 20,
                "text": segments[1]["text"],
                "title": "Second short",
                "virality_score": 75,
                "completion_score": 82,
                "hook_type": "story",
                "selection_reason": "Strong second moment.",
            },
        ]
        transcript = {"provider": "local-whisper", "segments": segments, "words": []}

        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=60),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_audio_for_clip_selection", return_value=transcript),
            mock.patch.object(main, "select_clips_for_video", return_value=clips) as select_clips,
            mock.patch.object(main, "export_short_clip", return_value=True) as export_short_clip,
        ):
            main._process_video_sync(video_id, str(source_path))

        self.assertEqual(segments, select_clips.call_args.args[0])
        self.assertEqual(2, export_short_clip.call_count)
        self.assertEqual(2, main.db_read("SELECT COUNT(*) AS count FROM shorts WHERE video_id=?", (video_id,))[0]["count"])


class VideoActionsTest(unittest.TestCase):
    def setUp(self):
        self.source_path = main.UPLOADS_DIR / "unit_actions_source.mp4"
        self.source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'completed')",
            ("unit_actions_source", "Actions source", str(self.source_path)),
        )
        self.video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_actions_source",),
        )[0]["id"]

    def tearDown(self):
        main.db_write("DELETE FROM short_uploads WHERE short_id IN (SELECT id FROM shorts WHERE video_id=?)", (self.video_id,))
        main.db_write("DELETE FROM shorts WHERE video_id=?", (self.video_id,))
        main.db_write("DELETE FROM videos WHERE id=?", (self.video_id,))
        self.source_path.unlink(missing_ok=True)
        (main.OUTPUTS_DIR / "unit_actions_short.mp4").unlink(missing_ok=True)

    def _create_short(self):
        (main.OUTPUTS_DIR / "unit_actions_short.mp4").write_text("short")
        main.db_write(
            "INSERT INTO shorts (video_id, filename, start_time, end_time, duration, title, status, upload_title, upload_description) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.video_id,
                "unit_actions_short.mp4",
                0,
                20,
                20,
                "Action short",
                "draft",
                "Action short",
                "A generated short ready to publish.",
            ),
        )
        return main.db_read(
            "SELECT id FROM shorts WHERE filename=?",
            ("unit_actions_short.mp4",),
        )[0]["id"]

    def test_delete_short_removes_file_and_database_row(self):
        short_id = self._create_short()

        deleted = main.delete_short_record(short_id)

        self.assertTrue(deleted)
        self.assertFalse((main.OUTPUTS_DIR / "unit_actions_short.mp4").exists())
        self.assertEqual([], main.db_read("SELECT id FROM shorts WHERE id=?", (short_id,)))

    def test_social_helpers_accept_generated_short_without_approval(self):
        short_id = self._create_short()

        tiktok = main.create_tiktok_ready_package(short_id)
        snapchat = main.create_snapchat_share(short_id)
        with mock.patch.object(main.youtube_upload, "upload_short_to_youtube") as upload:
            upload.return_value = {
                "platform_video_id": "yt123",
                "platform_url": "https://youtube.com/shorts/yt123",
            }
            youtube = main.create_platform_upload(short_id, "youtube")

        self.assertEqual("ready", tiktok["upload"]["status"])
        self.assertEqual("shared", snapchat["upload"]["status"])
        self.assertEqual("uploaded", youtube["status"])


class DashboardRenderingTest(unittest.TestCase):
    def test_dashboard_has_paste_url_download_control_and_direct_short_actions(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
        })
        response = main.templates.TemplateResponse(
            request,
            "index.html",
            context={
                "videos": [{
                    "id": 1,
                    "youtube_video_id": "video123",
                    "title": "Source video",
                    "published_at": "",
                    "thumbnail": "",
                    "status": "completed",
                    "source_path": str(main.UPLOADS_DIR / "video123.mp4"),
                    "error_message": "",
                    "steps_json": "",
                    "source_kind": "long",
                    "shorts": [{
                        "id": 44,
                        "filename": "ready_short.mp4",
                        "title": "Ready Short",
                        "duration": 21,
                        "start_time": 0,
                        "end_time": 21,
                        "status": "draft",
                        "latest_upload": None,
                        "platform_uploads": [],
                    }],
                }],
                "long_videos": [],
                "short_videos": [],
                "channel_id": "test",
                "ai_model": "groq",
                "has_youtube_key": True,
                "has_openai_key": False,
                "has_gemini_key": False,
                "has_groq_key": True,
                "youtube_connected": True,
                "has_youtube_oauth": True,
                "snapchat_configured": True,
                "output_preset": main.get_default_preset(),
                "output_config": main.preset_config_for_export(),
            },
        )
        html = response.body.decode()

        self.assertIn('id="youtube-url-input"', html)
        self.assertIn("Download &amp; Generate", html)
        self.assertIn("Upload to YouTube", html)
        self.assertIn("Prepare for TikTok", html)
        self.assertIn("Share to Snapchat", html)
        self.assertNotIn(">Review<", html)


class VideoSourceClassificationTest(unittest.TestCase):
    def test_parse_youtube_duration(self):
        self.assertEqual(main.parse_youtube_duration("PT45S"), 45.0)
        self.assertEqual(main.parse_youtube_duration("PT1M30S"), 90.0)
        self.assertEqual(main.parse_youtube_duration("PT1H2M3S"), 3723.0)
        self.assertIsNone(main.parse_youtube_duration(""))


if __name__ == "__main__":
    unittest.main()
