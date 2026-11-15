"""Convert a standard MIDI file to monophonic FDD notes."""

from collections import Counter

from mido import MidiFile

from fdd_core import MUSIC_GUI_MAX_HZ


def load_midi(path, max_hz=MUSIC_GUI_MAX_HZ):
    midi = MidiFile(filename=path)
    if midi.type == 2 or midi.ticks_per_beat <= 0:
        raise ValueError("このMIDIのトラック形式または時間形式には対応していません。")

    held = Counter()
    notes = []
    elapsed = 0.0
    previous_ms = 0
    for message in midi:
        elapsed += message.time * 1000
        current_ms = round(elapsed)
        duration = current_ms - previous_ms
        if duration:
            pitch = max((note for _, note in held), default=None)
            if notes and notes[-1][0] == pitch:
                notes[-1] = (pitch, notes[-1][1] + duration)
            else:
                notes.append((pitch, duration))
            previous_ms = current_ms
        if message.type in ("note_on", "note_off") and message.channel != 9:
            key = (message.channel, message.note)
            if message.type == "note_on" and message.velocity:
                held[key] += 1
            elif held[key] > 1:
                held[key] -= 1
            else:
                held.pop(key, None)

    if notes and notes[-1][0] is None:
        notes.pop()
    if not notes:
        raise ValueError("演奏できる旋律がMIDIにありません。")
    # Transpose the whole melody together to preserve its intervals.
    peak = max(pitch for pitch, _ in notes if pitch is not None)
    octaves = 0
    while round(440 * 2 ** ((peak - 69 - 12 * octaves) / 12)) > max_hz:
        octaves += 1
    return [(0 if pitch is None else max(20, round(440 * 2 ** ((pitch - 69 - 12 * octaves) / 12))), duration)
            for pitch, duration in notes]
