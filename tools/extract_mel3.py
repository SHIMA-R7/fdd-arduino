"""MIDI -> フロッピー用の単音メロディ

主旋律チャンネルを基本にし、そのチャンネルが「一定時間以上」休んでいる区間だけ、
代役チャンネルの音を使う。音符の合間の短い隙間では切り替えない。
"""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import midi2fdd as M

path   = sys.argv[1]
out    = sys.argv[2]
MAIN   = int(sys.argv[3]) if len(sys.argv) > 3 else 0
SUBS   = [int(x) for x in sys.argv[4].split(',')] if len(sys.argv) > 4 else [1, 3, 4, 13]
FMAX   = int(sys.argv[5]) if len(sys.argv) > 5 else 880
GAP_TAKEOVER = 1500   # 主旋律がこれ以上休んだら代役を使う（ms）
FMIN = 60
GAP_FILL = 120
MIN_NOTE = 45

div, tempo, ev = M.parse(path)

def timeline(channels):
    """(ms, 最高音 or None) の列を返す"""
    act = {c: {} for c in channels}
    tl = []
    for tick, on, ch, note in ev:
        if ch not in act:
            continue
        a = act[ch]
        if on:
            a[note] = a.get(note, 0) + 1
        else:
            if note in a:
                a[note] -= 1
                if a[note] <= 0:
                    del a[note]
        top = None
        for c in channels:
            if act[c]:
                t = max(act[c])
                if top is None or t > top:
                    top = t
        ms = M.tick_to_ms(tick, div, tempo)
        if tl and abs(tl[-1][0] - ms) < 1e-6:
            tl[-1] = (ms, top)
        else:
            tl.append((ms, top))
    return tl

main_tl = timeline([MAIN])
sub_tl  = timeline(SUBS)

def note_at(tl, ms):
    """時刻msにおける音（線形探索を避けるため呼び出し側で順に進める）"""
    raise NotImplementedError

# 主旋律の「長い休み」の区間を求める
gaps = []
# 主旋律が始まる前（イントロ）も代役に任せる
if main_tl and main_tl[0][0] >= GAP_TAKEOVER:
    gaps.append((0.0, main_tl[0][0]))
start = None
for i, (ms, n) in enumerate(main_tl):
    if n is None and start is None:
        start = ms
    elif n is not None and start is not None:
        if ms - start >= GAP_TAKEOVER:
            gaps.append((start, ms))
        start = None
if start is not None:
    gaps.append((start, 10 ** 9))

def in_gap(ms):
    for a, b in gaps:
        if a <= ms < b:
            return True
    return False

# 全時刻を統合し、区間ごとにどちらの音を使うか決める
points = sorted(set([m for m, _ in main_tl] + [m for m, _ in sub_tl] + [a for a, _ in gaps] + [b for _, b in gaps if b < 10 ** 9]))
mi = si = 0
cur_main = cur_sub = None
seq_raw = []
for ms in points:
    while mi < len(main_tl) and main_tl[mi][0] <= ms:
        cur_main = main_tl[mi][1]; mi += 1
    while si < len(sub_tl) and sub_tl[si][0] <= ms:
        cur_sub = sub_tl[si][1]; si += 1
    pick = cur_sub if in_gap(ms) else cur_main
    if seq_raw and seq_raw[-1][1] == pick:
        continue
    seq_raw.append([ms, pick])

seq = []
for k in range(len(seq_raw)):
    st = seq_raw[k][0]
    en = seq_raw[k + 1][0] if k + 1 < len(seq_raw) else st + 500
    dur = int(round(en - st))
    if dur <= 0:
        continue
    n = seq_raw[k][1]
    if n is None:
        seq.append([0, dur])
    else:
        f = 440.0 * (2 ** ((n - 69) / 12.0))
        while f > FMAX:
            f /= 2
        while f < FMIN:
            f *= 2
        seq.append([int(round(f)), dur])

merged = []
for f, d in seq:
    if f == 0 and merged and d <= GAP_FILL:
        merged[-1][1] += d; continue
    if merged and merged[-1][0] == f:
        merged[-1][1] += d; continue
    merged.append([f, d])

out_seq = []
for f, d in merged:
    if out_seq and d < MIN_NOTE:
        out_seq[-1][1] += d; continue
    if out_seq and out_seq[-1][0] == f:
        out_seq[-1][1] += d; continue
    out_seq.append([f, d])

json.dump(out_seq, open(out, 'w'))
print('音符数=%d  演奏時間=%.1f秒' % (len(out_seq), sum(d for _, d in out_seq) / 1000))
print('主旋律ch%d / 代役ch%s' % (MAIN, SUBS))
print('代役に渡した区間(秒):', [(round(a / 1000, 1), round(min(b, sum(d for _, d in out_seq)) / 1000, 1)) for a, b in gaps])
