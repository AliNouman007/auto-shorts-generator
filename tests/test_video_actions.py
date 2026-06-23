import unittest
from unittest import mock

from starlette.requests import Request

import main
from services import shorts_director


class ExportFilterTest(unittest.TestCase):
    def test_default_export_filter_preserves_full_frame_with_blurred_background(self):
        vf = main.build_export_video_filter()

        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", vf)
        self.assertIn("boxblur=30:1", vf)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", vf)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", vf)
        self.assertNotIn("crop=ih*9/16", vf)

    def test_subtitle_burn_in_uses_10_point_captions(self):
        srt_path = main.OUTPUTS_DIR / "unit_caption_style.srt"
        srt_path.write_text("caption")
        self.addCleanup(lambda: srt_path.unlink(missing_ok=True))

        vf = main.build_export_video_filter(str(srt_path), {
            "caption_font_size": 10,
            "encoder_preset": "veryfast",
        })

        self.assertIn("FontSize=10", vf)
        self.assertNotIn("FontSize=12", vf)

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


class YoutubeDownloadCommandTest(unittest.TestCase):
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

    def test_missing_ytdlp_js_runtime_returns_actionable_error(self):
        with mock.patch.object(main.shutil, "which", return_value=None):
            message = main.missing_ytdlp_runtime_message()

        self.assertIn("JavaScript runtime", message)
        self.assertIn("Node.js", message)


class CaptionTranscriptionTest(unittest.TestCase):
    def test_local_whisper_transcribes_without_forcing_language_or_translation(self):
        with mock.patch.object(main, "run_command") as run_command:
            run_command.return_value.returncode = 1

            main.transcribe_with_whisper_local("unit_audio.mp3")

        command = run_command.call_args.args[0]
        self.assertIn("--task", command)
        self.assertIn("transcribe", command)
        self.assertNotIn("translate", command)
        self.assertNotIn("--language", command)

    def test_api_transcription_data_does_not_force_language_or_romanization(self):
        data = main.build_whisper_transcription_data("whisper-1")

        self.assertEqual("whisper-1", data["model"])
        self.assertEqual("verbose_json", data["response_format"])
        self.assertNotIn("language", data)
        self.assertNotIn("prompt", data)

    def test_gemini_prompt_requests_accurate_transcription_without_translation(self):
        prompt = main.build_gemini_transcription_prompt()

        self.assertIn("Transcribe", prompt)
        self.assertIn("preserve the speaker's original words", prompt)
        self.assertIn("Do not translate", prompt)
        self.assertNotIn("Roman English", prompt)


class CaptionFormattingTest(unittest.TestCase):
    def test_srt_wraps_long_caption_into_blocks_with_at_most_three_lines(self):
        segments = [{
            "start": 0,
            "end": 9,
            "text": (
                "This is a very long caption sentence that should be split into smaller "
                "subtitle blocks so it never covers the full video screen."
            ),
        }]

        srt = main.generate_srt(segments, 0, 9)

        entries = [entry.splitlines() for entry in srt.split("\n\n")]
        self.assertGreater(len(entries), 1)
        for entry in entries:
            caption_lines = entry[2:]
            self.assertLessEqual(len(caption_lines), 3)
            self.assertTrue(all(len(line) <= 32 for line in caption_lines))

    def test_caption_request_body_defaults_to_enabled(self):
        self.assertTrue(main.captions_enabled_from_body({}))
        self.assertTrue(main.captions_enabled_from_body({"captions_enabled": True}))
        self.assertFalse(main.captions_enabled_from_body({"captions_enabled": False}))

    def test_srt_preserves_transcribed_script_before_wrapping(self):
        segments = [
            {"start": 0, "end": 2, "text": "तुम कैसे हो"},
            {"start": 2, "end": 4, "text": "تم کیسے ہو"},
        ]

        srt = main.generate_srt(segments, 0, 4)

        self.assertIn("तुम कैसे हो", srt)
        self.assertIn("تم کیسے ہو", srt)


class ProcessingCaptionToggleTest(unittest.TestCase):
    def test_processing_with_captions_disabled_exports_without_srt_file(self):
        source_path = main.UPLOADS_DIR / "unit_no_captions.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_no_captions", "No captions", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_no_captions",),
        )[0]["id"]
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))
        self.addCleanup(lambda: (main.OUTPUTS_DIR / "unit_no_captions_short_01.srt").unlink(missing_ok=True))

        clip = {
            "start": 0,
            "end": 10,
            "text": "This transcript should not be burned in.",
            "title": "No caption export",
            "virality_score": 70,
            "completion_score": 80,
            "hook_type": "useful",
            "reason": "Test clip.",
        }
        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=30),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_with_whisper_local", return_value=[clip]),
            mock.patch.object(main, "select_clips_for_video", return_value=[clip]),
            mock.patch.object(main, "export_short_clip", return_value=True) as export_short_clip,
        ):
            main._process_video_sync(video_id, str(source_path), captions_enabled=False)

        self.assertIsNone(export_short_clip.call_args.args[4])
        self.assertFalse((main.OUTPUTS_DIR / "unit_no_captions_short_01.srt").exists())

    def test_processing_stores_director_upload_metadata(self):
        source_path = main.UPLOADS_DIR / "unit_director_metadata.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_director_metadata", "Director metadata", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_director_metadata",),
        )[0]["id"]
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))

        clip = {
            "start": 0,
            "end": 12,
            "text": "Most people make this mistake. Here is the fix.",
            "title": "Fix This Shorts Mistake",
            "description": "A short explanation of the mistake and the fix.",
            "upload_title": "Fix This Shorts Mistake",
            "upload_description": "Most creators keep the slow setup. Cut to the result first. #shorts #editing #creator",
            "virality_score": 84,
            "completion_score": 88,
            "hook_type": "problem_solution",
            "selection_reason": "Clear hook and payoff.",
        }
        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=30),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_with_whisper_local", return_value=[clip]),
            mock.patch.object(main, "select_clips_for_video", return_value=[clip]),
            mock.patch.object(main, "export_short_clip", return_value=True),
        ):
            main._process_video_sync(video_id, str(source_path), captions_enabled=False)

        short = main.db_read(
            "SELECT title, description, upload_title, upload_description, selection_reason FROM shorts WHERE video_id=?",
            (video_id,),
        )[0]
        self.assertEqual("Fix This Shorts Mistake", short["title"])
        self.assertEqual("A short explanation of the mistake and the fix.", short["description"])
        self.assertEqual("Fix This Shorts Mistake", short["upload_title"])
        self.assertIn("#shorts", short["upload_description"])
        self.assertEqual("Clear hook and payoff.", short["selection_reason"])


class ClipSelectionTest(unittest.TestCase):
    def test_director_prompt_requires_hcvp_json_metadata(self):
        candidates = [{
            "candidate_id": 1,
            "start": 12,
            "end": 58,
            "duration": 46,
            "text": "Most people make this mistake. Here is the fix. That is the payoff.",
            "pre_score": 80,
            "completion_score": 82,
        }]

        prompt = shorts_director.build_director_prompt(candidates, target_count=1)

        self.assertIn("Hook", prompt)
        self.assertIn("Context", prompt)
        self.assertIn("Value", prompt)
        self.assertIn("Payoff", prompt)
        self.assertIn("upload_title", prompt)
        self.assertIn("upload_description", prompt)
        self.assertIn("strict JSON", prompt)

    def test_director_clips_include_review_and_upload_metadata(self):
        segments = [
            {"start": 0, "end": 8, "text": "Most creators lose viewers in the first two seconds."},
            {"start": 8.5, "end": 23, "text": "The problem is they keep the slow setup before showing the result."},
            {"start": 23.2, "end": 41, "text": "Cut straight to the mistake, then explain the context after the viewer is curious."},
            {"start": 41.3, "end": 58, "text": "That one change makes the Short feel faster and keeps people watching."},
        ]

        clips = shorts_director.select_director_clips(segments, duration=58, ai_model="openai")

        self.assertEqual(1, len(clips))
        for field in (
            "title",
            "description",
            "upload_title",
            "upload_description",
            "hook",
            "context",
            "value",
            "payoff",
            "selection_reason",
        ):
            self.assertTrue(clips[0].get(field), field)
        self.assertLessEqual(len(clips[0]["upload_title"]), 60)

    def test_director_validation_downgrades_filler_intro(self):
        segments = [
            {"start": 0, "end": 9, "text": "Hello guys so basically today we are going to start."},
            {"start": 9.2, "end": 32, "text": "The real mistake is keeping slow setup before the useful answer."},
        ]
        clip = {
            "start": 0,
            "end": 32,
            "duration": 32,
            "text": "Hello guys so basically today we are going to start. The real mistake is keeping slow setup before the useful answer.",
            "virality_score": 80,
            "completion_score": 80,
        }

        validation = shorts_director.validate_clip_structure(clip, segments, {})

        self.assertFalse(validation["valid"])
        self.assertIn("filler_intro", validation["issues"])
        self.assertLess(validation["score"], 80)

    def test_director_snaps_clip_to_transcript_segment_boundaries(self):
        segments = [
            {"start": 10, "end": 20, "text": "Here is the hook."},
            {"start": 20.4, "end": 35, "text": "Here is the payoff."},
        ]
        clip = {"start": 12, "end": 32, "duration": 20, "text": "Here is the hook. Here is the payoff."}

        snapped = shorts_director.snap_clip_to_segment_boundaries(clip, segments)

        self.assertEqual(10, snapped["start"])
        self.assertAlmostEqual(35.25, snapped["end"])

    def test_director_candidates_respect_default_hard_max(self):
        segments = [
            {"start": i * 20, "end": (i + 1) * 20 - 1, "text": f"Here is useful point {i}. It explains one mistake and the fix."}
            for i in range(14)
        ]

        candidates = shorts_director.build_director_candidates(segments, duration=280, preset={})

        self.assertTrue(candidates)
        self.assertTrue(all(candidate["duration"] <= 180 for candidate in candidates))

    def test_scene_candidates_preserve_complete_multi_sentence_moment(self):
        segments = [
            {"start": 0, "end": 8, "text": "Today I learned something surprising."},
            {"start": 8.5, "end": 18, "text": "Most people make this mistake when they start."},
            {"start": 18.2, "end": 32, "text": "The real problem is they skip the setup and lose the audience."},
            {"start": 32.5, "end": 48, "text": "Here is the simple fix that keeps people watching until the end."},
            {"start": 48.5, "end": 62, "text": "Use the first line to create curiosity, then explain the payoff clearly."},
        ]

        candidates = main.build_scene_candidates(segments, min_duration=15, hard_max_duration=300)

        self.assertTrue(candidates)
        self.assertTrue(any(c["duration"] > 45 for c in candidates))
        self.assertTrue(all(c["start"] <= c["end"] for c in candidates))
        self.assertTrue(all(c["completion_score"] >= 0 for c in candidates))

    def test_fallback_ranking_adds_virality_metadata(self):
        candidates = [{
            "start": 0,
            "end": 42,
            "duration": 42,
            "text": "Here is the surprising mistake and the simple fix that creates a clear payoff.",
            "pre_score": 80,
            "completion_score": 90,
            "hook_type": "useful",
        }]

        ranked = main.rank_candidates_fallback(candidates, target_count=1)

        self.assertEqual(1, len(ranked))
        self.assertIn("virality_score", ranked[0])
        self.assertIn("completion_score", ranked[0])
        self.assertIn("title", ranked[0])
        self.assertIn("reason", ranked[0])

    def test_short_source_video_becomes_one_complete_clip(self):
        segments = [
            {"start": 0, "end": 20, "text": "This is already a short complete video with a clear opening."},
            {"start": 20, "end": 50, "text": "It explains one topic and gives enough context for the viewer."},
            {"start": 50, "end": 75, "text": "Then it ends with a complete payoff."},
        ]

        clips = main.select_clips_for_video(segments, duration=75, ai_model="openai")

        self.assertEqual(1, len(clips))
        self.assertEqual(0, clips[0]["start"])
        self.assertEqual(75, clips[0]["end"])
        self.assertEqual("complete_short", clips[0]["hook_type"])

    def test_fallback_short_source_video_does_not_create_overlapping_clips(self):
        clips = main.fallback_clips(75)

        self.assertEqual(1, len(clips))
        self.assertEqual(0, clips[0]["start"])
        self.assertEqual(75, clips[0]["end"])


class VideoActionsTest(unittest.TestCase):
    def setUp(self):
        self.video_id = None
        self.short_ids = []
        self.source_path = main.UPLOADS_DIR / "test_cancel_video.mp4"
        self.source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'processing')",
            ("test_cancel_video", "Test video", str(self.source_path)),
        )
        self.video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("test_cancel_video",),
        )[0]["id"]

    def tearDown(self):
        for short_id in self.short_ids:
            main.db_write("DELETE FROM shorts WHERE id=?", (short_id,))
        if self.video_id:
            main.db_write("DELETE FROM shorts WHERE video_id=?", (self.video_id,))
            main.db_write("DELETE FROM videos WHERE id=?", (self.video_id,))
        for name in (
            "unit_delete_short.mp4",
            "unit_cancel_short.mp4",
            "unit_cancel_short.srt",
            "test_cancel_video_short_99.mp4",
        ):
            (main.OUTPUTS_DIR / name).unlink(missing_ok=True)
        self.source_path.unlink(missing_ok=True)

    def _create_short(self, filename):
        (main.OUTPUTS_DIR / filename).write_text("file")
        main.db_write(
            "INSERT INTO shorts (video_id, filename, start_time, end_time, duration, caption_text) VALUES (?,?,?,?,?,?)",
            (self.video_id, filename, 0, 5, 5, ""),
        )
        short_id = main.db_read(
            "SELECT id FROM shorts WHERE filename=?",
            (filename,),
        )[0]["id"]
        self.short_ids.append(short_id)
        return short_id

    def test_delete_short_removes_file_and_database_row(self):
        short_id = self._create_short("unit_delete_short.mp4")

        deleted = main.delete_short_record(short_id)

        self.assertTrue(deleted)
        self.assertFalse((main.OUTPUTS_DIR / "unit_delete_short.mp4").exists())
        self.assertEqual([], main.db_read("SELECT id FROM shorts WHERE id=?", (short_id,)))

    def test_deleting_last_short_resets_completed_video_to_waiting(self):
        main.db_write("UPDATE videos SET status='completed' WHERE id=?", (self.video_id,))
        short_id = self._create_short("unit_delete_short.mp4")

        main.delete_short_record(short_id)

        video = main.db_read("SELECT status FROM videos WHERE id=?", (self.video_id,))[0]
        self.assertEqual("waiting", video["status"])

    def test_cleanup_cancelled_video_removes_generated_files_and_resets_status(self):
        self._create_short("unit_cancel_short.mp4")
        (main.OUTPUTS_DIR / "unit_cancel_short.srt").write_text("subtitle")

        main.cleanup_cancelled_video(self.video_id, source_started_as_download=False)

        video = main.db_read("SELECT status, steps_json FROM videos WHERE id=?", (self.video_id,))[0]
        self.assertEqual("waiting", video["status"])
        self.assertIn("Cancelled", video["steps_json"])
        self.assertEqual([], main.db_read("SELECT id FROM shorts WHERE video_id=?", (self.video_id,)))
        self.assertFalse((main.OUTPUTS_DIR / "unit_cancel_short.mp4").exists())
        self.assertFalse((main.OUTPUTS_DIR / "unit_cancel_short.srt").exists())

    def test_cleanup_cancelled_video_removes_partial_output_without_database_row(self):
        partial = main.OUTPUTS_DIR / "test_cancel_video_short_99.mp4"
        partial.write_text("partial")

        main.cleanup_cancelled_video(self.video_id, source_started_as_download=False)

        self.assertFalse(partial.exists())

    def test_delete_source_video_removes_downloaded_file_and_resets_video(self):
        self._create_short("unit_cancel_short.mp4")
        main.db_write("UPDATE videos SET status='completed' WHERE id=?", (self.video_id,))

        deleted = main.delete_source_video(self.video_id)

        video = main.db_read("SELECT status, source_path FROM videos WHERE id=?", (self.video_id,))[0]
        self.assertTrue(deleted)
        self.assertFalse(self.source_path.exists())
        self.assertEqual("detected", video["status"])
        self.assertEqual("", video["source_path"])
        self.assertEqual([], main.db_read("SELECT id FROM shorts WHERE video_id=?", (self.video_id,)))

    def test_successful_processing_clears_previous_error_message(self):
        main.db_write(
            "UPDATE videos SET error_message=?, status='waiting' WHERE id=?",
            ("old yt-dlp 403 error", self.video_id),
        )
        clip = {
            "start": 0,
            "end": 10,
            "text": "Recovered after retry.",
            "title": "Recovered clip",
            "virality_score": 70,
            "completion_score": 80,
            "hook_type": "useful",
            "reason": "Retry succeeded.",
        }

        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=30),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_with_whisper_local", return_value=[clip]),
            mock.patch.object(main, "select_clips_for_video", return_value=[clip]),
            mock.patch.object(main, "export_short_clip", return_value=True),
        ):
            main._process_video_sync(self.video_id, str(self.source_path), captions_enabled=False)

        video = main.db_read("SELECT status, error_message FROM videos WHERE id=?", (self.video_id,))[0]
        self.assertEqual("completed", video["status"])
        self.assertIsNone(video["error_message"])

class DashboardRenderingTest(unittest.TestCase):
    def test_waiting_video_shows_generate_without_download_generate(self):
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
                    "youtube_video_id": "waiting_video",
                    "title": "Waiting video",
                    "published_at": "",
                    "thumbnail": "",
                    "status": "waiting",
                    "source_path": str(main.UPLOADS_DIR / "waiting_video.mp4"),
                    "error_message": "",
                    "steps_json": "",
                    "shorts": [],
                }],
                "channel_id": "test",
                "ai_model": "groq",
                "has_youtube_key": True,
                "has_openai_key": False,
                "has_gemini_key": False,
                "has_groq_key": True,
            },
        )
        html = response.body.decode()

        self.assertIn("Generate Shorts", html)
        self.assertIn("Upload File", html)
        self.assertIn("Delete Video", html)
        self.assertIn('id="captions-toggle-1"', html)
        self.assertIn("Captions", html)
        self.assertNotIn("Download &amp; Generate", html)

    def test_dashboard_header_uses_clear_status_labels(self):
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
                "videos": [],
                "channel_id": "test",
                "ai_model": "groq",
                "has_youtube_key": True,
                "has_openai_key": False,
                "has_gemini_key": False,
                "has_groq_key": True,
                "youtube_connected": True,
                "has_youtube_oauth": True,
            },
        )
        html = response.body.decode()

        self.assertIn("API Ready", html)
        self.assertIn("Upload Connected", html)
        self.assertIn("AI Groq", html)
        self.assertNotIn("YouTube:", html)
        self.assertNotIn("YouTube API", html)

    def test_frontend_does_not_poll_youtube_status(self):
        js = (main.STATIC_DIR / "app.js").read_text()

        self.assertNotIn("/youtube/status", js)

    def test_generated_short_renders_play_button_and_video_modal(self):
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
                    "youtube_video_id": "completed_video",
                    "title": "Completed video",
                    "published_at": "",
                    "thumbnail": "",
                    "status": "completed",
                    "source_path": str(main.UPLOADS_DIR / "completed_video.mp4"),
                    "error_message": "",
                    "steps_json": "",
                    "shorts": [{
                        "id": 7,
                        "filename": "completed_video_short_01.mp4",
                        "duration": 30,
                        "start_time": 0,
                        "end_time": 30,
                        "title": "Strong hook moment",
                        "virality_score": 87,
                        "completion_score": 92,
                        "hook_type": "useful",
                        "selection_reason": "Clear hook and payoff.",
                    }],
                }],
                "channel_id": "test",
                "ai_model": "groq",
                "has_youtube_key": True,
                "has_openai_key": False,
                "has_gemini_key": False,
                "has_groq_key": True,
            },
        )
        html = response.body.decode()

        self.assertIn("Play", html)
        self.assertIn("Strong hook moment", html)
        self.assertIn("87%", html)
        self.assertIn("Completion 92%", html)
        self.assertIn("useful", html)
        self.assertIn("Clear hook and payoff.", html)
        self.assertIn('openShortPlayer("completed_video_short_01.mp4")', html)
        self.assertIn('id="short-player-modal"', html)
        self.assertIn('id="short-player-video"', html)

    def test_video_card_has_stable_shorts_container_for_live_updates(self):
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
                    "youtube_video_id": "processing_video",
                    "title": "Processing video",
                    "published_at": "",
                    "thumbnail": "",
                    "status": "processing",
                    "source_path": str(main.UPLOADS_DIR / "processing_video.mp4"),
                    "error_message": "",
                    "steps_json": "",
                    "shorts": [],
                }],
                "channel_id": "test",
                "ai_model": "groq",
                "has_youtube_key": True,
                "has_openai_key": False,
                "has_gemini_key": False,
                "has_groq_key": True,
            },
        )
        html = response.body.decode()

        self.assertIn('id="shorts-section-1"', html)
        self.assertIn('id="shorts-grid-1"', html)

    def test_queue_row_renders_delete_short_action(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/queue",
            "headers": [],
        })
        response = main.templates.TemplateResponse(
            request,
            "queue.html",
            context={
                "queue": [{
                    "short_id": 44,
                    "upload_title": "Ready Short",
                    "title": "Ready Short",
                    "source_title": "Source video",
                    "duration": 21,
                    "short_status": "approved",
                    "upload_status": "",
                    "upload_id": None,
                    "platform_url": "",
                    "error_message": "",
                }],
                "status_filter": "all",
                "youtube_connected": False,
                "has_youtube_oauth": False,
            },
        )
        html = response.body.decode()

        self.assertIn('aria-label="Delete Short from queue"', html)
        self.assertIn("deleteShort(44, this)", html)
        self.assertNotIn("short-status-approved", html)

    def test_review_page_does_not_render_approve_reject_controls_or_marks(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/short/44/review",
            "headers": [],
        })
        response = main.templates.TemplateResponse(
            request,
            "review.html",
            context={
                "short": {
                    "id": 44,
                    "video_id": 2,
                    "filename": "ready_short.mp4",
                    "source_path": "",
                    "title": "Ready Short",
                    "upload_title": "Ready Short",
                    "description": "",
                    "upload_description": "",
                    "caption_text": "",
                    "start_time": 0,
                    "end_time": 21,
                    "duration": 21,
                    "status": "approved",
                    "source_title": "Source video",
                    "youtube_video_id": "source123",
                    "virality_score": None,
                    "completion_score": None,
                    "latest_upload": None,
                },
                "youtube_connected": False,
            },
        )
        html = response.body.decode()

        self.assertNotIn(">Approve<", html)
        self.assertNotIn(">Reject<", html)
        self.assertNotIn("short-status-approved", html)


class StudioWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.video_id = None
        self.short_id = None
        self.source_path = main.UPLOADS_DIR / "studio_workflow_source.mp4"
        self.source_path.write_text("source")
        existing = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("studio_workflow_video",),
        )
        for row in existing:
            main.db_write("DELETE FROM short_analytics WHERE upload_id IN (SELECT id FROM short_uploads WHERE short_id IN (SELECT id FROM shorts WHERE video_id=?))", (row["id"],))
            main.db_write("DELETE FROM short_uploads WHERE short_id IN (SELECT id FROM shorts WHERE video_id=?)", (row["id"],))
            main.db_write("DELETE FROM shorts WHERE video_id=?", (row["id"],))
            main.db_write("DELETE FROM videos WHERE id=?", (row["id"],))
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'completed')",
            ("studio_workflow_video", "Studio workflow", str(self.source_path)),
        )
        self.video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("studio_workflow_video",),
        )[0]["id"]
        main.db_write(
            "INSERT INTO shorts "
            "(video_id, filename, start_time, end_time, duration, caption_text, title, description, "
            "status, original_start_time, original_end_time, upload_title, upload_description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.video_id,
                "studio_workflow_short_01.mp4",
                0,
                15,
                15,
                "Original caption",
                "Original title",
                "",
                "draft",
                0,
                15,
                "Original title",
                "",
            ),
        )
        self.short_id = main.db_read(
            "SELECT id FROM shorts WHERE filename=?",
            ("studio_workflow_short_01.mp4",),
        )[0]["id"]
        (main.OUTPUTS_DIR / "studio_workflow_short_01.mp4").write_text("short")

    def tearDown(self):
        if self.short_id:
            main.db_write("DELETE FROM short_analytics WHERE upload_id IN (SELECT id FROM short_uploads WHERE short_id=?)", (self.short_id,))
            main.db_write("DELETE FROM short_uploads WHERE short_id=?", (self.short_id,))
            main.db_write("DELETE FROM shorts WHERE id=?", (self.short_id,))
        if self.video_id:
            main.db_write("DELETE FROM videos WHERE id=?", (self.video_id,))
        self.source_path.unlink(missing_ok=True)
        (main.OUTPUTS_DIR / "studio_workflow_short_01.mp4").unlink(missing_ok=True)
        (main.OUTPUTS_DIR / "studio_workflow_short_01.srt").unlink(missing_ok=True)

    def test_export_filter_accepts_explicit_preset_config(self):
        preset = {
            "caption_font_size": 10,
            "width": 1080,
            "height": 1920,
        }

        srt_path = main.OUTPUTS_DIR / "studio_workflow_short_01.srt"
        srt_path.write_text("caption")

        vf = main.build_export_video_filter(str(srt_path), preset)

        self.assertIn("scale=1080:1920", vf)
        self.assertIn("FontSize=10", vf)

    def test_short_metadata_status_and_timing_helpers(self):
        main.update_short_metadata(
            self.short_id,
            {
                "title": "Edited title",
                "description": "Edited description",
                "caption_text": "Edited caption",
                "upload_title": "Upload title",
                "upload_description": "Upload description",
            },
        )
        main.update_short_status(self.short_id, "approved")
        main.update_short_timing(self.short_id, 2, 14)

        short = main.get_short_detail(self.short_id)

        self.assertEqual("Edited title", short["title"])
        self.assertEqual("Edited caption", short["caption_text"])
        self.assertEqual("approved", short["status"])
        self.assertEqual(2, short["start_time"])
        self.assertEqual(14, short["end_time"])
        self.assertEqual(0, short["original_start_time"])
        self.assertEqual(15, short["original_end_time"])

    def test_invalid_short_timing_is_rejected(self):
        with self.assertRaises(ValueError):
            main.update_short_timing(self.short_id, 20, 10)

    def test_regenerate_short_uses_edited_caption_and_timing(self):
        main.update_short_metadata(self.short_id, {"caption_text": "Edited caption for export"})
        main.update_short_timing(self.short_id, 3, 12)

        with mock.patch.object(main, "export_short_clip", return_value=True) as export_short_clip:
            regenerated = main.regenerate_short(self.short_id)

        self.assertEqual(self.short_id, regenerated["id"])
        self.assertEqual(9, regenerated["duration"])
        self.assertEqual(3, export_short_clip.call_args.args[2])
        self.assertEqual(9, export_short_clip.call_args.args[3])
        srt_path = export_short_clip.call_args.args[4]
        self.assertTrue(srt_path.endswith(".srt"))
        self.assertIn("Edited caption for export", (main.OUTPUTS_DIR / "studio_workflow_short_01.srt").read_text())

    def test_upload_requires_approved_short_and_records_success(self):
        with self.assertRaises(ValueError):
            main.create_youtube_upload(self.short_id)

        main.update_short_status(self.short_id, "approved")
        with mock.patch.object(main.youtube_upload, "upload_short_to_youtube") as upload:
            upload.return_value = {
                "platform_video_id": "yt123",
                "platform_url": "https://youtube.com/shorts/yt123",
            }
            upload_row = main.create_youtube_upload(self.short_id)

        self.assertEqual("uploaded", upload_row["status"])
        self.assertEqual("yt123", upload_row["platform_video_id"])
        self.assertEqual("https://youtube.com/shorts/yt123", upload_row["platform_url"])

    def test_upload_failure_is_retryable(self):
        main.update_short_status(self.short_id, "approved")
        with mock.patch.object(main.youtube_upload, "upload_short_to_youtube", side_effect=RuntimeError("token missing")):
            upload_row = main.create_youtube_upload(self.short_id)

        self.assertEqual("failed", upload_row["status"])
        self.assertIn("token missing", upload_row["error_message"])

        with mock.patch.object(main.youtube_upload, "upload_short_to_youtube") as upload:
            upload.return_value = {"platform_video_id": "retry123", "platform_url": "https://youtube.com/shorts/retry123"}
            retried = main.retry_upload(upload_row["id"])

        self.assertEqual("uploaded", retried["status"])
        self.assertEqual("retry123", retried["platform_video_id"])

    def test_queue_and_analytics_helpers(self):
        main.update_short_status(self.short_id, "approved")
        main.db_write(
            "INSERT INTO short_uploads (short_id, platform, status, platform_video_id, platform_url) VALUES (?,?,?,?,?)",
            (self.short_id, "youtube", "uploaded", "analytics123", "https://youtube.com/shorts/analytics123"),
        )
        upload_id = main.db_read("SELECT id FROM short_uploads WHERE short_id=?", (self.short_id,))[0]["id"]

        queue = main.get_upload_queue("all")
        self.assertTrue(any(row["short_id"] == self.short_id for row in queue))

        with mock.patch.object(main.youtube_upload, "fetch_youtube_analytics") as analytics:
            analytics.return_value = {"views": 100, "likes": 9, "comments": 2, "watch_time": 0}
            refreshed = main.refresh_upload_analytics()

        self.assertGreaterEqual(refreshed, 1)
        stored = main.db_read("SELECT * FROM short_analytics WHERE upload_id=?", (upload_id,))
        self.assertEqual(100, stored[-1]["views"])


if __name__ == "__main__":
    unittest.main()
