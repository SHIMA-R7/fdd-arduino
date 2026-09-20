# MIDIファイルを、フロッピードライブ用の単音メロディに変換する
# 和音は「一番高い音」を採用。ドラム(ch10)は除外。
import struct, sys, json

def read_var(d, i):
    v = 0
    while True:
        b = d[i]; i += 1
        v = (v << 7) | (b & 0x7F)
        if not (b & 0x80): return v, i

def parse(path):
    d = open(path, 'rb').read()
    assert d[:4] == b'MThd', 'MIDIではありません'
    _, fmt, ntrk, div = struct.unpack('>IHHH', d[4:14])
    i = 14
    tempo_map = [(0, 500000)]   # (tick, us/qn)
    events = []                 # (tick, on/off, ch, note)
    for t in range(ntrk):
        assert d[i:i+4] == b'MTrk'
        ln = struct.unpack('>I', d[i+4:i+8])[0]
        j = i + 8; end = j + ln; tick = 0; status = 0
        while j < end:
            dt, j = read_var(d, j); tick += dt
            b = d[j]
            if b & 0x80: status = b; j += 1
            ev = status & 0xF0; ch = status & 0x0F
            if ev in (0x80, 0x90):
                note, vel = d[j], d[j+1]; j += 2
                if ev == 0x90 and vel > 0: events.append((tick, 1, ch, note))
                else: events.append((tick, 0, ch, note))
            elif ev in (0xA0, 0xB0, 0xE0):
                j += 2
            elif ev in (0xC0, 0xD0):
                j += 1
            elif status == 0xFF:
                mt = d[j]; j += 1
                ln2, j = read_var(d, j)
                if mt == 0x51:
                    tempo_map.append((tick, struct.unpack('>I', b'\x00' + d[j:j+3])[0]))
                j += ln2
            elif status in (0xF0, 0xF7):
                ln2, j = read_var(d, j); j += ln2
            else:
                j += 1
        i = end
    return div, sorted(tempo_map), sorted(events)

def tick_to_ms(tick, div, tempo_map):
    ms = 0.0; last_tick = 0; tempo = tempo_map[0][1]
    for tt, tv in tempo_map:
        if tt >= tick: break
        ms += (tt - last_tick) * tempo / div / 1000.0
        last_tick = tt; tempo = tv
    ms += (tick - last_tick) * tempo / div / 1000.0
    return ms

def main(path, out, drop_channels=(9,)):
    div, tempo_map, events = parse(path)
    active = {}       # note -> count
    timeline = []     # (ms, 最高音 or None)
    for tick, on, ch, note in events:
        if ch in drop_channels: continue
        if on: active[note] = active.get(note, 0) + 1
        else:
            if note in active:
                active[note] -= 1
                if active[note] <= 0: del active[note]
        top = max(active) if active else None
        ms = tick_to_ms(tick, div, tempo_map)
        if timeline and abs(timeline[-1][0] - ms) < 1e-6:
            timeline[-1] = (ms, top)
        else:
            timeline.append((ms, top))

    # 連続する同じ音をまとめる
    notes = []
    for k, (ms, top) in enumerate(timeline):
        if notes and notes[-1][1] == top:
            continue
        notes.append([ms, top])
    seq = []
    for k in range(len(notes)):
        start = notes[k][0]
        end = notes[k+1][0] if k+1 < len(notes) else start + 400
        dur = int(round(end - start))
        if dur <= 0: continue
        n = notes[k][1]
        if n is None:
            seq.append((0, dur))
        else:
            f = 440.0 * (2 ** ((n - 69) / 12.0))
            while f > 1100: f /= 2      # 高すぎる音はオクターブ下げ
            while f < 55:   f *= 2
            seq.append((int(round(f)), dur))
    json.dump(seq, open(out, 'w'))
    total = sum(d for _, d in seq)
    print(f'音符数={len(seq)}  演奏時間={total/1000:.1f}秒  分解能={div}  テンポ変更={len(tempo_map)}')
    print('最初の12個:', seq[:12])

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
