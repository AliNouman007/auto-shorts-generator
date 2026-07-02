import unittest
from unittest import mock

from starlette.requests import Request

import main
from services import presets
from services.comedy_v3 import model_router as comedy_v3_model_router
from services.comedy_v3 import pipeline as comedy_v3_pipeline
from services.comedy_v3 import selector as comedy_v3_selector


class ExportFilterTest(unittest.TestCase):
    def test_default_export_filter_preserves_full_frame_with_blurred_background(self):
        vf = main.build_export_video_filter(preset={"width": 1080, "height": 1920})

        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", vf)
        self.assertIn("boxblur=30:1", vf)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", vf)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", vf)
        self.assertNotIn("subtitles=", vf)

    def test_export_filter_ignores_removed_top_series_badge_config(self):
        vf = main.build_export_video_filter(preset={
            "series_badge_enabled": True,
            "series_badge_label": "Funniest Moment",
            "series_badge_text": "India's Got Latent EP1 - Part 10/28",
        })

        self.assertNotIn("drawbox=", vf)
        self.assertNotIn("drawtext=", vf)
        self.assertNotIn("Funniest Moment", vf)
        self.assertNotIn("India\\'s Got Latent EP1 - Part 10/28", vf)

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
    def test_extract_audio_scales_timeout_for_long_sources(self):
        with mock.patch.object(main, "run_command") as run_command:
            run_command.return_value.returncode = 0

            extracted = main.extract_audio(
                "long-source.mp4",
                "long-source-audio.mp3",
                source_duration=6148,
            )

        self.assertTrue(extracted)
        self.assertGreater(run_command.call_args.kwargs["timeout"], 300)

    def test_deepgram_transcription_requires_api_key(self):
        with mock.patch.object(main, "DEEPGRAM_API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "DEEPGRAM_API_KEY"):
                main.transcribe_audio_for_clip_selection("audio.mp3")

    def test_deepgram_transcription_maps_words_and_utterances_to_segments(self):
        payload = {
            "results": {
                "channels": [{
                    "alternatives": [{
                        "transcript": "Deepgram selects useful shorts.",
                        "words": [
                            {"word": "Deepgram", "start": 0.0, "end": 0.5},
                            {"punctuated_word": "selects", "start": 0.5, "end": 0.9},
                            {"word": "useful", "start": 0.9, "end": 1.2},
                            {"punctuated_word": "shorts.", "start": 1.2, "end": 1.7},
                        ],
                    }],
                }],
                "utterances": [{
                    "start": 0.0,
                    "end": 1.7,
                    "transcript": "Deepgram selects useful shorts.",
                    "words": [
                        {"word": "Deepgram", "start": 0.0, "end": 0.5},
                        {"punctuated_word": "selects", "start": 0.5, "end": 0.9},
                    ],
                }],
            },
        }
        response = mock.Mock(status_code=200)
        response.json.return_value = payload

        with (
            mock.patch.object(main, "DEEPGRAM_API_KEY", "dg-key"),
            mock.patch("main.Path") as path_cls,
            mock.patch("main.httpx.Client") as client_cls,
        ):
            path = mock.Mock()
            path.name = "audio.mp3"
            path.open = mock.mock_open(read_data=b"audio")
            path_cls.return_value = path
            client_cls.return_value.__enter__.return_value.post.return_value = response

            transcript = main.transcribe_audio_for_clip_selection("audio.mp3")

        self.assertEqual("deepgram", transcript["provider"])
        self.assertEqual("nova-2", transcript["model"])
        self.assertEqual("Deepgram selects useful shorts.", transcript["segments"][0]["text"])
        self.assertEqual("Deepgram", transcript["segments"][0]["words"][0]["word"])

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
        transcript = {"provider": "deepgram", "segments": segments, "words": []}

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

    def test_preset_defaults_to_comedy_v3_with_gemini_balanced(self):
        config = presets.normalize_preset_config({})

        self.assertEqual("comedy_v3", config["clip_engine"])
        self.assertEqual("gemini", config["comedy_v3_main_brain"])
        self.assertEqual("balanced", config["comedy_v3_quality_mode"])

    def test_select_clips_uses_v2_only_when_selected(self):
        segments = [{"start": 0, "end": 35, "text": "A complete funny setup and payoff."}]

        with (
            mock.patch.object(main.clip_selection, "select_dynamic_clips", return_value=[{"start": 0, "end": 35}]) as v2,
            mock.patch.object(main.comedy_v3_pipeline, "select_comedy_clips", return_value=[{"start": 1, "end": 30}]) as v3,
        ):
            clips = main.select_clips_for_video(
                segments,
                duration=300,
                ai_model="gemini",
                preset={"clip_engine": "v2"},
            )

        self.assertEqual([{"start": 0, "end": 35}], clips)
        v2.assert_called_once()
        v3.assert_not_called()

    def test_select_clips_uses_comedy_v3_only_when_selected(self):
        segments = [{"start": 0, "end": 35, "text": "A complete funny setup and payoff."}]

        with (
            mock.patch.object(main.clip_selection, "select_dynamic_clips", return_value=[{"start": 0, "end": 35}]) as v2,
            mock.patch.object(main.comedy_v3_pipeline, "select_comedy_clips", return_value=[{"start": 1, "end": 30}]) as v3,
        ):
            clips = main.select_clips_for_video(
                segments,
                duration=300,
                ai_model="groq",
                preset={
                    "clip_engine": "comedy_v3",
                    "comedy_v3_main_brain": "groq",
                    "comedy_v3_quality_mode": "balanced",
                },
            )

        self.assertEqual([{"start": 1, "end": 30}], clips)
        v3.assert_called_once()
        v2.assert_not_called()

    def test_comedy_v3_empty_result_does_not_fallback_to_v2(self):
        segments = [{"start": 0, "end": 35, "text": "Weak comedy filler."}]

        with (
            mock.patch.object(main.clip_selection, "select_dynamic_clips", return_value=[{"start": 0, "end": 35}]) as v2,
            mock.patch.object(main.comedy_v3_pipeline, "select_comedy_clips", return_value=[]) as v3,
        ):
            clips = main.select_clips_for_video(
                segments,
                duration=300,
                ai_model="gemini",
                preset={"clip_engine": "comedy_v3"},
            )

        self.assertEqual([], clips)
        v3.assert_called_once()
        v2.assert_not_called()

    def test_ui_exposes_comedy_v3_engine_brain_and_quality_controls(self):
        template = (main.TEMPLATES_DIR / "index.html").read_text()
        script = (main.STATIC_DIR / "app.js").read_text()

        self.assertIn('id="preset-clip-engine"', template)
        self.assertIn('value="comedy_v3"', template)
        self.assertIn('id="preset-comedy-brain"', template)
        self.assertIn('value="gemini"', template)
        self.assertIn('value="groq"', template)
        self.assertIn('id="preset-comedy-quality"', template)
        self.assertIn("clip_engine", script)
        self.assertIn("comedy_v3_main_brain", script)
        self.assertIn("comedy_v3_quality_mode", script)

    def test_comedy_v3_requires_selected_gemini_key(self):
        with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
            comedy_v3_pipeline.select_comedy_clips(
                [{"start": 0, "end": 20, "text": "Funny setup and payoff."}],
                duration=120,
                model_config={
                    "brain": "gemini",
                    "gemini_api_key": "",
                    "groq_api_key": "groq-key",
                },
            )

    def test_comedy_v3_requires_selected_groq_key(self):
        with self.assertRaisesRegex(RuntimeError, "GROQ_API_KEY"):
            comedy_v3_pipeline.select_comedy_clips(
                [{"start": 0, "end": 20, "text": "Funny setup and payoff."}],
                duration=120,
                model_config={
                    "brain": "groq",
                    "gemini_api_key": "gemini-key",
                    "groq_api_key": "",
                },
            )

    def test_groq_rate_limit_error_is_actionable(self):
        response = mock.Mock(status_code=429)
        response.text = '{"error":{"message":"rate limit exceeded"}}'
        response.json.return_value = {"error": {"message": "rate limit exceeded"}}
        response.headers = {
            "retry-after": "42",
            "x-ratelimit-limit-requests": "10",
            "x-ratelimit-remaining-requests": "0",
        }

        with (
            mock.patch("services.comedy_v3.model_router.httpx.Client") as client_cls,
            mock.patch("time.sleep") as sleep,
        ):
            client_cls.return_value.__enter__.return_value.post.return_value = response
            with self.assertRaisesRegex(
                RuntimeError,
                "Groq Comedy V3.*HTTP 429.*llama.*rate limit exceeded.*retry-after=42.*x-ratelimit-remaining-requests=0",
            ):
                comedy_v3_model_router.complete_json(
                    "prompt",
                    "groq",
                    30,
                    groq_api_key="groq-key",
                    groq_model="llama",
                )
        sleep.assert_called_once_with(43.0)

    def test_groq_rate_limit_retries_after_provider_wait(self):
        limited = mock.Mock(status_code=429)
        limited.text = '{"error":{"message":"try later"}}'
        limited.json.return_value = {"error": {"message": "try later"}}
        limited.headers = {"retry-after": "41"}

        success = mock.Mock(status_code=200)
        success.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}

        with (
            mock.patch("services.comedy_v3.model_router.httpx.Client") as client_cls,
            mock.patch("time.sleep") as sleep,
        ):
            post = client_cls.return_value.__enter__.return_value.post
            post.side_effect = [limited, success]

            result = comedy_v3_model_router.complete_json(
                "prompt",
                "groq",
                30,
                groq_api_key="groq-key",
                groq_model="llama",
            )

        self.assertEqual({"ok": True}, result)
        sleep.assert_called_once_with(42.0)
        self.assertEqual(2, post.call_count)

    def test_groq_rate_limit_waits_for_token_bucket_reset(self):
        limited = mock.Mock(status_code=429)
        limited.text = '{"error":{"message":"tokens per minute"}}'
        limited.json.return_value = {"error": {"message": "tokens per minute"}}
        limited.headers = {
            "retry-after": "3",
            "x-ratelimit-reset-tokens": "23.65s",
        }

        success = mock.Mock(status_code=200)
        success.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}

        with (
            mock.patch("services.comedy_v3.model_router.httpx.Client") as client_cls,
            mock.patch("time.sleep") as sleep,
        ):
            post = client_cls.return_value.__enter__.return_value.post
            post.side_effect = [limited, success]

            result = comedy_v3_model_router.complete_json(
                "prompt",
                "groq",
                30,
                groq_api_key="groq-key",
                groq_model="llama",
            )

        self.assertEqual({"ok": True}, result)
        sleep.assert_called_once_with(24.65)
        self.assertEqual(2, post.call_count)

    def test_groq_request_caps_output_tokens_for_on_demand_tier(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}

        with mock.patch("services.comedy_v3.model_router.httpx.Client") as client_cls:
            post = client_cls.return_value.__enter__.return_value.post
            post.return_value = response

            comedy_v3_model_router.complete_json(
                "prompt",
                "groq",
                30,
                groq_api_key="groq-key",
                groq_model="llama",
            )

        payload = post.call_args.kwargs["json"]
        self.assertLessEqual(payload["max_tokens"], 1024)

    def test_comedy_v3_prompt_samples_are_bounded(self):
        segments = [{"start": idx, "end": idx + 1, "text": f"segment {idx}"} for idx in range(100)]

        samples = comedy_v3_pipeline._samples(segments)

        self.assertLessEqual(len(samples), 24)

    def test_comedy_v3_prompt_items_are_compacted(self):
        items = [
            {
                "candidate_id": f"c_{idx}",
                "summary": "x" * 500,
                "people_involved": ["a", "b", "c", "d", "e", "f", "g"],
            }
            for idx in range(40)
        ]

        compact = comedy_v3_pipeline._compact_prompt_items(items, limit=12)

        self.assertEqual(12, len(compact))
        self.assertEqual(220, len(compact[0]["summary"]))
        self.assertEqual(["a", "b", "c", "d", "e", "f"], compact[0]["people_involved"])

    def test_balanced_selector_keeps_a_tier_and_strong_b_tier_only(self):
        clips = comedy_v3_selector.select_final_clips([
            {"candidate_id": "a", "start": 0, "end": 60, "quality_tier": "A", "worthiness_score": 0.91, "standalone_score": 0.8, "context_score": 0.8, "boundary_confidence": 0.8},
            {"candidate_id": "b", "start": 80, "end": 140, "quality_tier": "B", "worthiness_score": 0.76, "standalone_score": 0.75, "context_score": 0.75, "boundary_confidence": 0.75},
            {"candidate_id": "c", "start": 160, "end": 220, "quality_tier": "B", "worthiness_score": 0.45, "standalone_score": 0.4, "context_score": 0.4, "boundary_confidence": 0.8},
            {"candidate_id": "d", "start": 240, "end": 300, "quality_tier": "C", "worthiness_score": 0.9, "standalone_score": 0.9, "context_score": 0.9, "boundary_confidence": 0.9},
        ], quality_mode="balanced")

        self.assertEqual(["a", "b"], [clip["candidate_id"] for clip in clips])

    def test_comedy_v3_boundary_expander_includes_setup_punchline_and_reaction(self):
        segments = [
            {"start": 100, "end": 110, "text": "The host explains why everyone is teasing Raj."},
            {"start": 110, "end": 122, "text": "Raj says he is very confident today."},
            {"start": 122, "end": 130, "text": "The judge replies with a savage joke."},
            {"start": 130, "end": 138, "text": "Everyone laughs and Raj reacts."},
        ]
        expanded = comedy_v3_pipeline.expand_candidate_boundary(
            {
                "candidate_id": "c1",
                "scene_id": "scene_001",
                "rough_start": 122,
                "rough_end": 130,
                "moment_type": "savage_reply",
            },
            segments,
            max_duration=180,
        )

        self.assertEqual(100, expanded["start"])
        self.assertEqual(138, expanded["end"])
        self.assertGreaterEqual(expanded["boundary_confidence"], 0.7)

    def test_comedy_v3_dedupes_to_more_complete_joke(self):
        clips = comedy_v3_selector.select_final_clips([
            {"candidate_id": "short", "start": 120, "end": 150, "quality_tier": "A", "worthiness_score": 0.8, "standalone_score": 0.6, "context_score": 0.5, "boundary_confidence": 0.6},
            {"candidate_id": "complete", "start": 100, "end": 155, "quality_tier": "A", "worthiness_score": 0.82, "standalone_score": 0.9, "context_score": 0.9, "boundary_confidence": 0.9},
        ], quality_mode="balanced")

        self.assertEqual(["complete"], [clip["candidate_id"] for clip in clips])

    def test_clip_selection_error_marks_score_step_error(self):
        source_path = main.UPLOADS_DIR / "unit_selection_failure.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_selection_failure", "Selection failure", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_selection_failure",),
        )[0]["id"]
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))

        transcript = {
            "provider": "deepgram",
            "segments": [{"start": 0, "end": 20, "text": "Funny setup and payoff."}],
            "words": [],
        }

        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=300),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_audio_for_clip_selection", return_value=transcript),
            mock.patch.object(main, "select_clips_for_video", side_effect=RuntimeError("Groq Comedy V3 was rate-limited.")),
        ):
            main._process_video_sync(video_id, str(source_path))

        row = main.db_read("SELECT status,error_message,steps_json FROM videos WHERE id=?", (video_id,))[0]
        self.assertEqual("failed", row["status"])
        self.assertIn("rate-limited", row["error_message"])
        self.assertIn('"name": "Score & select highlights", "status": "error"', row["steps_json"])


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
