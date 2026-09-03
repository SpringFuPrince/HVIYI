from __future__ import annotations

import unittest
from typing import Any

import numpy as np

from app.asr.engine import FunAsrStreamingSession, merge_stream_text


class StreamingTextMergeTest(unittest.TestCase):
    def test_merges_incremental_chinese_fragments(self) -> None:
        self.assertEqual(merge_stream_text("大家", "好"), "大家好")
        self.assertEqual(merge_stream_text("中华人民共和国", "共和国万岁"), "中华人民共和国万岁")

    def test_accepts_cumulative_results_and_spaces_english_words(self) -> None:
        self.assertEqual(merge_stream_text("大家", "大家好"), "大家好")
        self.assertEqual(merge_stream_text("hello", "world"), "hello world")


class _FakeStreamingEngine:
    chunk_size = [5, 10, 5]

    def __init__(self) -> None:
        self.cache_ids: list[int] = []
        self.final_flags: list[bool] = []

    async def generate(
        self,
        audio: np.ndarray,
        cache: dict[str, Any],
        is_final: bool,
    ) -> str:
        self.cache_ids.append(id(cache))
        self.final_flags.append(is_final)
        cache["calls"] = int(cache.get("calls", 0)) + 1
        if not audio.size:
            return ""
        return f"片段{cache['calls']}"


class StreamingAsrSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_pcm_uses_one_cache_and_finishes_residual_audio(self) -> None:
        engine = _FakeStreamingEngine()
        session = FunAsrStreamingSession(engine, sample_rate=16_000)  # type: ignore[arg-type]
        first_half = np.zeros(4_800, dtype="<i2").tobytes()
        second_half = np.zeros(4_800, dtype="<i2").tobytes()

        self.assertEqual(await session.push(first_half), [])
        emitted = await session.push(second_half)
        self.assertEqual([item.text for item in emitted], ["片段1"])
        self.assertEqual((emitted[0].start_ms, emitted[0].end_ms), (0, 600))

        await session.push(np.zeros(1_600, dtype="<i2").tobytes())
        final = await session.finish()
        self.assertEqual([item.text for item in final], ["片段2"])
        self.assertEqual((final[0].start_ms, final[0].end_ms), (600, 700))
        self.assertEqual(len(set(engine.cache_ids)), 1)
        self.assertEqual(engine.final_flags, [False, True])

    async def test_finish_signals_model_for_exact_stride(self) -> None:
        engine = _FakeStreamingEngine()
        session = FunAsrStreamingSession(engine, sample_rate=16_000)  # type: ignore[arg-type]
        emitted = await session.push(np.zeros(9_600, dtype="<i2").tobytes())
        self.assertEqual(len(emitted), 1)
        self.assertEqual(await session.finish(), [])
        self.assertEqual(engine.final_flags, [False, True])
        self.assertEqual(len(set(engine.cache_ids)), 1)

    async def test_silence_endpoint_resets_model_cache_for_next_utterance(self) -> None:
        engine = _FakeStreamingEngine()
        session = FunAsrStreamingSession(engine, sample_rate=16_000)  # type: ignore[arg-type]

        first = await session.push(np.zeros(9_600, dtype="<i2").tobytes())
        self.assertEqual([item.text for item in first], ["片段1"])
        self.assertEqual(await session.finalize_utterance(), [])

        second = await session.push(np.zeros(9_600, dtype="<i2").tobytes())
        self.assertEqual([item.text for item in second], ["片段1"])
        self.assertEqual(engine.final_flags, [False, True, False])


if __name__ == "__main__":
    unittest.main()
