from typing import Literal, TypedDict


ClipMode = Literal["shorts", "highlights"]


class AudioPeak(TypedDict, total=False):
    start: float
    end: float
    peak_time: float
    energy: float
    prominence: float


class TranscriptSegment(TypedDict, total=False):
    start: float
    end: float
    text: str


class CandidateMoment(TypedDict, total=False):
    start: float
    end: float
    duration: float
    text: str
    has_audio_peak: bool
    audio_peak_energy: float
    peak_time: float


class EpisodeMap(TypedDict, total=False):
    title: str
    duration: float
    mode: ClipMode
    genre_hint: str
    detected_genre: str
    segments: list[TranscriptSegment]
    audio_peaks: list[AudioPeak]
    candidate_moments: list[CandidateMoment]


class ClipConstraints(TypedDict):
    mode: ClipMode
    min_duration: float
    max_duration: float
    safety_max_clips: int


class DirectorSelection(TypedDict, total=False):
    start: float
    end: float
    title: str
    description: str
    upload_title: str
    upload_description: str
    hook: str
    context: str
    value: str
    payoff: str
    virality_score: int
    completion_score: int
    hook_type: str
    reason: str
    selection_reason: str
