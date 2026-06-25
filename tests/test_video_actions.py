import unittest
from unittest import mock
import asyncio
import json

from starlette.requests import Request

import main
from services import shorts_director
from services.captions import deepgram as deepgram_captions
from services.captions import export as caption_export
from services.captions import line_builder, normalization


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

    def test_extract_youtube_video_id_rejects_invalid_values(self):
        for raw in ("", "not a url", "https://example.com/watch?v=dQw4w9WgXcQ", "too-short"):
            with self.subTest(raw=raw):
                self.assertEqual("", main.extract_youtube_video_id(raw))

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
    def test_local_whisper_transcribes_with_hindi_language_hint_without_translation(self):
        with mock.patch.object(main, "run_command") as run_command:
            run_command.return_value.returncode = 1

            main.transcribe_with_whisper_local("unit_audio.mp3")

        command = run_command.call_args.args[0]
        self.assertIn("--task", command)
        self.assertIn("transcribe", command)
        self.assertNotIn("translate", command)
        self.assertIn("--language", command)
        self.assertEqual("hi", command[command.index("--language") + 1])
        self.assertNotIn("--initial_prompt", command)

    def test_api_transcription_data_uses_language_hint_and_word_timestamps_without_prompt(self):
        data = main.build_whisper_transcription_data("whisper-1")

        self.assertEqual("whisper-1", data["model"])
        self.assertEqual("verbose_json", data["response_format"])
        self.assertEqual("hi", data["language"])
        self.assertEqual(["word", "segment"], data["timestamp_granularities[]"])
        self.assertNotIn("prompt", data)

    def test_api_transcription_data_can_keep_raw_original_text(self):
        data = main.build_whisper_transcription_data("whisper-1", caption_mode="original")

        self.assertEqual("whisper-1", data["model"])
        self.assertEqual("verbose_json", data["response_format"])
        self.assertNotIn("language", data)
        self.assertNotIn("prompt", data)
        self.assertEqual(["word", "segment"], data["timestamp_granularities[]"])

    def test_gemini_prompt_requests_accurate_transcription_without_translation(self):
        prompt = main.build_gemini_transcription_prompt()

        self.assertIn("Transcribe", prompt)
        self.assertIn("preserve the speaker's original words", prompt)
        self.assertIn("Do not translate", prompt)
        self.assertIn("Roman Hinglish", prompt)

    def test_large_groq_audio_uses_chunked_transcription(self):
        async def run_test():
            with (
                mock.patch.object(main, "GROQ_API_KEY", "key"),
                mock.patch.object(main, "should_chunk_groq_audio", return_value=True),
                mock.patch.object(main, "split_audio_for_transcription", return_value=[
                    {"path": "chunk_001.mp3", "offset": 0},
                    {"path": "chunk_002.mp3", "offset": 600},
                ]),
                mock.patch.object(main, "transcribe_groq_audio_file") as transcribe_file,
                mock.patch.object(main.Path, "unlink") as unlink,
            ):
                transcribe_file.side_effect = [
                    [{"start": 1, "end": 3, "text": "first"}],
                    [{"start": 2, "end": 5, "text": "second"}],
                ]

                segments = await main.transcribe_with_groq_api("large_audio.mp3")

            self.assertEqual([
                {"start": 1.0, "end": 3.0, "text": "first"},
                {"start": 602.0, "end": 605.0, "text": "second"},
            ], segments)
            transcribe_file.assert_has_awaits([
                mock.call("chunk_001.mp3"),
                mock.call("chunk_002.mp3"),
            ])
            self.assertEqual(2, unlink.call_count)

        import asyncio

        asyncio.run(run_test())

    def test_small_groq_audio_uses_direct_upload(self):
        async def run_test():
            with (
                mock.patch.object(main, "GROQ_API_KEY", "key"),
                mock.patch.object(main, "should_chunk_groq_audio", return_value=False),
                mock.patch.object(main, "transcribe_groq_audio_file", return_value=[{"start": 0, "end": 4, "text": "small"}]) as transcribe_file,
                mock.patch.object(main, "split_audio_for_transcription") as split_audio,
            ):
                segments = await main.transcribe_with_groq_api("small_audio.mp3")

            self.assertEqual([{"start": 0, "end": 4, "text": "small"}], segments)
            transcribe_file.assert_awaited_once_with("small_audio.mp3")
            split_audio.assert_not_called()

        import asyncio

        asyncio.run(run_test())

    def test_groq_direct_413_retries_with_chunking(self):
        async def run_test():
            with (
                mock.patch.object(main, "GROQ_API_KEY", "key"),
                mock.patch.object(main, "should_chunk_groq_audio", return_value=False),
                mock.patch.object(main, "split_audio_for_transcription", return_value=[
                    {"path": "retry_chunk.mp3", "offset": 30},
                ]),
                mock.patch.object(main.Path, "unlink"),
                mock.patch.object(main, "transcribe_groq_audio_file") as transcribe_file,
            ):
                transcribe_file.side_effect = [
                    main.GroqRequestTooLarge("Groq API error (413): Request Entity Too Large"),
                    [{"start": 5, "end": 8, "text": "retry"}],
                ]

                segments = await main.transcribe_with_groq_api("too_big.mp3")

            self.assertEqual([{"start": 35.0, "end": 38.0, "text": "retry"}], segments)
            self.assertEqual(2, transcribe_file.await_count)

        import asyncio

        asyncio.run(run_test())

    def test_groq_chunk_files_are_deleted_when_chunk_transcription_fails(self):
        async def run_test():
            with (
                mock.patch.object(main, "split_audio_for_transcription", return_value=[
                    {"path": "chunk_001.mp3", "offset": 0},
                    {"path": "chunk_002.mp3", "offset": 600},
                ]),
                mock.patch.object(main.Path, "unlink") as unlink,
                mock.patch.object(main, "transcribe_groq_audio_file", side_effect=RuntimeError("chunk failed")),
            ):
                with self.assertRaises(RuntimeError):
                    await main.transcribe_groq_audio_chunks("source.mp3")

            self.assertEqual(2, unlink.call_count)

        import asyncio

        asyncio.run(run_test())

    def test_split_audio_for_transcription_builds_ffmpeg_chunks(self):
        with (
            mock.patch.object(main, "get_audio_duration", return_value=1250),
            mock.patch.object(main, "run_command") as run_command,
        ):
            run_command.return_value.returncode = 0

            chunks = main.split_audio_for_transcription("uploads/source_audio.mp3", chunk_seconds=600, video_id=77)

        self.assertEqual([
            {"path": "uploads/source_audio_chunk_001.mp3", "offset": 0.0},
            {"path": "uploads/source_audio_chunk_002.mp3", "offset": 600.0},
            {"path": "uploads/source_audio_chunk_003.mp3", "offset": 1200.0},
        ], chunks)
        first_command = run_command.call_args_list[0].args[0]
        self.assertIn("-ss", first_command)
        self.assertIn("-t", first_command)
        self.assertEqual("600", first_command[first_command.index("-t") + 1])

    def test_offset_transcription_segments_preserves_original_timing(self):
        segments = [{"start": "2.5", "end": "6.25", "text": "chunk text"}]

        shifted = main.offset_transcription_segments(segments, 600)

        self.assertEqual([{"start": 602.5, "end": 606.25, "text": "chunk text"}], shifted)

    def test_offset_transcription_segments_offsets_nested_word_timestamps(self):
        segments = [{
            "start": 2,
            "end": 5,
            "text": "kaise ho",
            "words": [
                {"start": 2.1, "end": 2.6, "word": "kaise"},
                {"start": 2.7, "end": 3.0, "word": "ho"},
            ],
        }]

        shifted = main.offset_transcription_segments(segments, 600)

        self.assertEqual(602.1, shifted[0]["words"][0]["start"])
        self.assertEqual(603.0, shifted[0]["words"][1]["end"])

    def test_transcription_response_attaches_top_level_words_to_segments(self):
        payload = {
            "segments": [
                {"start": 0, "end": 2, "text": "kaise ho"},
                {"start": 2, "end": 4, "text": "sab theek"},
            ],
            "words": [
                {"start": 0.2, "end": 0.6, "word": "kaise"},
                {"start": 0.7, "end": 1.0, "word": "ho"},
                {"start": 2.2, "end": 2.6, "word": "sab"},
            ],
        }

        segments = main.segments_from_transcription_response(payload)

        self.assertEqual(2, len(segments))
        self.assertEqual(["kaise", "ho"], [word["word"] for word in segments[0]["words"]])
        self.assertEqual(["sab"], [word["word"] for word in segments[1]["words"]])

    def test_transcription_response_removes_instruction_leak_segments(self):
        payload = {
            "segments": [
                {"start": 0, "end": 2, "text": "kaise ho"},
                {"start": 2, "end": 4, "text": "Do not translate the meaning into English."},
            ]
        }

        segments = main.segments_from_transcription_response(payload)

        self.assertEqual(1, len(segments))
        self.assertEqual("kaise ho", segments[0]["text"])


class CaptionFormattingTest(unittest.TestCase):
    def test_deepgram_response_normalizes_utterances_and_words(self):
        payload = {
            "metadata": {"duration": 4.0, "model_info": {"id": {"arch": "nova-3"}}},
            "results": {
                "utterances": [{
                    "start": 0.1,
                    "end": 2.0,
                    "confidence": 0.91,
                    "transcript": "Hello kya haal hai?",
                    "speaker": 1,
                    "words": [
                        {"word": "hello", "punctuated_word": "Hello", "start": 0.1, "end": 0.4, "confidence": 0.99, "language": "en", "speaker": 1},
                        {"word": "kya", "start": 0.5, "end": 0.7, "confidence": 0.88, "language": "hi", "speaker": 1},
                    ],
                }],
                "channels": [{"alternatives": [{"confidence": 0.93, "languages": ["en", "hi"], "words": []}]}],
            },
        }

        result = deepgram_captions.normalize_deepgram_response(payload, model="nova-3")

        self.assertEqual("deepgram", result["provider"])
        self.assertEqual("nova-3", result["model"])
        self.assertEqual("en,hi", result["language"])
        self.assertAlmostEqual(0.93, result["confidence"])
        self.assertEqual(1, len(result["segments"]))
        self.assertEqual("Hello kya haal hai?", result["segments"][0]["text"])
        self.assertEqual("Hello", result["segments"][0]["words"][0]["word"])
        self.assertEqual(1, result["segments"][0]["speaker"])

    def test_deepgram_chunk_merge_offsets_segments_and_words(self):
        first = {
            "segments": [{"start": 1.0, "end": 2.0, "text": "hello", "words": [{"start": 1.0, "end": 1.3, "word": "hello"}]}],
            "words": [{"start": 1.0, "end": 1.3, "word": "hello"}],
            "confidence": 0.9,
            "language": "en",
            "provider": "deepgram",
            "model": "nova-3",
        }
        second = {
            "segments": [{"start": 0.5, "end": 1.5, "text": "kya haal", "words": [{"start": 0.5, "end": 0.8, "word": "kya"}]}],
            "words": [{"start": 0.5, "end": 0.8, "word": "kya"}],
            "confidence": 0.7,
            "language": "hi",
            "provider": "deepgram",
            "model": "nova-3",
        }

        merged = deepgram_captions.merge_chunk_results([(first, 0.0), (second, 480.0)])

        self.assertEqual(2, len(merged["segments"]))
        self.assertAlmostEqual(480.5, merged["segments"][1]["start"])
        self.assertAlmostEqual(480.8, merged["segments"][1]["words"][0]["end"])
        self.assertAlmostEqual(0.8, merged["confidence"])
        self.assertEqual("en,hi", merged["language"])

    def test_hinglish_normalization_keeps_english_and_romanizes_indic_scripts(self):
        text = "Hello \u0924\u0941\u092e \u0915\u0948\u0938\u0947 \u0939\u094b \u062a\u0645 \u06a9\u06cc\u0633\u06d2 \u06c1\u0648"

        normalized = normalization.normalize_caption_text(text, caption_mode="hinglish")

        self.assertIn("Hello", normalized)
        self.assertIn("tum kaise ho", normalized)
        self.assertNotIn("\u0924\u0941\u092e", normalized)
        self.assertNotIn("\u062a\u0645", normalized)

    def test_caption_cues_use_word_timestamps_and_do_not_overlap(self):
        segments = [{
            "start": 10.0,
            "end": 15.0,
            "text": "kaise ho audience hassi",
            "words": [
                {"start": 10.1, "end": 10.4, "word": "kaise", "confidence": 0.9, "speaker": 0},
                {"start": 10.5, "end": 10.8, "word": "ho", "confidence": 0.9, "speaker": 0},
                {"start": 13.0, "end": 13.4, "word": "audience", "confidence": 0.8, "speaker": 0},
                {"start": 13.5, "end": 13.8, "word": "hassi", "confidence": 0.8, "speaker": 0},
            ],
        }]

        cues = line_builder.build_caption_cues(segments, clip_start=10.0, clip_end=15.0)

        self.assertEqual(2, len(cues))
        self.assertAlmostEqual(0.1, cues[0]["start"], places=1)
        self.assertLessEqual(cues[0]["end"], cues[1]["start"])
        self.assertEqual("kaise ho", cues[0]["text"])
        self.assertEqual("audience hassi", cues[1]["text"])

    def test_srt_export_uses_structured_caption_cues(self):
        cues = [
            {"start": 0.2, "end": 1.1, "text": "kaise ho"},
            {"start": 1.4, "end": 2.2, "text": "audience hassi"},
        ]

        srt = caption_export.generate_srt_from_cues(cues)

        self.assertIn("00:00:00,200 --> 00:00:01,100", srt)
        self.assertIn("kaise ho", srt)
        self.assertIn("audience hassi", srt)

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

    def test_srt_defaults_to_hinglish_romanized_captions(self):
        segments = [
            {"start": 0, "end": 2, "text": "तुम कैसे हो"},
            {"start": 2, "end": 4, "text": "تم کیسے ہو"},
        ]

        srt = main.generate_srt(segments, 0, 4)

        self.assertIn("tum kaise ho", srt)
        self.assertNotIn("तुम कैसे हो", srt)
        self.assertNotIn("تم کیسے ہو", srt)

    def test_srt_can_preserve_original_script_when_requested(self):
        segments = [
            {"start": 0, "end": 2, "text": "तुम कैसे हो"},
            {"start": 2, "end": 4, "text": "تم کیسے ہو"},
        ]

        srt = main.generate_srt(segments, 0, 4, caption_mode="original")

        self.assertIn("तुम कैसे हो", srt)
        self.assertIn("تم کیسے ہو", srt)

    def test_srt_splits_long_segment_into_short_timed_caption_units(self):
        segments = [{
            "start": 0,
            "end": 9,
            "text": "ye joke bohat funny tha audience hassi aur judge react karta hai",
        }]

        srt = main.generate_srt(segments, 0, 9)

        entries = [entry.splitlines() for entry in srt.split("\n\n") if entry.strip()]
        self.assertGreater(len(entries), 1)
        for entry in entries:
            start_text, end_text = entry[1].split(" --> ")
            start_seconds = main.srt_time_to_seconds(start_text)
            end_seconds = main.srt_time_to_seconds(end_text)
            self.assertLessEqual(end_seconds - start_seconds, 2.8)

    def test_srt_prefers_word_timestamps_when_available(self):
        segments = [{
            "start": 0,
            "end": 8,
            "text": "kaise ho audience hassi",
            "words": [
                {"start": 1.2, "end": 1.5, "word": "kaise"},
                {"start": 1.6, "end": 1.9, "word": "ho"},
                {"start": 4.1, "end": 4.6, "word": "audience"},
                {"start": 4.7, "end": 5.1, "word": "hassi"},
            ],
        }]

        srt = main.generate_srt(segments, 0, 8)

        entries = [entry.splitlines() for entry in srt.split("\n\n") if entry.strip()]
        first_start = main.srt_time_to_seconds(entries[0][1].split(" --> ")[0])
        self.assertAlmostEqual(1.2, first_start, places=1)
        self.assertIn("kaise ho", "\n".join(entries[0][2:]))
        self.assertIn("audience hassi", "\n".join(entries[1][2:]))

    def test_srt_skips_transcription_instruction_leaks(self):
        segments = [
            {"start": 0, "end": 2, "text": "kaise ho"},
            {"start": 2, "end": 4, "text": "Do not translate the meaning into English."},
        ]

        srt = main.generate_srt(segments, 0, 4)

        self.assertIn("kaise ho", srt)
        self.assertNotIn("Do not translate", srt)


class ProcessingCaptionToggleTest(unittest.TestCase):
    def test_processing_exports_every_clip_selected_by_v2(self):
        source_path = main.UPLOADS_DIR / "unit_many_v2_clips.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_many_v2_clips", "Many V2 clips", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_many_v2_clips",),
        )[0]["id"]
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))

        clips = [
            {
                "start": idx * 40,
                "end": idx * 40 + 30,
                "duration": 30,
                "text": f"Funny selected clip {idx}",
                "title": f"Clip {idx}",
                "virality_score": 60,
                "completion_score": 75,
                "hook_type": "comedy",
                "selection_reason": "Selected by V2.",
                "timestamp_engine": "v2",
                "candidate_source": "comedy",
                "final_score": 0.5,
                "score_details_json": "{}",
                "judge_status": "deterministic_rescue",
            }
            for idx in range(24)
        ]

        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=3600),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_with_whisper_local", return_value=[clips[0]]),
            mock.patch.object(main, "select_clips_for_video", return_value=clips),
            mock.patch.object(main, "export_short_clip", return_value=True) as export_short_clip,
        ):
            main._process_video_sync(video_id, str(source_path), captions_enabled=False)

        self.assertEqual(24, export_short_clip.call_count)
        self.assertEqual(24, main.db_read("SELECT COUNT(*) AS count FROM shorts WHERE video_id=?", (video_id,))[0]["count"])

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

    def test_processing_writes_unicode_srt_as_utf8(self):
        source_path = main.UPLOADS_DIR / "unit_unicode_captions.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_unicode_captions", "Unicode captions", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_unicode_captions",),
        )[0]["id"]
        srt_path = main.OUTPUTS_DIR / "unit_unicode_captions_short_01.srt"
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))
        self.addCleanup(lambda: srt_path.unlink(missing_ok=True))

        clip = {
            "start": 0,
            "end": 12,
            "text": "यह punchline 😂 audience ko hasa deti hai.",
            "title": "Unicode clip",
            "virality_score": 80,
            "completion_score": 90,
            "hook_type": "comedy",
            "selection_reason": "Unicode caption test.",
            "timestamp_engine": "v2",
            "candidate_source": "comedy",
            "final_score": 0.8,
            "score_details_json": "{}",
            "judge_status": "deterministic_fill",
        }
        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=120),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_with_whisper_local", return_value=[clip]),
            mock.patch.object(main, "select_clips_for_video", return_value=[clip]),
            mock.patch.object(main, "export_short_clip", return_value=True),
        ):
            main._process_video_sync(video_id, str(source_path), captions_enabled=True)

        self.assertIn("😂", srt_path.read_text(encoding="utf-8"))

    def test_processing_writes_deepgram_caption_metadata_and_word_synced_srt(self):
        source_path = main.UPLOADS_DIR / "unit_deepgram_captions.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_deepgram_captions", "Deepgram captions", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_deepgram_captions",),
        )[0]["id"]
        srt_path = main.OUTPUTS_DIR / "unit_deepgram_captions_short_01.srt"
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))
        self.addCleanup(lambda: srt_path.unlink(missing_ok=True))

        segment = {
            "start": 0,
            "end": 6,
            "text": "kaise ho audience hassi",
            "words": [
                {"start": 0.2, "end": 0.5, "word": "kaise", "confidence": 0.92},
                {"start": 0.6, "end": 0.9, "word": "ho", "confidence": 0.9},
                {"start": 3.0, "end": 3.5, "word": "audience", "confidence": 0.88},
                {"start": 3.6, "end": 4.0, "word": "hassi", "confidence": 0.86},
            ],
        }
        transcript = {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "en,hi",
            "confidence": 0.89,
            "segments": [segment],
            "words": segment["words"],
            "status": "word_synced",
        }
        clip = {
            "start": 0,
            "end": 6,
            "text": segment["text"],
            "title": "Deepgram clip",
            "virality_score": 80,
            "completion_score": 90,
            "hook_type": "comedy",
        }

        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=60),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_audio_for_captions", return_value=transcript),
            mock.patch.object(main, "select_clips_for_video", return_value=[clip]),
            mock.patch.object(main, "export_short_clip", return_value=True),
        ):
            main._process_video_sync(video_id, str(source_path), captions_enabled=True)

        short = main.db_read("SELECT * FROM shorts WHERE video_id=?", (video_id,))[0]
        self.assertEqual("deepgram", short["caption_provider"])
        self.assertEqual("nova-3", short["caption_model"])
        self.assertEqual("en,hi", short["caption_language"])
        self.assertAlmostEqual(0.89, short["caption_confidence"])
        self.assertEqual("word_synced", short["caption_status"])
        self.assertIn("kaise ho", short["caption_srt"])
        self.assertIn("audience hassi", srt_path.read_text(encoding="utf-8"))

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

    def test_generated_description_does_not_paste_caption_transcript(self):
        caption_text = (
            "kaise ho audience hassi phir judge bolta hai wah kya punchline thi "
            "aur sab log clap karte hain contestant phir next joke start karta hai"
        )

        clip = shorts_director.enrich_clip_metadata({
            "start": 0,
            "end": 28,
            "text": caption_text,
            "title": "Audience Could Not Stop Laughing",
            "hook_type": "comedy",
        })

        self.assertNotEqual(caption_text, clip["description"])
        self.assertNotIn("kaise ho audience hassi", clip["description"])
        self.assertIn("Audience Could Not Stop Laughing", clip["description"])
        self.assertIn("#shorts", clip["upload_description"])


class ClipSelectionTest(unittest.TestCase):
    def test_episode_profile_fallback_extracts_dynamic_people_from_title(self):
        from services.clip_director.episode_intelligence import build_fallback_episode_profile

        profile = build_fallback_episode_profile(
            "INDIA’S GOT LATENT S2 EP1 ft. Alia Bhatt, Sharvari, Ashish Solanki",
            [],
            genre_hint="funny stage show",
        )

        names = [entity["name"] for entity in profile["important_entities"]]
        self.assertIn("Alia Bhatt", names)
        self.assertIn("Sharvari", names)
        self.assertIn("Ashish Solanki", names)
        self.assertEqual("comedy panel show", profile["show_type"])

    def test_episode_context_candidates_are_created_from_dynamic_entities(self):
        from services.clip_director.candidates import generate_candidates
        from services.clip_director.timeline import build_timeline

        timeline = build_timeline(
            [
                {"start": 0, "end": 20, "text": "Contestant starts normal setup."},
                {"start": 20, "end": 45, "text": "Then he roasts Alia and the audience starts laughing."},
                {"start": 45, "end": 72, "text": "Judges react and the punchline payoff continues."},
            ],
            duration=120,
        )
        profile = {
            "important_entities": [{"name": "Alia Bhatt", "aliases": ["alia"]}],
            "viral_moment_patterns": ["celebrity roast", "judge reaction"],
        }

        candidates = generate_candidates(
            timeline,
            {"min_duration": 30, "max_duration": 180},
            episode_profile=profile,
        )

        contextual = [candidate for candidate in candidates if candidate["candidate_source"] == "episode_context"]
        self.assertTrue(contextual)
        self.assertIn("Alia Bhatt", contextual[0]["episode_context_hits"])

    def test_comedy_reaction_candidate_expands_back_to_full_joke_setup(self):
        from services.clip_director.candidates import generate_candidates
        from services.clip_director.timeline import build_timeline

        timeline = build_timeline(
            [
                {"start": 0, "end": 8, "text": "Contestant starts the setup for the joke."},
                {"start": 8.2, "end": 18, "text": "He builds the story before the punchline."},
                {"start": 18.2, "end": 28, "text": "The punchline lands on the judges."},
                {"start": 28.2, "end": 36, "text": "Audience laughing applause gets loud."},
            ],
            duration=90,
        )

        candidates = generate_candidates(
            timeline,
            {"min_duration": 30, "max_duration": 180},
        )

        comedy_candidates = [
            candidate for candidate in candidates
            if candidate["candidate_source"] == "comedy"
        ]
        self.assertTrue(comedy_candidates)
        self.assertLessEqual(comedy_candidates[0]["start"], 0.1)
        self.assertGreaterEqual(comedy_candidates[0]["end"], 36)

    def test_performance_candidates_are_created_for_dance_and_stage_reactions(self):
        from services.clip_director.candidates import generate_candidates
        from services.clip_director.timeline import build_timeline

        timeline = build_timeline(
            [
                {"start": 0, "end": 12, "text": "Contestant starts a dance performance on stage."},
                {"start": 12.2, "end": 25, "text": "The judge cracks a joke during the act."},
                {"start": 25.2, "end": 42, "text": "Crowd applause and laughing reaction make the moment bigger."},
            ],
            duration=120,
        )

        candidates = generate_candidates(
            timeline,
            {"min_duration": 30, "max_duration": 180},
        )

        performance_candidates = [
            candidate for candidate in candidates
            if candidate["candidate_source"] == "performance_moment"
        ]
        self.assertTrue(performance_candidates)
        self.assertIn("dance performance", performance_candidates[0]["text"].lower())

    def test_episode_context_boost_rewards_entity_with_reaction_more_than_name_only(self):
        from services.clip_director.scoring import score_candidate

        profile = {
            "important_entities": [{"name": "Alia Bhatt", "aliases": ["alia"]}],
            "viral_moment_patterns": ["celebrity roast"],
        }
        name_only = {
            "candidate_id": "name",
            "candidate_source": "episode_context",
            "start": 0,
            "end": 50,
            "duration": 50,
            "text": "Alia is sitting there and the conversation continues.",
            "episode_profile": profile,
            "episode_context_hits": ["Alia Bhatt"],
        }
        with_reaction = {
            **name_only,
            "candidate_id": "reaction",
            "text": "Alia gets roasted in the joke and the audience laughing applause follows the punchline.",
        }

        name_score = score_candidate(name_only)
        reaction_score = score_candidate(with_reaction)

        self.assertGreater(reaction_score["feature_scores"]["episodeContext"], name_score["feature_scores"]["episodeContext"])
        self.assertGreater(reaction_score["final_score"], name_score["final_score"])

    def test_visual_performance_score_rewards_stage_actions_with_reaction(self):
        from services.clip_director.scoring import score_candidate

        performance = score_candidate({
            "candidate_id": "dance",
            "candidate_source": "performance_moment",
            "start": 0,
            "end": 42,
            "duration": 42,
            "text": "Contestant dance performance gets crowd applause and judge reaction.",
        })

        self.assertGreaterEqual(performance["feature_scores"]["visual"], 0.75)
        self.assertGreater(performance["final_score"], 0.45)

    def test_director_prompt_includes_episode_profile_for_judge(self):
        from services.clip_director.prompts import build_director_prompt

        prompt = build_director_prompt(
            {
                "title": "Comedy episode",
                "duration": 600,
                "mode": "shorts",
                "episode_profile": {
                    "show_type": "comedy panel show",
                    "important_entities": [{"name": "Dynamic Guest", "aliases": ["guest"]}],
                    "viral_moment_patterns": ["guest roast"],
                },
                "shortlisted_candidates": [{"candidate_id": "v2-1", "start": 0, "end": 60, "text": "guest roast"}],
            },
            {"mode": "shorts", "min_duration": 30, "max_duration": 180, "safety_max_clips": 30},
        )

        self.assertIn("episode_profile", prompt)
        self.assertIn("Dynamic Guest", prompt)

    def test_v2_pipeline_scores_snaps_dedupes_and_requires_llm_judge(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = [
            {"start": 8, "end": 22, "text": "Why does this mistake ruin shorts?"},
            {"start": 22.4, "end": 42, "text": "Because the setup hides the payoff and viewers leave."},
            {"start": 42.2, "end": 59, "text": "Here is the fix that creates a strong reveal and payoff."},
            {"start": 90, "end": 112, "text": "Another weaker topic starts without much value."},
        ]

        def fake_judge(episode_map, constraints, ai_model):
            candidates = episode_map["shortlisted_candidates"]
            return [{
                "candidate_id": candidates[0]["candidate_id"],
                "rank": 1,
                "start": candidates[0]["start"] + 1.0,
                "end": candidates[0]["end"] - 1.0,
                "title": "Fix The Retention Mistake",
                "reason": "Best hook and payoff.",
            }]

        clips = select_dynamic_clips(
            segments,
            duration=140,
            audio_peaks=[{"start": 38, "end": 41, "peak_time": 39, "energy": 0.95}],
            ai_model="openai",
            mode="shorts",
            llm_selector=fake_judge,
            allow_fallback=False,
            use_v2=True,
        )

        self.assertEqual(1, len(clips))
        self.assertEqual("v2", clips[0]["timestamp_engine"])
        self.assertIn(clips[0]["candidate_source"], {"audio_peak", "transcript_hook", "qa", "quote_value"})
        self.assertGreater(clips[0]["final_score"], 0.5)
        self.assertEqual("accepted", clips[0]["judge_status"])
        self.assertIn("score_details_json", clips[0])
        self.assertGreaterEqual(clips[0]["start"], 8)
        self.assertLessEqual(clips[0]["end"], 60.5)

    def test_v2_pipeline_uses_deterministic_fill_when_no_judge_is_configured(self):
        from services.clip_director.selection import select_dynamic_clips

        clips = select_dynamic_clips(
            [
                {"start": 0, "end": 35, "text": "Here is a surprising hook with a clear payoff."},
                {"start": 35.2, "end": 70, "text": "The answer explains the fix and why it matters."},
            ],
            duration=100,
            audio_peaks=[],
            llm_selector=None,
            allow_fallback=False,
            use_v2=True,
        )

        self.assertTrue(clips)
        self.assertTrue(all(clip["timestamp_engine"] == "v2" for clip in clips))
        self.assertTrue(any(clip["judge_status"] in {"deterministic_fill", "deterministic_rescue"} for clip in clips))

    def test_v2_judge_rejects_fresh_timestamps_but_accepts_small_adjustments(self):
        from services.clip_director.judge import apply_llm_judgement

        candidates = [{
            "candidate_id": "c1",
            "start": 10.0,
            "end": 50.0,
            "duration": 40.0,
            "text": "Here is a strong hook and payoff.",
            "final_score": 0.8,
            "candidate_source": "transcript_hook",
        }]

        accepted = apply_llm_judgement(
            candidates,
            [{"candidate_id": "c1", "start": 11.0, "end": 49.0, "reason": "Cleaner cut."}],
            max_adjustment=1.5,
        )
        rejected = apply_llm_judgement(
            candidates,
            [{"candidate_id": "c1", "start": 20.0, "end": 70.0, "reason": "Invented a new moment."}],
            max_adjustment=1.5,
        )

        self.assertEqual(1, len(accepted))
        self.assertEqual(11.0, accepted[0]["start"])
        self.assertEqual("accepted", accepted[0]["judge_status"])
        self.assertEqual([], rejected)

    def test_v2_score_formula_rewards_payoff_and_penalizes_mid_sentence(self):
        from services.clip_director.scoring import score_candidate

        candidate = {
            "candidate_id": "c1",
            "start": 10,
            "end": 48,
            "duration": 38,
            "text": "Why does retention drop? Here is the mistake, the fix, and the payoff.",
            "candidate_source": "qa",
            "has_audio_peak": True,
            "audio_peak_energy": 0.9,
            "mid_sentence_start": False,
            "mid_sentence_end": False,
        }
        weak = {
            **candidate,
            "candidate_id": "c2",
            "text": "and then we were talking for a while without a conclusion",
            "has_audio_peak": False,
            "mid_sentence_start": True,
            "mid_sentence_end": True,
        }

        strong_score = score_candidate(candidate)["final_score"]
        weak_score = score_candidate(weak)["final_score"]

        self.assertGreater(strong_score, weak_score)
        self.assertGreater(strong_score, 0.6)

    def test_v2_selection_cap_allows_up_to_thirty_qualified_clips(self):
        from services.clip_director.selection import constraints_for_mode

        self.assertEqual(30, constraints_for_mode("shorts")["safety_max_clips"])

    def test_v2_exports_all_qualified_deterministic_clips_instead_of_duration_target(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = []
        peaks = []
        for idx, start in enumerate([0, 90, 180, 270]):
            segments.extend([
                {"start": start, "end": start + 10, "text": f"Why does mistake {idx} hurt retention?"},
                {"start": start + 10.2, "end": start + 35, "text": "Because the hook hides the answer and viewers leave."},
                {"start": start + 35.2, "end": start + 58, "text": "Here is the fix, result, and payoff that makes it work."},
            ])
            peaks.append({"start": start + 18, "end": start + 24, "peak_time": start + 21, "energy": 0.95})

        clips = select_dynamic_clips(
            segments,
            duration=420,
            audio_peaks=peaks,
            llm_selector=None,
            allow_fallback=False,
            use_v2=True,
        )

        self.assertEqual(4, len(clips))
        self.assertTrue(all(clip["final_score"] >= 0.45 for clip in clips))
        self.assertTrue(all(clip["judge_status"] == "deterministic_fill" for clip in clips))

    def test_v2_comedy_scoring_prefers_laughter_applause_and_hinglish_jokes(self):
        from services.clip_director.scoring import score_candidate

        comedy = {
            "candidate_id": "funny",
            "candidate_source": "comedy",
            "start": 100,
            "end": 150,
            "duration": 50,
            "text": "Samay roast karta hai aur audience laughing applause ke saath joke ka punchline hit hota hai.",
            "has_audio_peak": True,
            "audio_peak_energy": 0.95,
        }
        generic = {
            "candidate_id": "generic",
            "candidate_source": "qa",
            "start": 200,
            "end": 250,
            "duration": 50,
            "text": "What is the truth foreign foreign",
            "has_audio_peak": False,
            "audio_peak_energy": 0.0,
        }

        self.assertGreater(score_candidate(comedy)["final_score"], 0.65)
        self.assertGreater(score_candidate(comedy)["final_score"], score_candidate(generic)["final_score"])

    def test_v2_rejects_low_score_judged_clips_and_fills_minimum_from_strong_comedy_candidates(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = []
        peaks = []
        for idx in range(10):
            start = 60 + idx * 210
            segments.extend([
                {"start": start, "end": start + 12, "text": f"Setup for joke {idx} starts with Samay roasting the panel."},
                {"start": start + 12.2, "end": start + 32, "text": "The punchline lands and audience laughing applause gets very loud."},
                {"start": start + 32.2, "end": start + 55, "text": "Everyone reacts to the funny moment and the payoff keeps going."},
            ])
            peaks.append({"start": start + 18, "end": start + 24, "peak_time": start + 21, "energy": 0.95})

        def weak_judge(episode_map, constraints, ai_model):
            candidates = episode_map["shortlisted_candidates"]
            return [{
                "candidate_id": candidates[-1]["candidate_id"],
                "start": candidates[-1]["start"],
                "end": candidates[-1]["end"],
                "title": "Weak Choice",
                "reason": "Only chose one.",
            }]

        clips = select_dynamic_clips(
            segments,
            duration=60 * 60,
            audio_peaks=peaks,
            ai_model="groq",
            mode="shorts",
            genre_hint="funny stage show",
            llm_selector=weak_judge,
            allow_fallback=False,
            use_v2=True,
        )

        self.assertGreaterEqual(len(clips), 8)
        self.assertLessEqual(len(clips), 30)
        self.assertTrue(all(clip["final_score"] >= 0.45 for clip in clips))
        self.assertTrue(any(clip["judge_status"] == "deterministic_fill" for clip in clips))
        self.assertTrue(all(clip["candidate_source"] in {"audio_peak", "comedy", "episode_context"} for clip in clips))

    def test_v2_long_comedy_tops_up_with_rescue_when_only_few_candidates_clear_score_floor(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = []
        for idx in range(80):
            start = idx * 42
            if idx in {4, 28}:
                text = "Why does this joke work? The setup, punchline, audience laughing applause, and payoff are all clear."
            else:
                text = "foreign"
            segments.append({"start": start, "end": start + 18, "text": text})

        clips = select_dynamic_clips(
            segments,
            duration=3560,
            audio_peaks=[],
            video_title="INDIA’S GOT LATENT S2 EP1",
            genre_hint="funny stage show",
            llm_selector=None,
            allow_fallback=False,
            use_v2=True,
        )

        self.assertGreaterEqual(len(clips), 20)
        self.assertLessEqual(len(clips), 30)
        self.assertTrue(any(clip["final_score"] >= 0.45 for clip in clips))
        self.assertTrue(any(clip["final_score"] < 0.45 for clip in clips))
        self.assertTrue(any(clip["judge_status"] == "deterministic_rescue" for clip in clips))

    def test_v2_judge_is_called_again_when_first_batch_returns_too_few_clips(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = []
        peaks = []
        for idx in range(16):
            start = 30 + idx * 180
            segments.extend([
                {"start": start, "end": start + 10, "text": f"Funny setup {idx} with comedy roast."},
                {"start": start + 10.1, "end": start + 35, "text": "Punchline and audience laughing applause make this a strong funny clip."},
                {"start": start + 35.2, "end": start + 58, "text": "Judges react and the payoff continues."},
            ])
            peaks.append({"start": start + 14, "end": start + 20, "peak_time": start + 17, "energy": 0.9})
        calls = []

        def sparse_judge(episode_map, constraints, ai_model):
            calls.append([c["candidate_id"] for c in episode_map["shortlisted_candidates"]])
            if len(calls) == 1:
                return []
            candidate = episode_map["shortlisted_candidates"][0]
            return [{
                "candidate_id": candidate["candidate_id"],
                "start": candidate["start"],
                "end": candidate["end"],
                "reason": "Second batch accepted.",
            }]

        clips = select_dynamic_clips(
            segments,
            duration=50 * 60,
            audio_peaks=peaks,
            genre_hint="comedy",
            llm_selector=sparse_judge,
            allow_fallback=False,
            use_v2=True,
        )

        self.assertGreaterEqual(len(calls), 2)
        self.assertGreaterEqual(len(clips), 6)

    def test_v2_long_comedy_selection_spreads_clips_across_video(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = []
        peaks = []
        for idx, start in enumerate([60, 180, 360, 900, 1500, 2100, 2700, 3300]):
            segments.extend([
                {"start": start, "end": start + 10, "text": f"Comedy setup {idx} with Samay roast."},
                {"start": start + 10.1, "end": start + 33, "text": "Audience laughing applause after the punchline makes this very funny."},
                {"start": start + 33.2, "end": start + 55, "text": "Judges react and the payoff continues."},
            ])
            peaks.append({"start": start + 13, "end": start + 20, "peak_time": start + 17, "energy": 0.92})

        def accept_all(episode_map, constraints, ai_model):
            return [
                {
                    "candidate_id": candidate["candidate_id"],
                    "start": candidate["start"],
                    "end": candidate["end"],
                    "reason": "Accepted.",
                }
                for candidate in episode_map["shortlisted_candidates"]
            ]

        clips = select_dynamic_clips(
            segments,
            duration=3600,
            audio_peaks=peaks,
            genre_hint="funny stage show",
            llm_selector=accept_all,
            allow_fallback=False,
            use_v2=True,
        )

        self.assertGreaterEqual(len(clips), 8)
        self.assertTrue(any(clip["start"] >= 1800 for clip in clips))
        self.assertTrue(any(clip["start"] < 600 for clip in clips))

    def test_v2_long_comedy_rescues_sparse_transcript_when_judge_returns_nothing(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = []
        for idx in range(80):
            start = idx * 42
            text = "foreign" if idx % 3 == 0 else f"Panel banter moment {idx} continues with crowd energy"
            segments.append({"start": start, "end": start + 18, "text": text})

        clips = select_dynamic_clips(
            segments,
            duration=3560,
            audio_peaks=[],
            video_title="INDIA’S GOT LATENT S2 EP1",
            genre_hint="funny stage show",
            llm_selector=lambda *_args: [],
            allow_fallback=False,
            use_v2=True,
        )

        self.assertGreaterEqual(len(clips), 8)
        self.assertTrue(all(clip["timestamp_engine"] == "v2" for clip in clips))
        self.assertTrue(all(clip["score_details_json"] for clip in clips))
        self.assertTrue(any(clip["start"] >= 1800 for clip in clips))
        self.assertTrue(any("rescue" in clip["judge_status"] for clip in clips))

    def test_v2_long_comedy_rescues_all_foreign_transcript(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = [
            {"start": idx * 3.0, "end": idx * 3.0 + 2.2, "text": "foreign"}
            for idx in range(1165)
        ]

        clips = select_dynamic_clips(
            segments,
            duration=3560,
            audio_peaks=[],
            video_title="INDIA’S GOT LATENT S2 EP1",
            genre_hint="funny stage show",
            llm_selector=lambda *_args: [],
            allow_fallback=False,
            use_v2=True,
        )

        self.assertGreaterEqual(len(clips), 8)
        self.assertTrue(all(clip["timestamp_engine"] == "v2" for clip in clips))
        self.assertTrue(all(clip["score_details_json"] for clip in clips))
        self.assertTrue(any(clip["start"] >= 1800 for clip in clips))

    def test_short_video_complete_clip_uses_v2_metadata(self):
        clip = main.select_clips_for_video(
            [{"start": 0, "end": 57, "text": "Short complete video."}],
            duration=57,
            ai_model="groq",
            video_title="Short funny video",
        )[0]

        self.assertEqual("v2", clip["timestamp_engine"])
        self.assertEqual("complete_short", clip["candidate_source"])
        self.assertGreater(clip["final_score"], 0)
        self.assertIn("V2 timestamp engine", clip["score_details_json"])

    def test_audio_peaks_are_converted_to_candidate_moments(self):
        from services.clip_director.audio import peaks_to_moments

        segments = [
            {"start": 90, "end": 115, "text": "The contestant starts setting up the joke."},
            {"start": 115, "end": 150, "text": "The punchline lands and everyone starts laughing."},
            {"start": 150, "end": 176, "text": "The judges react and the moment pays off."},
        ]
        peaks = [{"start": 126, "end": 130, "peak_time": 128, "energy": 0.95}]

        moments = peaks_to_moments(peaks, segments, duration=240, mode="shorts")

        self.assertEqual(1, len(moments))
        self.assertLessEqual(moments[0]["start"], 90)
        self.assertGreaterEqual(moments[0]["end"], 176)
        self.assertTrue(moments[0]["has_audio_peak"])

    def test_episode_map_includes_context_for_director(self):
        from services.clip_director.episode_map import build_episode_map

        episode = build_episode_map(
            segments=[{"start": 0, "end": 12, "text": "A funny stage moment."}],
            duration=600,
            audio_peaks=[{"start": 4, "end": 7, "peak_time": 5, "energy": 0.8}],
            title="India's Got Latent Season 2 Episode 1",
            mode="highlights",
            genre_hint="funny stage show",
        )

        self.assertEqual("highlights", episode["mode"])
        self.assertEqual("funny stage show", episode["genre_hint"])
        self.assertIn("India's Got Latent", episode["title"])
        self.assertEqual(1, len(episode["audio_peaks"]))
        self.assertEqual(1, len(episode["segments"]))

    def test_use_v2_false_still_uses_v2_selector(self):
        from services.clip_director import selection

        expected = [{"timestamp_engine": "v2", "start": 0, "end": 90}]
        with mock.patch.object(selection, "select_dynamic_clips_v2", return_value=expected) as selector:
            clips = selection.select_dynamic_clips(
                [{"start": 0, "end": 90, "text": "The setup gets a big laugh."}],
                duration=120,
                audio_peaks=[],
                ai_model="gemini",
                mode="shorts",
                use_v2=False,
            )

        self.assertEqual(expected, clips)
        selector.assert_called_once()

    def test_shorts_mode_rejects_llm_clips_over_three_minutes(self):
        from services.clip_director.selection import select_dynamic_clips

        def fake_llm_selector(episode_map, constraints, ai_model):
            return [{"start": 0, "end": 240, "title": "Too Long", "reason": "Too long for Shorts."}]

        clips = select_dynamic_clips(
            [{"start": 0, "end": 240, "text": "Long stage moment."}],
            duration=300,
            audio_peaks=[],
            ai_model="gemini",
            mode="shorts",
            llm_selector=fake_llm_selector,
        )

        self.assertTrue(all(clip["duration"] <= 180 for clip in clips))

    def test_highlights_mode_allows_five_minute_clips(self):
        from services.clip_director.selection import constraints_for_mode

        constraints = constraints_for_mode("highlights")

        self.assertEqual(300, constraints["max_duration"])

    def test_dynamic_selection_without_llm_still_uses_v2_scoring(self):
        from services.clip_director.selection import select_dynamic_clips

        segments = [
            {"start": 0, "end": 45, "text": "A funny setup starts here."},
            {"start": 45, "end": 95, "text": "The audience laughs at the payoff."},
            {"start": 180, "end": 230, "text": "Another moment gets applause."},
        ]

        clips = select_dynamic_clips(
            segments,
            duration=260,
            audio_peaks=[{"start": 50, "end": 55, "peak_time": 52, "energy": 0.9}],
            ai_model="openai",
            mode="shorts",
            llm_selector=lambda *_args: [],
        )

        self.assertTrue(clips)
        self.assertTrue(all(clip.get("timestamp_engine") == "v2" for clip in clips))

    def test_main_passes_audio_title_mode_and_genre_to_dynamic_director(self):
        segments = [{"start": 0, "end": 120, "text": "A complete funny clip."}]
        clip = {
            "start": 0,
            "end": 120,
            "duration": 120,
            "text": "A complete funny clip.",
            "title": "Funny Clip",
            "virality_score": 90,
            "completion_score": 90,
            "hook_type": "story",
            "selection_reason": "Selected dynamically.",
        }

        with mock.patch.object(main.clip_selection, "select_dynamic_clips", return_value=[clip]) as selector:
            clips = main.select_clips_for_video(
                segments,
                duration=300,
                ai_model="gemini",
                audio_path="uploads/unit_audio.mp3",
                video_title="India's Got Latent Season 2 Episode 1",
                preset={"clip_output_mode": "highlights", "genre_hint": "funny stage show"},
            )

        self.assertEqual([clip], clips)
        self.assertEqual("uploads/unit_audio.mp3", selector.call_args.kwargs["audio_path"])
        self.assertEqual("India's Got Latent Season 2 Episode 1", selector.call_args.kwargs["video_title"])
        self.assertEqual("highlights", selector.call_args.kwargs["mode"])
        self.assertEqual("funny stage show", selector.call_args.kwargs["genre_hint"])
        self.assertFalse(selector.call_args.kwargs["allow_fallback"])

    def test_processing_stores_v2_timestamp_metadata(self):
        source_path = main.UPLOADS_DIR / "unit_v2_metadata.mp4"
        source_path.write_text("source")
        main.db_write(
            "INSERT INTO videos (youtube_video_id, title, source_path, status) VALUES (?,?,?,'waiting')",
            ("unit_v2_metadata", "V2 metadata", str(source_path)),
        )
        video_id = main.db_read(
            "SELECT id FROM videos WHERE youtube_video_id=?",
            ("unit_v2_metadata",),
        )[0]["id"]
        self.addCleanup(lambda: main.db_write("DELETE FROM shorts WHERE video_id=?", (video_id,)))
        self.addCleanup(lambda: main.db_write("DELETE FROM videos WHERE id=?", (video_id,)))
        self.addCleanup(lambda: source_path.unlink(missing_ok=True))

        clip = {
            "start": 0,
            "end": 35,
            "duration": 35,
            "text": "A strong hook with payoff.",
            "title": "V2 Clip",
            "virality_score": 88,
            "completion_score": 82,
            "hook_type": "qa",
            "selection_reason": "Selected by V2.",
            "timestamp_engine": "v2",
            "candidate_source": "qa",
            "final_score": 0.86,
            "score_details_json": "{\"features\":{\"hook\":1.0}}",
            "judge_status": "accepted",
        }
        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=80),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_with_whisper_local", return_value=[clip]),
            mock.patch.object(main, "select_clips_for_video", return_value=[clip]),
            mock.patch.object(main, "export_short_clip", return_value=True),
        ):
            main._process_video_sync(video_id, str(source_path), captions_enabled=False)

        short = main.db_read(
            "SELECT timestamp_engine, candidate_source, final_score, score_details_json, judge_status "
            "FROM shorts WHERE video_id=?",
            (video_id,),
        )[0]
        self.assertEqual("v2", short["timestamp_engine"])
        self.assertEqual("qa", short["candidate_source"])
        self.assertAlmostEqual(0.86, short["final_score"])
        self.assertIn("\"hook\"", short["score_details_json"])
        self.assertEqual("accepted", short["judge_status"])

    def test_main_does_not_fall_back_to_legacy_selector_when_v2_returns_no_clips(self):
        with (
            mock.patch.object(main.clip_selection, "select_dynamic_clips", return_value=[]) as dynamic_selector,
            mock.patch.object(main.shorts_director, "select_dynamic_clips") as legacy_dynamic,
            mock.patch.object(main.shorts_director, "select_director_clips") as legacy_selector,
        ):
            clips = main.select_clips_for_video(
                [{"start": 0, "end": 120, "text": "Comedy transcript."}],
                duration=3600,
                ai_model="groq",
            )

        self.assertEqual([], clips)
        dynamic_selector.assert_called_once()
        legacy_dynamic.assert_not_called()
        legacy_selector.assert_not_called()

    def test_complete_short_clip_keeps_source_as_one_clip(self):
        segments = [
            {"start": 0, "end": 12, "text": "Here is the hook."},
            {"start": 12.5, "end": 58, "text": "Here is the payoff for the full short."},
        ]

        clips = main.complete_short_clip(segments, duration=58)

        self.assertEqual(1, len(clips))
        self.assertEqual(0, clips[0]["start"])
        self.assertEqual(58, clips[0]["end"])
        self.assertEqual("Complete short", clips[0]["title"])
        self.assertEqual("complete_short", clips[0]["hook_type"])
        self.assertEqual("v2", clips[0]["timestamp_engine"])
        self.assertIn("Here is the hook.", clips[0]["text"])
        self.assertIn("already short", clips[0]["selection_reason"])

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

    def test_legacy_director_clip_selector_is_removed(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy clip selection has been removed"):
            shorts_director.select_director_clips(
                [{"start": 0, "end": 58, "text": "A complete moment."}],
                duration=58,
                ai_model="openai",
            )

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

    def test_legacy_fallback_clips_are_removed(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy clip fallback has been removed"):
            main.fallback_clips(75)


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

    def test_cleanup_cancelled_video_keeps_existing_shorts_and_resets_status(self):
        short_id = self._create_short("unit_cancel_short.mp4")
        (main.OUTPUTS_DIR / "unit_cancel_short.srt").write_text("subtitle")

        main.cleanup_cancelled_video(self.video_id, source_started_as_download=False)

        video = main.db_read("SELECT status, steps_json FROM videos WHERE id=?", (self.video_id,))[0]
        self.assertEqual("waiting", video["status"])
        self.assertIn("Cancelled", video["steps_json"])
        self.assertEqual([short_id], [
            row["id"] for row in main.db_read("SELECT id FROM shorts WHERE video_id=?", (self.video_id,))
        ])
        self.assertTrue((main.OUTPUTS_DIR / "unit_cancel_short.mp4").exists())
        self.assertTrue((main.OUTPUTS_DIR / "unit_cancel_short.srt").exists())

    def test_cleanup_cancelled_video_removes_partial_output_without_database_row(self):
        partial = main.OUTPUTS_DIR / "test_cancel_video_short_99.mp4"
        partial.write_text("partial")

        main.cleanup_cancelled_video(self.video_id, source_started_as_download=False)

        self.assertFalse(partial.exists())

    def test_cleanup_cancelled_download_keeps_downloaded_source_when_present(self):
        main.cleanup_cancelled_video(self.video_id, source_started_as_download=True)

        video = main.db_read("SELECT status, source_path FROM videos WHERE id=?", (self.video_id,))[0]
        self.assertEqual("waiting", video["status"])
        self.assertEqual(str(self.source_path), video["source_path"])
        self.assertTrue(self.source_path.exists())

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

    def test_delete_video_record_removes_source_shorts_and_video_row(self):
        self._create_short("unit_cancel_short.mp4")
        main.db_write("UPDATE videos SET status='completed' WHERE id=?", (self.video_id,))

        deleted = main.delete_video_record(self.video_id)

        self.assertTrue(deleted)
        self.assertFalse(self.source_path.exists())
        self.assertEqual([], main.db_read("SELECT id FROM shorts WHERE video_id=?", (self.video_id,)))
        self.assertEqual([], main.db_read("SELECT id FROM videos WHERE id=?", (self.video_id,)))
        self.video_id = None

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

    def test_processing_fails_instead_of_fallback_when_ai_selects_no_clips(self):
        segment = {"start": 0, "end": 10, "text": "Transcript exists."}

        with (
            mock.patch.object(main, "missing_tools_message", return_value=""),
            mock.patch.object(main, "is_valid_video", return_value=True),
            mock.patch.object(main, "get_video_duration", return_value=120),
            mock.patch.object(main, "extract_audio", return_value=True),
            mock.patch.object(main, "transcribe_with_whisper_local", return_value=[segment]),
            mock.patch.object(main, "select_clips_for_video", return_value=[]),
            mock.patch.object(main, "fallback_clips") as fallback_clips,
            mock.patch.object(main, "export_short_clip") as export_short_clip,
        ):
            main._process_video_sync(self.video_id, str(self.source_path), captions_enabled=False)

        video = main.db_read("SELECT status, error_message FROM videos WHERE id=?", (self.video_id,))[0]
        self.assertEqual("failed", video["status"])
        self.assertIn("AI clip selection failed", video["error_message"])
        fallback_clips.assert_not_called()
        export_short_clip.assert_not_called()

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
        self.assertIn('aria-label="Remove video from list"', html)
        self.assertIn("deleteVideoRecord(1, this)", html)
        self.assertIn('id="captions-toggle-1"', html)
        self.assertIn("Captions", html)
        self.assertNotIn('id="dl-btn-1"', html)

    def test_dashboard_has_paste_url_download_control(self):
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
            },
        )
        html = response.body.decode()
        js = (main.STATIC_DIR / "app.js").read_text()

        self.assertIn('id="youtube-url-input"', html)
        self.assertIn("Download &amp; Generate", html)
        self.assertIn("downloadYoutubeUrl", html)
        self.assertIn("/youtube-url", js)

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

    def test_generated_short_renders_v2_timestamp_badges(self):
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
                        "timestamp_engine": "v2",
                        "candidate_source": "qa",
                        "final_score": 0.87,
                        "judge_status": "accepted",
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

        self.assertIn("Engine v2", html)
        self.assertIn("Source qa", html)
        self.assertIn("Score 87%", html)

    def test_legacy_short_does_not_render_zero_v2_score(self):
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
                    "youtube_video_id": "legacy_video",
                    "title": "Legacy video",
                    "published_at": "",
                    "thumbnail": "",
                    "status": "completed",
                    "source_path": str(main.UPLOADS_DIR / "legacy_video.mp4"),
                    "error_message": "",
                    "steps_json": '[{"name":"Score & select highlights","status":"done","detail":"legacy"}]',
                    "shorts": [{
                        "id": 7,
                        "filename": "legacy_video_short_01.mp4",
                        "duration": 30,
                        "start_time": 0,
                        "end_time": 30,
                        "title": "Legacy clip",
                        "virality_score": 100,
                        "completion_score": 100,
                        "hook_type": "question",
                        "final_score": 0.0,
                        "timestamp_engine": "",
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

        self.assertIn("Engine legacy", html)
        self.assertNotIn("Score 0%", html)
        self.assertIn("Processing Log", html)

    def test_v2_short_hides_engine_log_but_keeps_score_badges(self):
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
                    "youtube_video_id": "v2_video",
                    "title": "V2 video",
                    "published_at": "",
                    "thumbnail": "",
                    "status": "completed",
                    "source_path": str(main.UPLOADS_DIR / "v2_video.mp4"),
                    "error_message": "",
                    "steps_json": '[{"name":"Score & select highlights","status":"done","detail":"V2 selected 8 clips"}]',
                    "shorts": [{
                        "id": 7,
                        "filename": "v2_video_short_01.mp4",
                        "duration": 30,
                        "start_time": 0,
                        "end_time": 30,
                        "title": "V2 clip",
                        "timestamp_engine": "v2",
                        "candidate_source": "audio_peak",
                        "final_score": 0.82,
                        "score_details_json": "{\"features\":{\"audioReaction\":0.9},\"penalties\":{}}",
                        "judge_status": "deterministic_fill",
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

        self.assertIn("Engine v2", html)
        self.assertIn("Source audio_peak", html)
        self.assertIn("Score 82%", html)
        self.assertNotIn("V2 Engine Log", html)
        self.assertNotIn("audioReaction", html)
        self.assertIn("Processing Log", html)

    def test_live_short_card_renders_v2_timestamp_badges(self):
        js = (main.STATIC_DIR / "app.js").read_text()

        self.assertIn("timestamp_engine", js)
        self.assertIn("candidate_source", js)
        self.assertIn("final_score", js)

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

    def test_review_page_renders_v2_timestamp_badges(self):
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
                    "status": "draft",
                    "source_title": "Source video",
                    "youtube_video_id": "source123",
                    "virality_score": None,
                    "completion_score": None,
                    "latest_upload": None,
                    "timestamp_engine": "v2",
                    "candidate_source": "audio_peak",
                    "final_score": 0.91,
                    "judge_status": "accepted",
                },
                "youtube_connected": False,
            },
        )
        html = response.body.decode()

        self.assertIn("Engine v2", html)
        self.assertIn("Source audio_peak", html)
        self.assertIn("Score 91%", html)
        self.assertIn("Judge accepted", html)

    def test_review_page_renders_simple_metadata_without_caption_editor_or_engine_log(self):
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
                    "caption_text": "kaise ho",
                    "caption_provider": "deepgram",
                    "caption_model": "nova-3",
                    "caption_language": "en,hi",
                    "caption_confidence": 0.91,
                    "caption_status": "word_synced",
                    "caption_cues_json": json.dumps([
                        {"start": 0.2, "end": 1.0, "text": "kaise ho", "confidence": 0.91}
                    ]),
                    "score_details_json": "{\"features\":{\"audioReaction\":0.9}}",
                    "start_time": 0,
                    "end_time": 21,
                    "duration": 21,
                    "status": "draft",
                    "source_title": "Source video",
                    "youtube_video_id": "source123",
                    "latest_upload": None,
                },
                "youtube_connected": False,
            },
        )
        html = response.body.decode()

        self.assertIn("Captions deepgram", html)
        self.assertIn("Language en,hi", html)
        self.assertIn("Confidence 91%", html)
        self.assertIn('id="review-title"', html)
        self.assertIn('id="review-description"', html)
        self.assertNotIn("Upload Title", html)
        self.assertNotIn("Upload Description", html)
        self.assertNotIn("caption-editor", html)
        self.assertNotIn("data-caption-cue-row", html)
        self.assertNotIn("saveCaptionRows", html)
        self.assertNotIn("V2 Engine Log", html)


class VideoSourceClassificationTest(unittest.TestCase):
    def test_parse_youtube_duration(self):
        self.assertEqual(main.parse_youtube_duration("PT45S"), 45.0)
        self.assertEqual(main.parse_youtube_duration("PT1M30S"), 90.0)
        self.assertEqual(main.parse_youtube_duration("PT1H2M3S"), 3723.0)
        self.assertIsNone(main.parse_youtube_duration(""))

    def test_classify_source_video_by_duration(self):
        self.assertEqual(
            main.classify_source_video({"id": 1, "source_duration": 45}),
            "short",
        )
        self.assertEqual(
            main.classify_source_video({"id": 2, "source_duration": 120}),
            "short",
        )
        self.assertEqual(
            main.classify_source_video({"id": 3, "source_duration": 181}),
            "long",
        )

    def test_classify_source_video_by_shorts_hashtag_without_duration(self):
        self.assertEqual(
            main.classify_source_video({
                "id": 4,
                "title": "Funny moment #Shorts",
                "description": "",
            }),
            "short",
        )

    def test_dashboard_renders_long_and_short_video_tabs(self):
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
                "videos": [
                    {
                        "id": 1,
                        "youtube_video_id": "long_video",
                        "title": "Long video",
                        "published_at": "",
                        "thumbnail": "",
                        "status": "detected",
                        "source_path": "",
                        "error_message": "",
                        "steps_json": "",
                        "shorts": [],
                        "source_kind": "long",
                    },
                    {
                        "id": 2,
                        "youtube_video_id": "short_video",
                        "title": "Short video",
                        "published_at": "",
                        "thumbnail": "",
                        "status": "detected",
                        "source_path": "",
                        "error_message": "",
                        "steps_json": "",
                        "shorts": [],
                        "source_kind": "short",
                    },
                ],
                "long_videos": [{"id": 1}],
                "short_videos": [{"id": 2}],
                "channel_id": "test",
                "ai_model": "groq",
                "has_youtube_key": True,
                "has_openai_key": False,
                "has_gemini_key": False,
                "has_groq_key": True,
            },
        )
        html = response.body.decode()

        self.assertIn('id="video-tab-long"', html)
        self.assertIn('id="video-tab-short"', html)
        self.assertIn("Long Form (1)", html)
        self.assertIn("Shorts (1)", html)
        self.assertIn('data-source-kind="long"', html)
        self.assertIn('data-source-kind="short"', html)
        self.assertIn("switchVideoTab", (main.STATIC_DIR / "app.js").read_text())


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

    def test_update_short_captions_validates_and_saves_structured_rows(self):
        cues = [
            {"start": 0.2, "end": 1.0, "text": "kaise ho", "confidence": 0.92},
            {"start": 1.2, "end": 2.0, "text": "audience hassi", "confidence": 0.88},
        ]

        short = main.update_short_captions(self.short_id, cues)

        self.assertEqual("edited", short["caption_status"])
        self.assertEqual("kaise ho audience hassi", short["caption_text"])
        self.assertEqual(cues, json.loads(short["caption_cues_json"]))
        self.assertIn("kaise ho", short["caption_srt"])

    def test_update_short_captions_rejects_overlapping_rows(self):
        cues = [
            {"start": 0.2, "end": 1.0, "text": "kaise ho"},
            {"start": 0.9, "end": 2.0, "text": "audience hassi"},
        ]

        with self.assertRaises(ValueError):
            main.update_short_captions(self.short_id, cues)

    def test_regenerate_short_prefers_edited_structured_caption_rows(self):
        main.update_short_captions(self.short_id, [
            {"start": 0.2, "end": 1.0, "text": "structured caption"},
        ])

        with mock.patch.object(main, "export_short_clip", return_value=True) as export_short_clip:
            short = main.regenerate_short(self.short_id)

        self.assertEqual("studio_workflow_short_01.mp4", short["filename"])
        srt_path = export_short_clip.call_args.args[4]
        self.assertIsNotNone(srt_path)
        self.assertIn("structured caption", (main.OUTPUTS_DIR / "studio_workflow_short_01.srt").read_text())

    def test_regenerate_short_writes_unicode_srt_as_utf8(self):
        unicode_caption = "यह joke 😂 audience ko hasa deta hai."
        main.update_short_metadata(self.short_id, {"caption_text": unicode_caption})

        with mock.patch.object(main, "export_short_clip", return_value=True):
            main.regenerate_short(self.short_id)

        srt_text = (main.OUTPUTS_DIR / "studio_workflow_short_01.srt").read_text(encoding="utf-8")
        self.assertIn("😂", srt_text)
        self.assertIn("यह", srt_text)

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
