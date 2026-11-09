"""Распознавание речи. Работает локально, звук никуда не отправляется.

Движки (выбираются в config.json -> asr.engine):
  whisper — faster-whisper, ставится обычным pip, ffmpeg не нужен (по умолчанию);
  gigaam  — GigaAM от Сбера, сильнее на русском, но нужны PyTorch и ffmpeg,
            одна фраза — до 25 секунд;
  none    — голосовой ввод выключен, только клавиатура;
  test    — для проверки без модели: всегда возвращает фразу из config.json.
"""
import threading


def decode_audio_16k(path: str):
    """Звук из браузера (webm/ogg/mp4) -> 16 кГц, моно, float32.
    Свой декодер, чтобы не зависеть от изменений в разных версиях PyAV."""
    import av
    import numpy as np

    chunks = []
    with av.open(path) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for out in resampler.resample(frame) or []:
                chunks.append(out.to_ndarray().reshape(-1))
        try:
            for out in resampler.resample(None) or []:
                chunks.append(out.to_ndarray().reshape(-1))
        except Exception:
            pass
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class BaseEngine:
    name = "base"
    max_seconds = 60

    def __init__(self):
        self.ready = False
        self.loading = False
        self.error = None

    def start_loading(self):
        if self.ready or self.loading:
            return
        self.loading = True
        self.error = None

        def _run():
            try:
                self.load()
                self.ready = True
            except Exception as exc:  # показываем причину в интерфейсе
                self.error = (f"Не удалось загрузить модель распознавания ({type(exc).__name__}: {exc}). "
                              "При первом запуске нужен интернет: модель скачивается один раз. "
                              "Без интернета перенесите модель с другого компьютера (см. README).")
            finally:
                self.loading = False

        threading.Thread(target=_run, daemon=True).start()

    def load(self):
        pass

    def transcribe(self, path: str) -> str:
        raise NotImplementedError

    def info(self) -> dict:
        if self.ready:
            message = "Голосовой ввод готов"
        elif self.loading:
            message = "Загружается модель распознавания. Первый запуск может занять несколько минут — скачивается модель"
        else:
            message = self.error or "Голосовой ввод выключен"
        return {"engine": self.name, "ready": self.ready, "loading": self.loading,
                "error": self.error, "message": message, "max_seconds": self.max_seconds}


class WhisperEngine(BaseEngine):
    name = "whisper"
    max_seconds = 60

    def __init__(self, model: str, device: str, compute_type: str, prompt: str = ""):
        super().__init__()
        self.model_name, self.device, self.compute_type, self.prompt = model, device, compute_type, prompt
        self.model = None
        self._lock = threading.Lock()

    def load(self):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)

    def transcribe(self, path: str) -> str:
        with self._lock:
            audio = decode_audio_16k(path)
            if audio.size < 1600:  # меньше 0,1 с
                return ""
            segments, _ = self.model.transcribe(
                audio, language="ru", beam_size=5, vad_filter=True,
                initial_prompt=self.prompt or None, condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()

    def info(self) -> dict:
        d = super().info()
        d["model"] = self.model_name
        return d


class GigaAMEngine(BaseEngine):
    name = "gigaam"
    max_seconds = 25

    def __init__(self, model: str):
        super().__init__()
        self.model_name = model
        self.model = None
        self._lock = threading.Lock()

    def load(self):
        import gigaam
        self.model = gigaam.load_model(self.model_name)

    def transcribe(self, path: str) -> str:
        with self._lock:
            result = self.model.transcribe(path)
        return (result if isinstance(result, str) else getattr(result, "text", str(result))).strip()

    def info(self) -> dict:
        d = super().info()
        d["model"] = self.model_name
        return d


class DisabledEngine(BaseEngine):
    name = "none"

    def info(self) -> dict:
        d = super().info()
        d["message"] = "Голосовой ввод выключен в config.json — вводите текст с клавиатуры"
        return d


class TestEngine(BaseEngine):
    name = "test"

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def transcribe(self, path: str) -> str:
        return self.text


def create_engine(cfg: dict, prompt: str = "") -> BaseEngine:
    engine = (cfg.get("engine") or "whisper").lower()
    if engine == "whisper":
        return WhisperEngine(cfg.get("whisper_model", "small"), cfg.get("whisper_device", "cpu"),
                             cfg.get("whisper_compute_type", "int8"), prompt)
    if engine == "gigaam":
        return GigaAMEngine(cfg.get("gigaam_model", "v3_e2e_rnnt"))
    if engine == "test":
        return TestEngine(cfg.get("test_text", "Тестовая запись."))
    return DisabledEngine()
