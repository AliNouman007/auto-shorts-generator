from pathlib import Path

from .types import AudioPeak, CandidateMoment, ClipMode, TranscriptSegment


def detect_audio_energy_peaks(
    audio_path: str,
    hop_length: int = 512,
    n_fft: int = 2048,
    prominence: float = 0.5,
    min_distance: float = 2.0,
) -> list[AudioPeak]:
    import librosa
    from scipy.signal import find_peaks

    y, sample_rate = librosa.load(audio_path, sr=22050)
    rms = librosa.feature.rms(y=y, hop_length=hop_length, frame_length=n_fft)[0]
    if len(rms) == 0:
        return []
    times = librosa.times_like(rms, sr=sample_rate, hop_length=hop_length)
    normalized = (rms - rms.min()) / (rms.max() - rms.min() + 1e-8)
    peaks, properties = find_peaks(
        normalized,
        prominence=prominence,
        distance=int(min_distance * sample_rate / hop_length),
    )
    return [
        {
            "peak_time": float(times[peak_idx]),
            "start": max(0.0, float(times[peak_idx]) - 1.0),
            "end": float(times[peak_idx]) + 2.0,
            "energy": float(normalized[peak_idx]),
            "prominence": float(properties["prominences"][idx]),
        }
        for idx, peak_idx in enumerate(peaks)
    ]


def merge_overlapping_peaks(peaks: list[AudioPeak], merge_threshold: float = 5.0) -> list[AudioPeak]:
    if not peaks:
        return []
    merged = [dict(peak) for peak in sorted(peaks, key=lambda item: float(item.get("start", 0)))]
    kept = [merged[0]]
    for peak in merged[1:]:
        last = kept[-1]
        if float(peak.get("start", 0)) - float(last.get("end", 0)) <= merge_threshold:
            last["end"] = max(float(last.get("end", 0)), float(peak.get("end", 0)))
            last["energy"] = max(float(last.get("energy", 0)), float(peak.get("energy", 0)))
            last["prominence"] = max(float(last.get("prominence", 0)), float(peak.get("prominence", 0)))
        else:
            kept.append(peak)
    return kept


def analyze_audio_peaks(audio_path: str | None) -> list[AudioPeak]:
    if not audio_path or not Path(audio_path).exists():
        return []
    try:
        return merge_overlapping_peaks(detect_audio_energy_peaks(audio_path))
    except Exception:
        return []


def peaks_to_moments(
    peaks: list[AudioPeak],
    segments: list[TranscriptSegment],
    duration: float,
    mode: ClipMode = "shorts",
) -> list[CandidateMoment]:
    max_duration = 180.0 if mode == "shorts" else 300.0
    moments: list[CandidateMoment] = []
    for peak in peaks:
        peak_start = float(peak.get("start", peak.get("peak_time", 0)))
        peak_end = float(peak.get("end", peak_start))
        window_start = max(0.0, peak_start - 45.0)
        window_end = min(float(duration), peak_end + 60.0)
        window_segments = [
            seg for seg in segments
            if float(seg.get("end", seg.get("start", 0))) >= window_start
            and float(seg.get("start", 0)) <= window_end
        ]
        if window_segments:
            start = float(window_segments[0].get("start", window_start))
            end = float(window_segments[-1].get("end", window_end))
            text = " ".join(str(seg.get("text", "")).strip() for seg in window_segments).strip()
        else:
            start = window_start
            end = window_end
            text = ""
        if end - start > max_duration:
            center = float(peak.get("peak_time", (peak_start + peak_end) / 2))
            start = max(0.0, center - max_duration * 0.45)
            end = min(float(duration), start + max_duration)
        moments.append({
            "start": start,
            "end": end,
            "duration": max(0.0, end - start),
            "text": text,
            "has_audio_peak": True,
            "audio_peak_energy": float(peak.get("energy", 0)),
            "peak_time": float(peak.get("peak_time", peak_start)),
        })
    return moments
