"""T1 (session-topic-folder-20261016): the pure in-session topic index."""
from __future__ import annotations


from opencohost.core.context.session_topics import (
    SessionTopicIndex,
    compact_user_text,
    fragment_key,
)

A = [1.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0]
NEAR_A = [0.9, 0.1, 0.0]


def _idx(**kw) -> SessionTopicIndex:
    return SessionTopicIndex(**kw)


def test_new_topic_then_nearest_centroid_assignment():
    idx = _idx()
    t1 = idx.add_fragment(A, "the vulkan renderer is slow again today", "Sounds painful", 1)
    t2 = idx.add_fragment(NEAR_A, "vulkan renderer stutters on the main scene", "Profile it", 2)
    t3 = idx.add_fragment(B, "what should we cook for dinner tonight friend", "Pasta", 3)
    assert t1 == t2
    assert t3 != t1
    assert len(idx.snapshot()) == 2


def test_trivial_turn_is_skipped():
    idx = _idx(min_words=4)
    assert idx.add_fragment(A, "ok thanks", "np", 1) is None
    assert idx.snapshot() == ()


def test_degenerate_vectors_are_skipped():
    idx = _idx()
    assert idx.add_fragment([], "a long enough user sentence here", "k", 1) is None
    assert idx.add_fragment([0.0, 0.0, 0.0], "a long enough user sentence here", "k", 2) is None
    assert idx.add_fragment(A, "a long enough user sentence here", "k", 3) is not None
    # dimension mismatch against existing topics never raises and opens its own topic
    assert idx.add_fragment([1.0, 0.0], "another long enough user sentence", "k", 4) is not None


def test_query_hits_best_topic_and_excludes_window_fragments():
    idx = _idx()
    idx.add_fragment(A, "the vulkan renderer is slow again today", "Sounds painful", 1)
    idx.add_fragment(A, "vulkan renderer stutters on the main scene", "Profile it", 2)
    idx.add_fragment(B, "what should we cook for dinner tonight friend", "Pasta", 3)
    k2 = fragment_key("vulkan renderer stutters on the main scene")
    hit = idx.query(NEAR_A, exclude_keys={k2})
    assert hit is not None
    assert [f.turn_seq for f in hit.fragments] == [1]
    assert hit.score >= 0.6


def test_query_misses_when_all_fragments_still_in_window():
    idx = _idx()
    idx.add_fragment(A, "the vulkan renderer is slow again today", "Sounds painful", 1)
    k = fragment_key("the vulkan renderer is slow again today")
    assert idx.query(A, exclude_keys={k}) is None


def test_query_misses_below_threshold_and_on_empty_or_bad_vec():
    idx = _idx()
    assert idx.query(A) is None
    idx.add_fragment(A, "the vulkan renderer is slow again today", "Sounds painful", 1)
    assert idx.query(B) is None
    assert idx.query(None) is None
    assert idx.query([]) is None


def test_query_picks_the_next_topic_when_best_has_nothing_evicted():
    idx = _idx(assign_threshold=0.95, retrieve_threshold=0.5)
    idx.add_fragment(A, "the vulkan renderer is slow again today", "x", 1)
    idx.add_fragment([0.7, 0.7, 0.0], "mixed topic about renderer and food", "y", 2)
    k1 = fragment_key("the vulkan renderer is slow again today")
    hit = idx.query(A, exclude_keys={k1})
    assert hit is not None and [f.turn_seq for f in hit.fragments] == [2]


def test_fragments_are_bounded_newest_kept_and_returned_newest_first():
    idx = _idx(max_fragments=3)
    for i in range(1, 7):
        idx.add_fragment(A, f"vulkan renderer question number {i} again", f"k{i}", i)
    (topic,) = idx.snapshot()
    assert [f["turn_seq"] for f in topic["fragments"]] == [4, 5, 6]
    hit = idx.query(A, max_return=2)
    assert [f.turn_seq for f in hit.fragments] == [6, 5]


def test_topics_are_bounded_and_oldest_touched_is_dropped():
    idx = _idx(max_topics=3, assign_threshold=0.99)
    for i in range(5):
        v = [0.0] * 5
        v[i] = 1.0
        idx.add_fragment(v, f"unique subject number {i} for this topic", "k", i + 1)
    snap = idx.snapshot()
    assert len(snap) == 3
    assert sorted(t["last_seq"] for t in snap) == [3, 4, 5]


def test_compact_user_text_caps_words_and_chars():
    assert compact_user_text("one two three four five six seven eight nine ten") == "one two three four five six seven eight"
    long = "x" * 300
    assert len(compact_user_text(long)) <= 100


def test_fragment_text_is_compacted_in_snapshot():
    idx = _idx()
    idx.add_fragment(A, "one two three four five six seven eight nine ten", "Kira reply", 1)
    (topic,) = idx.snapshot()
    assert topic["fragments"][0]["user"] == "one two three four five six seven eight"
    assert topic["fragments"][0]["kira"] == "Kira reply"


def test_reset_clears_everything():
    idx = _idx()
    idx.add_fragment(A, "the vulkan renderer is slow again today", "x", 1)
    idx.reset()
    assert idx.snapshot() == ()
    assert idx.query(A) is None


def test_snapshot_is_detached_copy():
    idx = _idx()
    idx.add_fragment(A, "the vulkan renderer is slow again today", "x", 1)
    snap = idx.snapshot()
    assert isinstance(snap, tuple)
    assert isinstance(snap[0]["centroid"], tuple)
