"""準確率測試：抽詞庫真實詞組句 -> 反推應打的英數 -> 轉回 -> 逐字比對。"""
import csv, random, sys, os
sys.path.insert(0, os.path.dirname(__file__))
import zhuyinfix as Z

INV = {v: k for k, v in Z.KEYMAP.items()}
def to_keys(bpmf_syls):
    out = []
    for syl in bpmf_syls:
        k = "".join(INV[c] for c in syl)
        if syl[-1] not in Z.TONES:
            k += " "
        out.append(k)
    return "".join(out).rstrip() if False else "".join(out)

rows = []
with open("tsi.csv", encoding="utf-8", newline="") as f:
    for r in csv.reader(f):
        if len(r) < 3 or r[0].startswith("#"): continue
        try: fq = float(r[1])
        except: continue
        w, b = r[0], r[2].split()
        if 2 <= len(w) <= 4 and len(b) == len(w) and fq >= 50 and all(c in Z.ALL_BPMF for c in "".join(b)):
            rows.append((w, b, fq))
random.seed(7)
weights = [fq for _, _, fq in rows]
lex = Z.Lexicon()
N = 500
tot_c = ok_c = ok_s = 0
fails = []
for _ in range(N):
    picks = random.choices(rows, weights=weights, k=random.randint(2, 4))
    sent = "".join(w for w, _, _ in picks)
    syls = [s for _, b, _ in picks for s in b]
    keys = to_keys(syls)
    res, _, _ = Z.convert(keys, lex)
    tot_c += len(sent)
    ok_c += sum(a == b for a, b in zip(sent, res)) if len(res) == len(sent) else 0
    if res == sent: ok_s += 1
    elif len(fails) < 12: fails.append((keys, sent, res))
print(f"句數 {N}  整句正確 {ok_s/N:.1%}  逐字正確 {ok_c/tot_c:.1%}")
for k, s, r in fails: print(f"  {k!r:40} 期望 {s}  得到 {r}")
