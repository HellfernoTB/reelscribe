"""faster-whisper wrapper.

The model is expensive to load, so a ``Transcriber`` instance loads it once
(lazily) and reuses it for every clip and every task in the process.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TranscriptResult:
    text: str
    rows: list[tuple[float, float, str]] = field(default_factory=list)
    language: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.text


class Transcriber:
    def __init__(
        self,
        model_size: str = "medium",
        device: str = "auto",
        language: str | None = None,
        compute_type: str = "int8",
        model_factory=None,
    ):
        self.model_size = model_size
        self.device = device
        self.language = language
        self.compute_type = compute_type
        self._model_factory = model_factory
        self._model = None
        self._downgraded = False

    @property
    def model(self):
        if self._model is None:
            if self._model_factory is not None:
                self._model = self._model_factory(self.model_size, self.device)
            else:  # pragma: no cover - requires the real dependency
                self._model = self._load_real_model()
        return self._model

    def _load_real_model(self):  # pragma: no cover - requires the real dependency
        from faster_whisper import WhisperModel

        device = "cpu" if (self.device == "auto" and self._downgraded) else self.device
        compute = "int8" if device == "cpu" else self.compute_type
        return WhisperModel(self.model_size, device=device, compute_type=compute)

    def run(self, audio_path, task: str = "transcribe") -> TranscriptResult:
        try:
            return self._run(audio_path, task)
        except Exception:  # pragma: no cover - requires the real dependency
            # A broken/absent CUDA toolchain only surfaces at inference time.
            # Fall back to CPU once, then let any further error propagate.
            if self._model_factory is None and not self._downgraded and self.device in ("auto", "cuda"):
                self._downgraded = True
                self.device = "cpu"
                self._model = None
                return self._run(audio_path, task)
            raise

    def _run(self, audio_path, task: str) -> TranscriptResult:
        segments, info = self.model.transcribe(
            str(audio_path),
            vad_filter=True,
            language=self.language,
            task=task,
        )
        rows: list[tuple[float, float, str]] = []
        parts: list[str] = []
        for seg in segments:
            text = seg.text.strip()
            rows.append((seg.start, seg.end, text))
            parts.append(text)
        return TranscriptResult(
            text=" ".join(parts).strip(),
            rows=rows,
            language=getattr(info, "language", None),
        )

    def translate_to_english(self, audio_path) -> str | None:
        """Second pass rendering the audio as English. Returns text or None."""
        return self.run(audio_path, task="translate").text or None
