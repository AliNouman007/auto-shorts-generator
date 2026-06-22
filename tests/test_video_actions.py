import unittest

from starlette.requests import Request

import main


class ExportFilterTest(unittest.TestCase):
    def test_default_export_filter_preserves_full_frame_with_blurred_background(self):
        vf = main.build_export_video_filter()

        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", vf)
        self.assertIn("boxblur=30:1", vf)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", vf)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", vf)
        self.assertNotIn("crop=ih*9/16", vf)


class ClipSelectionTest(unittest.TestCase):
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
        self.assertNotIn("Download &amp; Generate", html)

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


if __name__ == "__main__":
    unittest.main()
