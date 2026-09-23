"""Звук: синтез, бэкенды, фильтрация настройками (SND-01…SND-06)."""
from __future__ import annotations

import unittest
import wave
import io

from rwf.settings import Settings
from rwf.sound import (EVENTS, NullBackend, SoundManager, pick_backend,
                       synth_wav)


class TestSynth(unittest.TestCase):
    def test_wav_header_and_len(self):
        for ev in EVENTS:
            data = synth_wav(ev, 0.7)
            self.assertEqual(data[:4], b"RIFF", ev)
            with wave.open(io.BytesIO(data), "rb") as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getframerate(), 22050)
                self.assertGreater(w.getnframes(), 100)

    def test_volume_scales_samples(self):
        loud = synth_wav("fire", 1.0)
        quiet = synth_wav("fire", 0.1)
        self.assertNotEqual(loud, quiet)

    def test_unknown_event_raises(self):
        with self.assertRaises(KeyError):
            synth_wav("nope", 0.5)


class TestBackend(unittest.TestCase):
    def test_pick_null(self):
        self.assertIsInstance(pick_backend("null"), NullBackend)

    def test_manager_filters_by_settings(self):
        s = Settings(data={"sound": {"enabled": True,
                                      "events": {"fire": False}}})
        m = SoundManager(settings=s, backend=NullBackend())
        m.start()
        m.play("fire")          # выключено событием
        m.play("ui")
        s.set("sound.enabled", False)
        m.play("ui")            # выключено мастером
        import time
        time.sleep(0.4)
        m.stop()
        self.assertEqual(m.played, ["ui"])

    def test_manager_queue_overflow_safe(self):
        m = SoundManager(settings=None, backend=NullBackend())
        for _ in range(1000):
            m.play("ui")        # переполнение не должно бросать

    def test_bus_hook(self):
        m = SoundManager(settings=None, backend=NullBackend())
        m.start()
        m.bus_hook("missile.hit")
        m.bus_hook("unknown.topic")
        import time
        time.sleep(0.3)
        m.stop()
        self.assertEqual(m.played, ["explosion"])


if __name__ == "__main__":
    unittest.main()
