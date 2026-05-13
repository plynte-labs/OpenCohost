"""Tests for avatar runtime state bridge."""

from __future__ import annotations

import threading
import time

import pytest

from avatar.avatar_state import AvatarState, AvatarStateBridge


class TestAvatarStateEnum:
    def test_all_expected_states_exist(self):
        assert AvatarState.IDLE.value == "idle"
        assert AvatarState.LISTENING.value == "listening"
        assert AvatarState.THINKING.value == "thinking"
        assert AvatarState.SPEAKING.value == "speaking"
        assert AvatarState.SPEAKING_ALT.value == "speaking_alt"
        assert AvatarState.SLEEPING.value == "sleeping"
        assert AvatarState.ANGRY.value == "angry"
        assert AvatarState.ERROR.value == "error"


class TestAvatarStateBridge:
    def test_initial_state_is_idle(self):
        bridge = AvatarStateBridge()
        assert bridge.get_state() == AvatarState.IDLE

    def test_set_state_changes_state(self):
        bridge = AvatarStateBridge()
        bridge.set_state(AvatarState.LISTENING)
        assert bridge.get_state() == AvatarState.LISTENING

    def test_duplicate_state_does_nothing(self):
        bridge = AvatarStateBridge()
        calls = []
        bridge.subscribe(lambda s: calls.append(s))
        bridge.set_state(AvatarState.IDLE)  # already idle
        assert len(calls) == 0

    def test_subscribe_receives_state_changes(self):
        bridge = AvatarStateBridge()
        received = []
        bridge.subscribe(lambda s: received.append(s))
        bridge.set_state(AvatarState.THINKING)
        assert received == [AvatarState.THINKING]

    def test_unsubscribe_stops_receiving(self):
        bridge = AvatarStateBridge()
        received = []
        sid = bridge.subscribe(lambda s: received.append(s))
        bridge.unsubscribe(sid)
        bridge.set_state(AvatarState.SPEAKING)
        assert len(received) == 0

    def test_multiple_subscribers_all_notified(self):
        bridge = AvatarStateBridge()
        r1, r2 = [], []
        bridge.subscribe(lambda s: r1.append(s))
        bridge.subscribe(lambda s: r2.append(s))
        bridge.set_state(AvatarState.ERROR)
        assert r1 == [AvatarState.ERROR]
        assert r2 == [AvatarState.ERROR]

    def test_speech_text(self):
        bridge = AvatarStateBridge()
        assert bridge.get_speech_text() == ""
        bridge.set_speech_text("Hola chat")
        assert bridge.get_speech_text() == "Hola chat"

    def test_from_pipeline_state_mapping(self):
        assert AvatarStateBridge.from_pipeline_state("listening") == AvatarState.LISTENING
        assert AvatarStateBridge.from_pipeline_state("processing") == AvatarState.THINKING
        assert AvatarStateBridge.from_pipeline_state("speaking") == AvatarState.SPEAKING
        assert AvatarStateBridge.from_pipeline_state("playing") == AvatarState.SPEAKING
        assert AvatarStateBridge.from_pipeline_state("idle") == AvatarState.IDLE
        assert AvatarStateBridge.from_pipeline_state("error") == AvatarState.ERROR
        assert AvatarStateBridge.from_pipeline_state("downloading") == AvatarState.IDLE
        assert AvatarStateBridge.from_pipeline_state("init") == AvatarState.IDLE

    def test_from_pipeline_state_unknown_defaults_to_idle(self):
        assert AvatarStateBridge.from_pipeline_state("unknown") == AvatarState.IDLE

    def test_thread_safety_rapid_updates(self):
        """Verify bridge handles rapid concurrent state changes without crashing."""
        bridge = AvatarStateBridge()
        errors = []

        def worker():
            try:
                for _ in range(50):
                    bridge.set_state(AvatarState.LISTENING)
                    bridge.set_state(AvatarState.THINKING)
                    bridge.set_state(AvatarState.SPEAKING)
                    bridge.set_state(AvatarState.IDLE)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
