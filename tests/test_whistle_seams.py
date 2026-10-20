"""Regressions for observed overlapping-window ownership failures."""
import json
import wave
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from textflowkit.core import whistle as W


def word(text, start, end):
    return W._ParsedWord(text, start, end, None)


def assemble(left, right, boundary=26):
    a = W._Window(0, 0, boundary, 0, boundary + 2)
    b = W._Window(1, boundary, boundary + 26, boundary - 2, boundary + 28)
    owned = W._own_in_core(b, right, is_last=False)
    return W._own_in_core(a, left, is_last=False) + W._reconcile_boundary(
        left, right, boundary=boundary, owned=owned
    )


def test_same_occurrence_kept_by_both_cores_is_emitted_once():
    assert [w.text for w in assemble([word('Let', 25.92, 26)],
                                   [word('Let', 25.92, 26.08)])] == ['Let']


def test_same_occurrence_rejected_by_both_cores_is_not_lost():
    result = assemble([word('19th', 25.6, 26.8)], [word('19th', 25.6, 26.24)])
    assert [w.text for w in result] == ['19th']


def test_large_start_shift_same_end_is_still_one_occurrence():
    assert len(assemble([word('item', 25.2, 26.56)],
                        [word('item', 26.4, 26.56)])) == 1


def test_real_repeated_speech_keeps_both_occurrences():
    words = [word('the', 25.5, 25.8), word('the', 26.1, 26.3)]
    assert [w.text for w in assemble(words, words)] == ['the', 'the']


def test_disagreeing_repeat_counts_are_not_blindly_deduplicated():
    left = [word('the', 25.52, 26.4), word('the', 26.4, 27.68)]
    right = [word('the', 25.52, 27.68)]
    assert len(assemble(left, right)) == 2


def test_same_word_at_separate_times_is_not_a_match():
    assert len(assemble([word('yes', 25, 25.2)], [word('yes', 26.3, 26.5)])) == 2


def test_different_words_at_same_time_are_preserved():
    assert len(assemble([word('cat', 25.9, 26)], [word('cap', 26, 26.1)])) == 2


CASES = json.loads((Path(__file__).parent / 'fixtures/whistle_seam_regressions.json').read_text())['cases']


@pytest.mark.parametrize('case', CASES, ids=lambda c: f'{c["kind"]}-{c["boundary"]}')
def test_real_meeting_boundary_cases(case):
    left = [W._ParsedWord(**w) for w in case['left']]
    right = [W._ParsedWord(**w) for w in case['right']]
    boundary = case['boundary']
    result = assemble(left, right, boundary)
    key = lambda w: (w.text, w.start, w.end)
    keys = [key(w) for w in result]
    targets = [(w['text'], w['start'], w['end'])
               for w in (case['target_left'], case['target_right'])]
    if case['kind'] == 'ambiguous':
        a = W._Window(0, 0, boundary, 0, boundary + 2)
        b = W._Window(1, boundary, boundary + 26, boundary - 2, boundary + 28)
        before = W._own_in_core(a, left, is_last=False) + W._own_in_core(b, right, is_last=False)
        assert keys == [key(w) for w in before]
    else:
        assert sum(keys.count(k) for k in set(targets)) == 1


def test_three_real_repetitions_survive():
    words = [word('I', 25.5, 25.7), word('I', 25.8, 25.95), word('I', 26.1, 26.3)]
    assert [w.text for w in assemble(words, words)] == ['I', 'I', 'I']


def test_public_transcribe_resume_preserves_seam_context(monkeypatch, tmp_path):
    audio = tmp_path / 'speech.wav'
    with wave.open(str(audio), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\0\0' * int(16000 * (2 * W.CORE_SECONDS + 8)))
    monkeypatch.setattr(W, 'ensure_assets', lambda **kw: (tmp_path / 'binary', tmp_path / 'model'))
    outputs = [
        [('Let', W.CORE_SECONDS - .08, W.CORE_SECONDS)],
        [('Let', 1.92, 2.08), ('me', 2.08, 2.4),
         ('19th', W.CORE_SECONDS + 1.6, W.CORE_SECONDS + 2.8)],
        [('19th', 1.6, 2.24), ('again', 2.8, 3.1)],
    ]
    calls = []

    def native(cmd, **kwargs):
        if Path(cmd[-1]).name == 'gap-recovery.wav':
            return SimpleNamespace(returncode=0, timed_out=False, stderr='',
                                   stdout=json.dumps({'text': '', 'words': []}))
        index = int(Path(cmd[-1]).stem.split('-')[-1])
        calls.append(index)
        words = [{'word': t, 'start': a, 'end': b} for t, a, b in outputs[index]]
        return SimpleNamespace(returncode=0, timed_out=False, stderr='',
                               stdout=json.dumps({'text': ' '.join(w['word'] for w in words),
                                                  'language': 'en', 'words': words}))

    monkeypatch.setattr(W, '_run_native', native)
    snapshots = []
    full = W.WhistleEngine().transcribe(audio, on_progress=snapshots.append)
    flatten = lambda t: [(w.text, w.start, w.end) for s in t.segments for w in s.words]
    assert [w[0] for w in flatten(full)] == ['Let', 'me', '19th', 'again']
    for completed in (1, 2):
        saved = json.loads(json.dumps(snapshots[completed - 1]))
        calls.clear()
        resumed = W.WhistleEngine().transcribe(audio, resume_progress=saved)
        assert flatten(resumed) == flatten(full)
        assert calls == list(range(completed, 3))
    incompatible = dict(snapshots[0], schema='textflowkit.whistle.progress/1')
    with pytest.raises(W.WhistleError, match='schema'):
        W.WhistleEngine().transcribe(audio, resume_progress=incompatible)
    with pytest.raises(W.WhistleError, match='policy'):
        W.WhistleEngine().transcribe(audio, resume_progress=dict(
            snapshots[0], policy='core=26;context<=2;max=30;seam=1'))
    for bad in (None, [{'text': 'bad', 'start': 0, 'end': 1}]):
        with pytest.raises(W.WhistleError, match='seam|overlap'):
            W.WhistleEngine().transcribe(audio, resume_progress=dict(snapshots[0], seam_words=bad))
    # Independent windows can move the start of a DIFFERENT following word
    # back by 40 ms. Preserve both words and repair timing, never delete one.
    edge = W.CORE_SECONDS
    outputs[:] = [
        [('So', edge - .6, edge - .599), ('the', edge - .6, edge + .76)],
        [('So', 1.36, 1.361), ('the', 1.36, 2.8)],
        [('done', 2.2, 2.4)],
    ]
    timed = W.WhistleEngine().transcribe(audio)
    sequence = flatten(timed)
    assert [w[0] for w in sequence] == ['So', 'the', 'done']
    assert all(a[1] <= b[1] for a, b in pairwise(sequence))
    assert timed.metadata['boundary_time_repairs_since_resume'] == 1


@pytest.mark.parametrize('left_text, heard_left, accept', [
    ('ready', 'ready', True), ('Debbie', 'Debby', True), ('ready', 'hello', False),
])
def test_gap_recovery_requires_both_timed_anchors_and_preserves_repeats(
    monkeypatch, tmp_path, left_text, heard_left, accept
):
    seen = []
    monkeypatch.setattr(W, '_write_window_wav', lambda source, window, path: seen.append(window))
    raw = [{'word': t, 'start': a, 'end': b} for t, a, b in [
        (heard_left, .1, .4), ('I', 2, 2.2), ('I', 2.3, 2.5), ('Mayor', 7, 7.4),
    ]]
    monkeypatch.setattr(W, '_run_native', lambda *a, **k: SimpleNamespace(
        returncode=0, timed_out=False, stderr='', stdout=json.dumps({'text': 'speech', 'words': raw})
    ))
    right = word('Mayor', 7, 7.4)
    stats = {'attempted': 0, 'accepted': 0, 'words': 0}
    output, recovered = W._recover_short_gaps(
        tmp_path / 'source.wav', [right], preceding=word(left_text, .1, .4),
        base_cmd=['unused'], scratch=tmp_path, timeout=1, check_cancel=None,
        stats=stats, duration=10, repairs=W.Repairs(),
    )
    assert [w.text for w in output] == (['I', 'I', 'Mayor'] if accept else ['Mayor'])
    assert len(recovered) == (2 if accept else 0)
    assert stats == {'attempted': 1, 'accepted': int(accept), 'words': 2 if accept else 0}
    assert all(w.clip_end - w.clip_start <= 15 for w in seen)


def test_long_pause_does_not_trigger_gap_recovery(monkeypatch, tmp_path):
    def forbidden(*a, **k):
        raise AssertionError('A long pause must not start native recovery')
    monkeypatch.setattr(W, '_run_native', forbidden)
    right = word('next', 30, 31)
    stats = {'attempted': 0, 'accepted': 0, 'words': 0}
    output, recovered = W._recover_short_gaps(
        tmp_path / 'source.wav', [right], preceding=word('before', 1, 2),
        base_cmd=['unused'], scratch=tmp_path, timeout=1, check_cancel=None,
        stats=stats, duration=40, repairs=W.Repairs(),
    )
    assert output == [right] and recovered == [] and stats['attempted'] == 0
