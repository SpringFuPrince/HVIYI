"""真正的流式 ASR：连续 PCM 输入，FunASR cache 跨帧保留。"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
from dotenv import load_dotenv


load_dotenv()
logger = logging.getLogger(__name__)


class AsrUnavailableError(RuntimeError):
    """流式模型未安装、未启用或推理失败。"""


@dataclass(frozen=True)
class AsrResult:
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    speaker_name: str = "本机用户"


def _parse_chunk_size(raw: str) -> list[int]:
    try:
        values = [int(value.strip()) for value in raw.split(",")]
    except ValueError as exc:
        raise AsrUnavailableError("ASR_CHUNK_SIZE必须是三个逗号分隔的整数") from exc
    if len(values) != 3 or any(value < 0 for value in values) or values[1] <= 0:
        raise AsrUnavailableError("ASR_CHUNK_SIZE格式无效，例如：5,10,5")
    return values


def merge_stream_text(current: str, fragment: str) -> str:
    """合并 FunASR 增量或累计文本，避免字幕按模型 chunk 拆成多句。"""

    current = " ".join(current.strip().split())
    fragment = " ".join(fragment.strip().split())
    if not fragment:
        return current
    if not current:
        return fragment
    if fragment.startswith(current):
        return fragment
    if current.endswith(fragment):
        return current

    overlap = 0
    for size in range(min(len(current), len(fragment)), 0, -1):
        if current[-size:] == fragment[:size]:
            overlap = size
            break
    suffix = fragment[overlap:]
    if not suffix:
        return current
    separator = " " if current[-1].isascii() and current[-1].isalnum() and suffix[0].isascii() and suffix[0].isalnum() else ""
    return f"{current}{separator}{suffix}"


class FunAsrStreamingEngine:
    """进程级 FunASR 流式模型；每条 WebSocket 拥有独立 cache。"""

    def __init__(self) -> None:
        self.model_name = os.getenv("ASR_MODEL", "paraformer-zh-streaming").strip()
        self.punc_model_name = os.getenv("ASR_PUNC_MODEL", "ct-punc").strip()
        self.device = os.getenv("ASR_DEVICE", "cpu").strip()
        self.chunk_size = _parse_chunk_size(os.getenv("ASR_CHUNK_SIZE", "5,10,5"))
        self.encoder_chunk_look_back = int(os.getenv("ASR_ENCODER_CHUNK_LOOK_BACK", "4"))
        self.decoder_chunk_look_back = int(os.getenv("ASR_DECODER_CHUNK_LOOK_BACK", "1"))
        self._model: Any | None = None
        self._punc_model: Any | None = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from funasr import AutoModel
            except ImportError as exc:
                raise AsrUnavailableError(
                    "缺少funasr依赖，请执行 uv sync 后重新启动本地服务"
                ) from exc
            try:
                self._model = AutoModel(
                    model=self.model_name,
                    device=self.device,
                    disable_update=True,
                )
                if self.punc_model_name:
                    try:
                        self._punc_model = AutoModel(
                            model=self.punc_model_name,
                            device=self.device,
                            disable_update=True,
                        )
                    except Exception as exc:
                        logger.warning("FunASR标点模型加载失败，将保留无标点文本：%s", exc)
            except Exception as exc:
                raise AsrUnavailableError(f"FunASR流式模型加载失败：{exc}") from exc
        return self._model

    def _generate(self, audio: np.ndarray, cache: dict[str, Any], is_final: bool) -> str:
        model = self._load_model()
        try:
            with self._inference_lock:
                output = model.generate(
                    input=audio,
                    cache=cache,
                    is_final=is_final,
                    chunk_size=self.chunk_size,
                    encoder_chunk_look_back=self.encoder_chunk_look_back,
                    decoder_chunk_look_back=self.decoder_chunk_look_back,
                )
        except Exception as exc:
            raise AsrUnavailableError(f"FunASR流式推理失败：{exc}") from exc
        if not output:
            return ""
        first = output[0] if isinstance(output, list) else output
        if isinstance(first, dict):
            return str(first.get("text", "")).strip()
        return str(first).strip()

    async def generate(self, audio: np.ndarray, cache: dict[str, Any], is_final: bool) -> str:
        return await asyncio.to_thread(self._generate, audio, cache, is_final)

    async def warmup(self) -> None:
        """连接 ready 前完成权重加载，避免第一帧承担冷启动延迟。"""

        await asyncio.to_thread(self._load_model)

    def _punctuate(self, text: str) -> str:
        if self._punc_model is None or not text.strip():
            return text.strip()
        try:
            with self._inference_lock:
                output = self._punc_model.generate(input=text.strip())
        except Exception as exc:
            logger.warning("FunASR标点恢复失败，将保留原文本：%s", exc)
            return text.strip()
        if not output:
            return text.strip()
        first = output[0] if isinstance(output, list) else output
        punctuated = first.get("text", "") if isinstance(first, dict) else str(first)
        return str(punctuated).strip() or text.strip()

    async def punctuate(self, text: str) -> str:
        return await asyncio.to_thread(self._punctuate, text)


class FunAsrStreamingSession:
    """单条录音流的状态；输入格式固定为单声道 PCM16 little-endian。"""

    def __init__(self, engine: FunAsrStreamingEngine, sample_rate: int) -> None:
        if sample_rate != 16_000:
            raise AsrUnavailableError("流式ASR仅接收16000Hz单声道PCM16")
        self.engine = engine
        self.sample_rate = sample_rate
        self.cache: dict[str, Any] = {}
        self.pending = np.empty(0, dtype=np.float32)
        self.processed_samples = 0
        self.closed = False
        # FunASR 官方定义：中间值 * 960 为每次送入模型的采样点数。
        self.stride_samples = engine.chunk_size[1] * 960

    async def warmup(self) -> None:
        await self.engine.warmup()

    async def punctuate(self, text: str) -> str:
        return await self.engine.punctuate(text)

    @staticmethod
    def _decode_pcm16(payload: bytes) -> np.ndarray:
        if len(payload) % 2:
            raise AsrUnavailableError("PCM16字节长度必须是2的倍数")
        return np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0

    async def push(self, payload: bytes) -> list[AsrResult]:
        if self.closed:
            raise AsrUnavailableError("流式ASR会话已经结束")
        samples = self._decode_pcm16(payload)
        if not samples.size:
            return []
        self.pending = np.concatenate((self.pending, samples))
        results: list[AsrResult] = []
        while self.pending.size >= self.stride_samples:
            chunk = self.pending[: self.stride_samples]
            self.pending = self.pending[self.stride_samples :]
            result = await self._recognize(chunk, is_final=False)
            if result is not None:
                results.append(result)
        return results

    async def finish(self) -> list[AsrResult]:
        if self.closed:
            return []
        self.closed = True
        return await self._finalize_utterance()

    async def finalize_utterance(self) -> list[AsrResult]:
        """在静音端点冲刷当前语句，并为下一句话创建新的模型 cache。"""

        if self.closed:
            return []
        return await self._finalize_utterance()

    async def _finalize_utterance(self) -> list[AsrResult]:
        chunk = self.pending
        self.pending = np.empty(0, dtype=np.float32)
        result = await self._recognize(chunk, is_final=True)
        self.cache = {}
        return [result] if result is not None else []

    async def _recognize(self, chunk: np.ndarray, is_final: bool) -> AsrResult | None:
        start_sample = self.processed_samples
        self.processed_samples += int(chunk.size)
        text = await self.engine.generate(chunk, self.cache, is_final)
        if not text:
            return None
        return AsrResult(
            text=text,
            start_ms=round(start_sample * 1000 / self.sample_rate),
            end_ms=round(self.processed_samples * 1000 / self.sample_rate),
        )


_streaming_engine: FunAsrStreamingEngine | None = None
_streaming_engine_lock = threading.Lock()


def create_streaming_asr_session(sample_rate: int = 16_000) -> FunAsrStreamingSession:
    """创建一条真正保留模型 cache 的流式识别会话。"""

    provider = os.getenv("ASR_PROVIDER", "disabled").strip().lower()
    if provider in {"", "disabled", "none"}:
        raise AsrUnavailableError(
            "流式ASR未启用；请设置ASR_PROVIDER=funasr_streaming"
        )
    if provider not in {"funasr", "funasr_streaming"}:
        raise AsrUnavailableError(f"流式模式不支持ASR_PROVIDER={provider}")

    global _streaming_engine
    if _streaming_engine is None:
        with _streaming_engine_lock:
            if _streaming_engine is None:
                _streaming_engine = FunAsrStreamingEngine()
    return FunAsrStreamingSession(_streaming_engine, sample_rate)
