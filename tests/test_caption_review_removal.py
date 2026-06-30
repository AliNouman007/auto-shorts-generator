import unittest
from unittest import mock

from starlette.requests import Request

import main


class CaptionReviewRemovalTest(unittest.TestCase):
    def test_export_filter_ignores_subtitle_files(self):
        srt_path = main.OUTPUTS_DIR / "removed_caption_path.srt"
        srt_path.write_text("caption")
        self.addCleanup(lambda: srt_path.unlink(missing_ok=True))

        vf = main.build_export_video_filter(str(srt_path), {
            "width": 1080,
            "height": 1920,
            "caption_font_size": 10,
        })

        self.assertNotIn("subtitles=", vf)
        self.assertNotIn("FontSize", vf)

    def test_processing_uses_transcript_for_selection_without_writing_captions(self):
        source_path = main.UPLOADS_DIR / "unit_removed_captions.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_removed_captions", "Removed captions", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_removed_captions",),
        )[0]["id"]
        srt_path = main.OUTPUTS_DIR / "unit_removed_captions_short_01.srt"
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))
        self.addCleanup(lambda: srt_path.unlink(missing_ok=True))

        segment = {"start": 0, "end": 8, "text": "Transcript selects the useful short."}
        clip = {
            "start": 0,
            "end": 8,
            "text": segment["text"],
            "title": "Useful short",
            "virality_score": 70,
            "completion_score": 80,
            "hook_type": "useful_tip",
            "selection_reason": "Transcript still drives clip selection.",
        }
        transcript = {
            "provider": "deepgram",
            "model": "nova-2",
            "segments": [segment],
            "words": [],
        }

        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=60),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_audio_for_clip_selection", return_value=transcript),
            mock.patch.object(main, "select_clips_for_video", return_value=[clip]) as select_clips,
            mock.patch.object(main, "export_short_clip", return_value=True) as export_short_clip,
        ):
            main._process_video_sync(video_id, str(source_path))

        self.assertEqual([segment], select_clips.call_args.args[0])
        self.assertIsNone(export_short_clip.call_args.args[4])
        self.assertFalse(srt_path.exists())

    def test_dashboard_short_card_has_direct_publish_actions_without_review_or_caption_ui(self):
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

        self.assertNotIn("captions-toggle", html)
        self.assertNotIn(">Captions<", html)
        self.assertNotIn("Top Part Badge", html)
        self.assertNotIn("Badge Label", html)
        self.assertNotIn(">Review<", html)
        self.assertNotIn(">Approve<", html)
        self.assertNotIn(">Reject<", html)
        self.assertIn("Upload to YouTube", html)
        self.assertIn("Prepare for TikTok", html)
        self.assertIn("Share to Snapchat", html)

    def test_queue_has_publish_actions_without_review_or_approval_gate(self):
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
                    "short_status": "draft",
                    "upload_status": "",
                    "upload_id": None,
                    "platform_url": "",
                    "error_message": "",
                }],
                "status_filter": "all",
                "youtube_connected": True,
                "has_youtube_oauth": True,
                "snapchat_configured": True,
            },
        )
        html = response.body.decode()

        self.assertNotIn("/review", html)
        self.assertNotIn(">Edit<", html)
        self.assertIn("Upload to YouTube", html)
        self.assertIn("Prepare for TikTok", html)
        self.assertIn("Share to Snapchat", html)

    def test_social_publish_helpers_accept_generated_short_without_approval(self):
        source_path = main.UPLOADS_DIR / "unit_direct_publish_source.mp4"
        short_path = main.OUTPUTS_DIR / "unit_direct_publish_short.mp4"
        source_path.write_text("source")
        short_path.write_text("short")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'completed')",
            ("unit_direct_publish_source", "Direct publish source", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_direct_publish_source",),
        )[0]["id"]
        main.db_write(
            "INSERT INTO shorts (video_id, filename, start_time, end_time, duration, title, description, status, upload_title, upload_description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                video_id,
                "unit_direct_publish_short.mp4",
                0,
                20,
                20,
                "Direct publish short",
                "A generated short ready for publishing.",
                "draft",
                "Direct publish short",
                "A generated short ready for publishing.",
            ),
        )
        short_id = main.db_read(
            "SELECT id FROM shorts WHERE filename=?",
            ("unit_direct_publish_short.mp4",),
        )[0]["id"]
        self.addCleanup(lambda: main.db_write("DELETE FROM short_uploads WHERE short_id=?", (short_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE id=?", (short_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))
        self.addCleanup(lambda: short_path.unlink(missing_ok=True))

        tiktok = main.create_tiktok_ready_package(short_id)
        snapchat = main.create_snapchat_share(short_id)
        with mock.patch.object(main.youtube_upload, "upload_short_to_youtube") as upload:
            upload.return_value = {
                "platform_video_id": "yt-direct",
                "platform_url": "https://youtube.com/shorts/yt-direct",
            }
            youtube = main.create_platform_upload(short_id, "youtube")

        self.assertEqual("ready", tiktok["upload"]["status"])
        self.assertEqual("shared", snapchat["upload"]["status"])
        self.assertEqual("uploaded", youtube["status"])


if __name__ == "__main__":
    unittest.main()
