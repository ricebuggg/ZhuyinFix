"""ZhuyinFix -- 全域快捷鍵：把「忘記切輸入法」打出來的英數亂碼還原成中文。

  su3cl3            -> 你好
  ji394t au04       -> 我愛吃麵

用法
  python zhuyinfix.py                 常駐（系統匣有圖示），Ctrl+Q 觸發
  python zhuyinfix.py --text "su3cl3" 命令列測試，不常駐

觸發後流程
  1. 有選取文字 -> 轉換選取的那段
     沒選取     -> 自動抓整行，只轉換行尾的英數亂碼那段，前面中文不動；
                   前面的中文會當「上下文」，候選詞含同一行出現過的字會加分
  2. 結果直接貼回覆蓋，原本剪貼簿內容（純文字）會還原
  3. 游標旁跳出提示：原注音 -> 結果；繼續打字/Enter 就消失，Esc 復原成原本英數
  4. 選字：再按一次 Ctrl+Q 進入選字模式（像輸入法）：
       ←→ 移到要改的字、↑↓ 在候選字裡移動、Enter 或數字鍵確定、Space 下一頁、Esc 結束。
       候選 1 永遠是原本的字，一路 Enter = 都不換。換字會直接把剛貼上的那段重貼一次
  5. 被動偵測：沒按快捷鍵、直接按 Enter 時，若剛打的那串很像注音亂碼（≥4 音節、
     全部合法音節、有聲調、詞庫全認得），會先攔下 Enter 自動轉換；再按 Enter 送出、Esc 復原。
     系統匣選單可關閉。

詞庫：libchewing-data (LGPL-2.1) dict/chewing/tsi.csv + word.csv，放同目錄。
     首次載入後會存 lexicon.cache.pkl，之後啟動 <0.2s。
學習：選字結束時，只學「真的被改過的字」和它與相鄰字組成的 2 字詞。同一組注音第一次選
     只加分（20000），第二次選同一結果才置頂（500000）；user_phrases.csv（999999）永遠優先。
     想忘掉某個學錯的詞，刪 learned.csv 那一行。
標點：夾在亂碼裡的 < > ? ! : 會轉成 ，。？！：
"""
import argparse
import csv
import pickle
import re
import math
import os
import queue
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DICT_FILES = ("tsi.csv", "word.csv", "user_phrases.csv", "learned.csv")  # 後面的覆蓋前面
LEARNED = os.path.join(BASE, "learned.csv")   # 選字自動學習（程式自己寫，可手動編輯/刪除）
LEARN_BOOST = 500000.0    # 同一組音節第二次選同一結果 -> 置頂（僅次於 user_phrases 999999）
LEARN_FIRST = 20000.0     # 第一次選 -> 明顯加分但不置頂，避免一次誤選就壓死常用詞
CTX_BONUS = 1.5           # 候選詞含有同一行前文已出現的字 -> log 機率加分
CACHE = os.path.join(BASE, "lexicon.cache.pkl")
HOTKEY = "<ctrl>+q"
MAX_PHRASE = 6           # 詞庫最長詞（音節數）
POPUP_MS = 3000

# ------------------------------------------------------------ 鍵盤對照（大千/標準注音）
KEYMAP = {
    "1": "ㄅ", "q": "ㄆ", "a": "ㄇ", "z": "ㄈ",
    "2": "ㄉ", "w": "ㄊ", "s": "ㄋ", "x": "ㄌ",
    "e": "ㄍ", "d": "ㄎ", "c": "ㄏ",
    "r": "ㄐ", "f": "ㄑ", "v": "ㄒ",
    "5": "ㄓ", "t": "ㄔ", "g": "ㄕ", "b": "ㄖ",
    "y": "ㄗ", "h": "ㄘ", "n": "ㄙ",
    "u": "ㄧ", "j": "ㄨ", "m": "ㄩ",
    "8": "ㄚ", "i": "ㄛ", "k": "ㄜ", ",": "ㄝ",
    "9": "ㄞ", "o": "ㄟ", "l": "ㄠ", ".": "ㄡ",
    "0": "ㄢ", "p": "ㄣ", ";": "ㄤ", "/": "ㄥ",
    "-": "ㄦ",
    "3": "ˇ", "4": "ˋ", "6": "ˊ", "7": "˙",
}
CONSONANTS = set("ㄅㄆㄇㄈㄉㄊㄋㄌㄍㄎㄏㄐㄑㄒㄓㄔㄕㄖㄗㄘㄙ")
MEDIALS = set("ㄧㄨㄩ")
FINALS = set("ㄚㄛㄜㄝㄞㄟㄠㄡㄢㄣㄤㄥㄦ")
TONES = set("ˇˋˊ˙")
ALL_BPMF = CONSONANTS | MEDIALS | FINALS | TONES


def _stage(sym):
    if sym in CONSONANTS:
        return 1
    if sym in MEDIALS:
        return 2
    if sym in FINALS:
        return 3
    return 4  # tone


STANDALONE = set("ㄓㄔㄕㄖㄗㄘㄙ")   # 可以不接韻母單獨成音節的聲母
# 中文模式下要按 Shift 才打得出的標點，在英文模式會變成這些符號；只在夾在注音亂碼裡時才轉
PUNCT = {"<": "，", ">": "。", "?": "？", "!": "！", ":": "："}


def valid_syllable(syl):
    body = syl.rstrip("ˇˋˊ˙")
    if not body:
        return False
    if any(c in MEDIALS or c in FINALS for c in body):
        return True
    return len(body) == 1 and body in STANDALONE


def tokenize(text):
    """英數亂碼 -> token 串。token 是 ('syl', 'ㄋㄧˇ') 或 ('lit', '?')。
    音節在聲調鍵或空白（一聲）結束；若下一個符號的順序倒退（例如聲母接在
    韻母後面卻沒打聲調）也視為一聲結束，所以漏打空白仍能斷音。"""
    tokens, cur, stage = [], "", 0

    def flush():
        nonlocal cur, stage
        if cur:
            tokens.append(("syl", cur))
        cur, stage = "", 0

    for ch in text:
        sym = KEYMAP.get(ch.lower())
        if ch == " ":
            flush()          # 一聲；多餘空白直接吃掉
            continue
        if sym is None:
            flush()
            tokens.append(("lit", ch))
            continue
        st = _stage(sym)
        if st == 4:
            if cur:
                cur += sym
            flush()
            continue
        if cur and st <= stage:
            flush()
        cur += sym
        stage = st
    flush()
    return tokens


# ------------------------------------------------------------ 詞庫
class Lexicon:
    def __init__(self):
        self.table = {}      # tuple(syllables) -> (word, logp)
        self.chars = {}      # syllable -> {char: freq}  同音字表（選字用）
        self.total = 0.0
        self.learned = {}    # (key, word) -> count  選字學習次數
        self.alts = {}       # tuple(syllables) -> [(word, logp), ...] 同音替代詞（前文加分用）
        stamp = tuple((fn, int(os.path.getmtime(os.path.join(BASE, fn))))
                      for fn in DICT_FILES if os.path.exists(os.path.join(BASE, fn)))
        try:
            with open(CACHE, "rb") as f:
                c = pickle.load(f)
            if c.get("stamp") == stamp:
                table, chars, total, learned, alts = c["table"], c["chars"], c["total"], c["learned"], c.get("alts", {})
                self.table, self.chars, self.total, self.learned, self.alts = table, chars, total, learned, alts
                self.unknown = -math.log(self.total or 1) - 5.0
                return
        except Exception:
            self.table, self.chars, self.total, self.learned, self.alts = {}, {}, 0.0, {}, {}
        self._build()
        try:
            with open(CACHE, "wb") as f:
                pickle.dump({"stamp": stamp, "table": self.table, "chars": self.chars, "alts": self.alts,
                             "total": self.total, "learned": self.learned}, f, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass

    def _build(self):
        raw = {}
        for fn in DICT_FILES:
            path = os.path.join(BASE, fn)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8", newline="") as f:
                for row in csv.reader(f):
                    if len(row) < 3 or row[0].startswith("#"):
                        continue
                    word, freq, bpmf = row[0], row[1], row[2]
                    if not word or all(c in ALL_BPMF for c in word):
                        continue
                    try:
                        freq = float(freq)
                    except ValueError:
                        continue
                    key = tuple(bpmf.split())
                    if len(key) != len(word) or len(key) > MAX_PHRASE:
                        continue
                    if fn == "learned.csv":
                        cnt = int(row[3]) if len(row) > 3 and row[3].isdigit() else 2
                        self.learned[(key, word)] = cnt
                        freq = LEARN_BOOST if cnt >= 2 else LEARN_FIRST
                    cands = raw.setdefault(key, {})
                    cands[word] = max(cands.get(word, 0), freq + 1)
                    self.total += freq + 1
                    if len(key) == 1:
                        d = self.chars.setdefault(key[0], {})
                        d[word] = max(d.get(word, 0), freq + 1)
        lt = math.log(self.total or 1)
        for key, cands in raw.items():
            ranked = sorted(cands.items(), key=lambda kv: -kv[1])
            word, f = ranked[0]
            self.table[key] = (word, math.log(f) - lt)
            if len(key) >= 2 and len(ranked) > 1:      # 多字詞才留替代（單字選字走 chars）
                self.alts[key] = [(w, math.log(x) - lt) for w, x in ranked[1:4]]
        self.unknown = -lt - 5.0

    def _bump(self, key, word):
        """學習一筆：第一次 LEARN_FIRST，第二次起 LEARN_BOOST（置頂）。回傳新 count。"""
        cnt = self.learned.get((key, word), 0) + 1
        self.learned[(key, word)] = cnt
        f = LEARN_BOOST if cnt >= 2 else LEARN_FIRST
        lp = math.log(f + 1) - math.log(self.total or 1)
        cur = self.table.get(key)
        if cur is not None and cur[0] == word:
            self.table[key] = (word, max(lp, cur[1]))
        elif cur is None or cur[1] <= lp:
            self.table[key] = (word, lp)
            if cur is not None and len(key) >= 2:     # 舊首選降為替代詞，新詞從替代表移除
                alts = [(w, x) for w, x in self.alts.get(key, []) if w != word]
                alts.append(cur)
                self.alts[key] = sorted(alts, key=lambda t: -t[1])[:3]
        if len(key) == 1:
            d = self.chars.setdefault(key[0], {})
            d[word] = max(d.get(word, 0), f)
        return cnt

    def learn(self, orig, final):
        """選字結束時呼叫：比較原結果與最終結果，只學「使用者真的改過的字」本身，
        以及它和最終相鄰字組成的 2 字詞（相鄰字必須是有注音的中文）。
        同一組音節第二次選同一結果才置頂，一次誤選不會壓死常用詞。
        寫入 learned.csv：詞,詞頻,注音,次數；想忘掉就刪那一行。"""
        changed = [i for i, (a, b) in enumerate(zip(orig, final)) if a[0] != b[0] and b[1]]
        if not changed:
            return
        n = len(final)
        segs = set()
        for i in changed:
            segs.add((i, i + 1))
            if i > 0 and final[i - 1][1]:
                segs.add((i - 1, i + 1))
            if i + 1 < n and final[i + 1][1]:
                segs.add((i, i + 2))
        rows = {}
        for a, b in segs:
            seg = final[a:b]
            word = "".join(c for c, _ in seg)
            key = tuple(sy for _, sy in seg)
            rows[(key, word)] = self._bump(key, word)
        # 重寫整個 learned.csv（檔案很小）
        try:
            lines = ["# ZhuyinFix 自動學習檔（選字時自動寫入）。格式：詞,詞頻,注音,次數。學錯就刪那一行。"]
            for (key, word), cnt in sorted(self.learned.items(), key=lambda kv: -kv[1]):
                f = int(LEARN_BOOST if cnt >= 2 else LEARN_FIRST)
                lines.append(f"{word},{f},{' '.join(key)},{cnt}")
            with open(LEARNED, "w", encoding="utf-8", newline="") as fh:
                fh.write("\n".join(lines) + "\n")
            try:
                os.remove(CACHE)        # 下次啟動重建
            except OSError:
                pass
        except OSError:
            pass
        print(f"  學習: {', '.join(f'{w}x{c}' for (_, w), c in rows.items())}", flush=True)

    def homophones(self, syl, limit=20):
        d = self.chars.get(syl, {})
        return [c for c, _ in sorted(d.items(), key=lambda kv: -kv[1])][:limit]

    def best(self, syls, ctx=None):
        """Viterbi：把音節串切成詞庫裡機率總和最高的詞串。
        ctx = 同一行前文出現過的中文字集合，候選詞含這些字就加分（同句用字傾向重複）。
        回傳 [(word, syl_tuple), ...]。"""
        n = len(syls)
        score = [-math.inf] * (n + 1)
        back = [None] * (n + 1)
        score[0] = 0.0
        for i in range(1, n + 1):
            for j in range(max(0, i - MAX_PHRASE), i):
                if score[j] == -math.inf:
                    continue
                key = tuple(syls[j:i])
                hit = self.table.get(key)
                if hit:
                    w, lp = hit
                    if ctx:
                        if any(c in ctx for c in w):
                            lp += CTX_BONUS
                        for w2, lp2 in self.alts.get(key, ()):
                            if any(c in ctx for c in w2) and lp2 + CTX_BONUS > lp:
                                w, lp = w2, lp2 + CTX_BONUS
                elif i - j == 1:
                    w, lp = syls[j], self.unknown   # 查不到就留注音
                else:
                    continue
                s = score[j] + lp
                if s > score[i]:
                    score[i], back[i] = s, (j, w)
        out, i = [], n
        while i > 0:
            j, w = back[i]
            out.append((w, tuple(syls[j:i])))
            i = j
        return list(reversed(out))


def looks_like_garbage(text, lex):
    """判斷一段英數是不是「忘記切輸入法」打出來的注音。條件全部要成立：
    至少 4 個音節；每個空白分開的段都能拆成合法音節、無大寫；至少一個聲調鍵（一整句
    全一聲不合理）；轉出來沒有殘留注音（每個音節詞庫都認得）。英文句子幾乎過不了第 3、4 關。"""
    t = text.strip()
    letters = [c for c in t if c.isalpha() and c.isascii()]
    if letters and all(c.isupper() for c in letters):
        t = t.lower()
    if len(t) < 6 or any(c.isupper() for c in t) or not re.search(r"[3467]", t):
        return False
    nsyl = 0
    for chunk in t.split():
        tk = tokenize(chunk)
        syls = [v for k, v in tk if k == "syl"]
        lits = [v for k, v in tk if k == "lit"]
        if not syls or any(not valid_syllable(x) for x in syls) or any(v not in PUNCT for v in lits):
            return False
        nsyl += len(syls)
    if nsyl < 4:
        return False
    result, _, _ = convert(t, lex)
    return not any(c in ALL_BPMF for c in result)


def convert(text, lex, ctx=""):
    """回傳 (中文結果, 注音原文, pieces)；pieces = [(char, syllable|None), ...]
    給選字用，literal 的字 syllable 為 None。以空白切段；某段若含不合法音節（例如夾在
    句中的英文單字 ticket -> ㄔㄛ ㄏㄜ ㄍ ㄔ），整段視為英文原樣保留。"""
    ctxset = {c for c in ctx if "\u4e00" <= c <= "\u9fff"} if ctx else None
    letters = [c for c in text if c.isalpha() and c.isascii()]
    if letters and all(c.isupper() for c in letters):
        text = text.lower()          # Caps Lock 開著打的：整段都大寫就當小寫處理
    tokens = []
    for m in re.finditer(r"\S+|\s+", text):
        chunk = m.group()
        if chunk.isspace():
            continue
        tk = tokenize(chunk)
        syls = [v for k, v in tk if k == "syl"]
        has_upper = any(c.isupper() for c in chunk)   # 注音打字不會出現大寫
        if syls and not has_upper and all(valid_syllable(x) for x in syls):
            tokens.extend(("lit", PUNCT.get(v, v)) if k == "lit" else (k, v) for k, v in tk)
        else:
            tokens.append(("lit", chunk))
    pieces, bp, run = [], [], []

    def flush_run():
        if run:
            for word, key in lex.best(run, ctxset):
                for ch, sy in zip(word, key):
                    pieces.append((ch, sy))
            bp.append(" ".join(run))
            run.clear()

    for kind, val in tokens:
        if kind == "syl":
            run.append(val)
        else:
            flush_run()
            pieces.extend((c, None) for c in val)
            bp.append(val)
    flush_run()
    return "".join(c for c, _ in pieces), " ".join(bp), pieces


# ------------------------------------------------------------ 全域快捷鍵
def run_daemon(lex):
    import ctypes
    from pynput import keyboard
    from pynput.keyboard import Controller, Key
    import tkinter as tk

    user32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    for fn, res in ((user32.GetClipboardData, ctypes.c_void_p), (k32.GlobalLock, ctypes.c_void_p),
                    (k32.GlobalAlloc, ctypes.c_void_p), (user32.SetClipboardData, ctypes.c_void_p)):
        fn.restype = res
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    CF_UNICODETEXT = 13

    def _open_clip():
        for _ in range(20):
            if user32.OpenClipboard(None):
                return True
            time.sleep(0.005)
        return False

    def clip_get():
        if not _open_clip():
            return None
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None          # 不是文字（圖片/檔案）-> None，還原時就跳過不清掉
            ptr = k32.GlobalLock(h)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    def clip_set(text):
        if not _open_clip():
            return False
        try:
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = k32.GlobalAlloc(0x0042, len(data))       # GMEM_MOVEABLE | GMEM_ZEROINIT
            if not h:
                return False
            ptr = k32.GlobalLock(h)
            if not ptr:
                k32.GlobalFree(h)
                return False
            ctypes.memmove(ptr, data, len(data))
            k32.GlobalUnlock(h)
            user32.EmptyClipboard()                      # 資料備妥後才清空，失敗不會弄丟原內容
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                k32.GlobalFree(h)
                return False
            return True
        finally:
            user32.CloseClipboard()

    kb = Controller()
    jobs = queue.Queue()
    state = {"hwnd": None, "pieces": [], "popup": None, "orig": [], "prev_text": "", "prev_result": "",
             "auto_enter": True, "enabled": True, "busy": False, "restore_gen": 0}
    clip_lock = threading.Lock()

    def release_mods():
        for k in (Key.ctrl_l, Key.ctrl_r, Key.shift_l, Key.shift_r, Key.ctrl, Key.shift):
            try:
                kb.release(k)
            except Exception:
                pass

    def copy_sel():
        """Ctrl+C 後用剪貼簿序號判斷「複製完成」，不用固定 sleep。沒選取時多數 App 不會改剪貼簿。"""
        state["restore_gen"] += 1            # 取消還在排隊的剪貼簿還原，免得它被當成選取內容
        seq0 = user32.GetClipboardSequenceNumber()
        with kb.pressed(Key.ctrl):
            kb.press("c"); kb.release("c")
        deadline = time.time() + 0.5
        while time.time() < deadline:
            if user32.GetClipboardSequenceNumber() != seq0:
                for _ in range(10):                  # 序號先跳、內容可能晚一點才寫好
                    t = clip_get()
                    if t:
                        return t
                    time.sleep(0.005)
                return clip_get()
            time.sleep(0.002)
        return None

    def paste_text(text, saved):
        with clip_lock:
            clip_set(text)
        with kb.pressed(Key.ctrl):
            kb.press("v"); kb.release("v")
        state["restore_gen"] += 1
        gen = state["restore_gen"]

        def restore():          # Slack 這類 Electron 讀剪貼簿較慢，晚一點再還原
            time.sleep(0.4)
            if saved is not None and state["restore_gen"] == gen:
                with clip_lock:
                    clip_set(saved)
        threading.Thread(target=restore, daemon=True).start()

    def on_hotkey():
        if not state["enabled"]:
            return
        if state["popup"] is not None:
            jobs.put(("select",))
            return
        threading.Thread(target=do_convert, daemon=True).start()

    def do_convert(auto=False):
        if state["busy"]:
            if auto:
                kb.press(Key.enter); kb.release(Key.enter)
            return
        state["busy"] = True
        try:
            _do_convert(auto)
        finally:
            state["busy"] = False

    def _do_convert(auto):
        t0 = time.time()
        state["hwnd"] = user32.GetForegroundWindow()
        try:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(state["hwnd"], buf, 256)
            print(f"[{time.strftime('%H:%M:%S')}] {'自動偵測' if auto else '快捷鍵'} 視窗={buf.value!r}", flush=True)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 快捷鍵 (取視窗名失敗 {e})", flush=True)
        saved = clip_get()
        release_mods()
        ctx = ""
        text = None if auto else copy_sel()
        if not text:
            # 沒選取：先 Shift+Home 抓整行，但只換行尾的英數亂碼那一段，
            # 前面已經打好的中文不動（避免貼上失敗時整行不見）。
            with kb.pressed(Key.shift):
                kb.press(Key.home); kb.release(Key.home)
            time.sleep(0.02)
            text = copy_sel()
            if text:
                m = re.search(r"[\x20-\x7e]+$", text)
                tail = m.group().lstrip() if m else ""
                ctx = text[:m.start()] if m else ""
                if tail and tail != text.strip():
                    kb.press(Key.right); kb.release(Key.right)   # 取消選取，回到行尾
                    with kb.pressed(Key.shift):
                        for _ in range(len(tail)):
                            kb.press(Key.left); kb.release(Key.left)
                    time.sleep(0.02)
                    text = tail
        if not text or not text.strip() or (auto and not looks_like_garbage(text, lex)):
            if saved is not None:
                with clip_lock:
                    clip_set(saved)
            if auto:                                        # 任何中止路徑都要把攔掉的 Enter 補回去
                if text:
                    kb.press(Key.right); kb.release(Key.right)   # 取消選取
                kb.press(Key.enter); kb.release(Key.enter)
            else:
                jobs.put(("show", "（沒有抓到文字）", "", []))
            return
        t1 = time.time()
        result, bpmf, pieces = convert(text, lex, ctx)
        state["prev_text"], state["prev_result"] = text, result
        t2 = time.time()
        paste_text(result, saved)
        print(f"{text!r} -> {bpmf} -> {result}  (抓字 {(t1 - t0) * 1000:.0f}ms / 轉換 {(t2 - t1) * 1000:.0f}ms / 貼上 {(time.time() - t2) * 1000:.0f}ms)", flush=True)
        jobs.put(("show", result, bpmf, pieces, auto))

    def undo_convert():
        """Esc：把剛貼上的結果選起來，換回原本的英數。"""
        saved = clip_get()
        with kb.pressed(Key.shift):
            for _ in range(len(state["prev_result"])):
                kb.press(Key.left); kb.release(Key.left)
        time.sleep(0.03)
        paste_text(state["prev_text"], saved)
        print("  復原", flush=True)

    def replace_char(idx, new_ch):
        """把剛貼上的整段選起來重貼一次（只換第 idx 個字）。"""
        pieces = state["pieces"]
        pieces[idx] = (new_ch, pieces[idx][1])
        new_text = "".join(c for c, _ in pieces)
        saved = clip_get()
        if state["hwnd"] and user32.GetForegroundWindow() != state["hwnd"]:
            user32.SetForegroundWindow(state["hwnd"])
            time.sleep(0.15)
        with kb.pressed(Key.shift):
            for _ in range(len(new_text)):
                kb.press(Key.left); kb.release(Key.left)
        time.sleep(0.03)
        paste_text(new_text, saved)
        state["prev_result"] = new_text
        print(f"  選字 [{idx}] -> {new_ch}: {new_text}", flush=True)

    # --- tk（主執行緒） ---
    root = tk.Tk()
    root.withdraw()

    def _report(exc, val, tb):
        import traceback as _tb
        print("[tk error]", "".join(_tb.format_exception(exc, val, tb)), flush=True)
    root.report_callback_exception = _report
    BG, FG, DIM, HI, SEL, CUR = "#1f2937", "#f9fafb", "#9ca3af", "#374151", "#2563eb", "#4b5563"
    FONT = ("Microsoft JhengHei", 14, "bold")
    SMALL = ("Microsoft JhengHei", 11)
    IDLE_MS = 8000
    PAGE = 9
    sel = {"active": False, "idx": 0, "cands": [], "page": 0, "cur": 0}

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint), ("hwndActive", ctypes.c_void_p),
                    ("hwndFocus", ctypes.c_void_p), ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                    ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p), ("rcCaret", _RECT)]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    try:
        import uiautomation as _uia          # Electron/Chromium 不設 Win32 caret，改走 UI Automation
    except Exception:
        _uia = None

    def _uia_caret():
        """UIA TextPattern：選取範圍(collapsed=插入點)的矩形；contenteditable 常只給整個輸入框 → 退回框的左下角。"""
        if _uia is None:
            return None
        c = _uia.GetFocusedControl()
        if c is None:
            return None
        try:
            tp = c.GetPattern(_uia.PatternId.TextPattern)
            if tp:
                for r in (tp.GetSelection() or []):
                    for rc in (r.GetBoundingRectangles() or []):
                        if rc.width() <= 40 and rc.height() <= 80:
                            return rc.left, rc.bottom, "caret"
        except Exception:
            pass
        rc = c.BoundingRectangle
        if rc and 0 < rc.height() <= 300 and 0 < rc.width() < 3000:
            return rc.left, rc.bottom, "box"
        return None

    def caret_pos():
        """回傳 (x, y, 來源)。來源 caret=插入點左下 / box=輸入框左下 / mouse=滑鼠。"""
        try:
            hwnd = state["hwnd"] or user32.GetForegroundWindow()
            tid = user32.GetWindowThreadProcessId(hwnd, None)
            gti = _GUITHREADINFO(); gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndCaret \
                    and (gti.rcCaret.right > gti.rcCaret.left or gti.rcCaret.bottom > gti.rcCaret.top):
                pt = _POINT(gti.rcCaret.left, gti.rcCaret.bottom)
                user32.ClientToScreen(gti.hwndCaret, ctypes.byref(pt))
                return pt.x, pt.y, "caret"
        except Exception:
            pass
        try:
            t0 = time.time()
            r = _uia_caret()
            if r:
                print(f"[popup] uia {r[2]} {time.time()-t0:.2f}s", flush=True)
                return r
        except Exception as e:
            print("[popup] uia failed:", e, flush=True)
        x, y = root.winfo_pointerxy()
        return x, y, "mouse"

    def close_popup(back=True):
        w = state["popup"]
        state["popup"] = None
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        if sel["active"] and state["orig"]:
            try:
                lex.learn(state["orig"], state["pieces"])
            except Exception as e:
                print("[learn]", e, flush=True)
        state["orig"] = []
        sel["active"] = False

    def show_popup(result, bpmf, pieces, auto=False):
        close_popup(back=False)
        state["pieces"] = list(pieces)
        state["orig"] = list(pieces)
        sel.update(active=False, idx=0, cands=[], page=0, cur=0)
        win = tk.Toplevel(root)
        state["popup"] = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        x, y, src = caret_pos()
        if src == "mouse":
            x, y = x + 16, y + 20
        else:
            y += 6
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x, y = max(0, min(x, sw - 320)), max(0, min(y, sh - 160))
        win.geometry(f"+{x}+{y}")
        win.update_idletasks()
        try:
            hwnd = user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
            GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOPMOST = -20, 0x08000000, 0x00000008
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        except Exception as e:
            print("[popup] noactivate failed:", e, flush=True)
        timer = {"id": None}

        def arm():
            if timer["id"]:
                win.after_cancel(timer["id"])
            if not sel["active"]:
                timer["id"] = win.after(IDLE_MS, lambda: close_popup(back=True))
        arm()

        tk.Label(win, text=bpmf or " ", fg=DIM, bg=BG, font=SMALL,
                 justify="left").pack(anchor="w", padx=10, pady=(6, 0))
        row = tk.Frame(win, bg=BG)
        row.pack(anchor="w", padx=8, pady=(0, 4))
        cand = tk.Frame(win, bg=BG)
        cand.pack(anchor="w", padx=8, pady=(0, 4))
        hint = tk.Label(win, text=("已自動轉換 · Enter 送出 · Esc 復原 · Ctrl+Q 選字" if auto
                                   else "再按 Ctrl+Q 進入選字 · Esc 復原"), fg=DIM, bg=BG,
                        font=("Microsoft JhengHei", 8))
        hint.pack(anchor="w", padx=10, pady=(0, 6))
        labels = []
        for i, (ch, sy) in enumerate(pieces):
            l = tk.Label(row, text=ch, fg=FG if sy else DIM, bg=BG, font=FONT, padx=3,
                         cursor="hand2" if sy else "arrow")
            l.pack(side="left")
            if sy:
                l.bind("<Button-1>", lambda e, i=i: (enter_select(), move_to(i)))
            labels.append(l)

        def selectable(i):
            return 0 <= i < len(state["pieces"]) and state["pieces"][i][1] is not None

        def render_cands():
            for w in cand.winfo_children():
                w.destroy()
            cs = sel["cands"]
            if not cs:
                tk.Label(cand, text="（沒有其他同音字）", fg=DIM, bg=BG, font=SMALL).pack(anchor="w")
                return
            pages = (len(cs) + PAGE - 1) // PAGE
            base = sel["page"] * PAGE
            for n, c in enumerate(cs[base:base + PAGE]):
                bg = SEL if n == sel["cur"] else BG
                l = tk.Label(cand, text=f"{n + 1}  {c}", fg=FG, bg=bg, font=SMALL,
                             padx=8, pady=1, anchor="w", width=8, cursor="hand2")
                l.pack(anchor="w")
                l.bind("<Button-1>", lambda e, n=n: pick(n))
            if pages > 1:
                tk.Label(cand, text=f"{sel['page'] + 1}/{pages}  Space/PgDn 下一頁", fg=DIM, bg=BG,
                         font=("Microsoft JhengHei", 8)).pack(anchor="w")

        def move_to(i):
            arm()
            if not selectable(i):
                return
            for l in labels:
                l.config(bg=BG)
            labels[i].config(bg=CUR)
            ch, sy = state["pieces"][i]
            # 目前這個字永遠排第 1：按 Enter/1 就是「保留、跳下一字」，不會被強迫換字
            sel.update(idx=i, cands=[ch] + [c for c in lex.homophones(sy, 60) if c != ch], page=0, cur=0)
            render_cands()

        def step(d):
            i = sel["idx"] + d
            while 0 <= i < len(state["pieces"]):
                if selectable(i):
                    move_to(i)
                    return
                i += d

        def pick(n):
            cs = sel["cands"]
            k = sel["page"] * PAGE + n
            if k >= len(cs) or sel.get("busy"):
                return
            ch, idx = cs[k], sel["idx"]
            if ch == state["pieces"][idx][0]:       # 選了原字：不重貼，直接跳下一字
                arm()
                jobs.put(("advance",))
                return
            labels[idx].config(text=ch)
            sel["busy"] = True
            arm()

            def worker():
                try:
                    replace_char(idx, ch)
                finally:
                    sel["busy"] = False
                    jobs.put(("advance",))
            threading.Thread(target=worker, daemon=True).start()

        def enter_select():
            arm()
            hint.config(text="←→ 換字位  ↑↓ 選候選  Enter 保留/確定  數字 選字  Space 下頁  Esc 結束")
            if not sel["active"]:
                sel["active"] = True
                first = next((i for i in range(len(state["pieces"])) if selectable(i)), None)
                if first is None:
                    close_popup(back=True)
                    return
                move_to(first)

        def on_key(ks, char):
            arm()
            if ks == "Escape":
                close_popup(back=True)
            elif ks == "Return":
                if sel["cands"]:
                    pick(sel["cur"])
                else:
                    step(1)
            elif ks == "Left":
                step(-1)
            elif ks == "Right":
                step(1)
            elif ks == "Down":
                if sel["cands"]:
                    sel["cur"] = min(sel["cur"] + 1, min(PAGE, len(sel["cands"]) - sel["page"] * PAGE) - 1)
                    render_cands()
            elif ks == "Up":
                sel["cur"] = max(0, sel["cur"] - 1)
                render_cands()
            elif ks in ("space", "Next"):
                pages = (len(sel["cands"]) + PAGE - 1) // PAGE
                if pages > 1:
                    sel["page"] = (sel["page"] + 1) % pages
                    sel["cur"] = 0
                    render_cands()
            elif ks == "Prior":
                pages = (len(sel["cands"]) + PAGE - 1) // PAGE
                if pages > 1:
                    sel["page"] = (sel["page"] - 1) % pages
                    sel["cur"] = 0
                    render_cands()
            elif char and char.isdigit() and char != "0":
                pick(int(char) - 1)
            elif ks == "Tab":
                pick(sel["cur"])
        def advance():
            nxt = next((i for i in range(sel["idx"] + 1, len(state["pieces"])) if selectable(i)), None)
            if nxt is None:
                close_popup(back=True)
            else:
                move_to(nxt)

        win.enter_select = enter_select
        win.on_key = on_key
        win.advance = advance

    def poll():
        try:
            while True:
                msg = jobs.get_nowait()
                if msg[0] == "show":
                    show_popup(*msg[1:])
                elif msg[0] == "select":
                    w = state["popup"]
                    if w is not None:
                        w.enter_select()
                elif msg[0] == "close":
                    if not sel["active"]:
                        close_popup(back=True)
                elif msg[0] == "quit":
                    root.destroy()
                    return
                elif msg[0] == "undo":
                    if state["popup"] is not None and not sel["active"]:
                        close_popup(back=False)
                        threading.Thread(target=undo_convert, daemon=True).start()
                elif msg[0] == "advance":
                    w = state["popup"]
                    if w is not None and sel["active"]:
                        w.advance()
                elif msg[0] == "key":
                    w = state["popup"]
                    if w is not None and sel["active"]:
                        w.on_key(msg[1], msg[2])
        except queue.Empty:
            pass
        root.after(100, poll)

    VK = {0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x20: "space",
          0x0D: "Return", 0x1B: "Escape", 0x21: "Prior", 0x22: "Next", 0x09: "Tab"}
    VK.update({0x30 + d: str(d) for d in range(10)})

    MODS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}
    # 被動偵測用：記錄使用者最近連續打的英數（vkCode -> 字元）
    TYPED = {0x30 + d: str(d) for d in range(10)}
    TYPED.update({0x41 + i: chr(0x61 + i) for i in range(26)})
    TYPED.update({0xBA: ";", 0xBC: ",", 0xBE: ".", 0xBF: "/", 0xBD: "-", 0x20: " "})
    typed = {"buf": "", "hwnd": None}
    CHEAP = re.compile(r"^[a-z0-9;,./\- ]{6,}$")   # TYPED 表記的是小寫，Caps Lock 也一樣     # hook 內只做便宜檢查；Viterbi 放到 worker

    imm32 = ctypes.windll.imm32
    imm32.ImmGetDefaultIMEWnd.restype = ctypes.c_void_p
    imm32.ImmGetDefaultIMEWnd.argtypes = [ctypes.c_void_p]
    user32.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                           ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]

    def ime_chinese(hwnd):
        """前景視窗的輸入法現在是不是中文模式。是的話使用者打的英數是注音鍵、Enter 是在選字，
        不能攔。查不到（純英文鍵盤/無 IME）就當英文模式。"""
        try:
            ime = imm32.ImmGetDefaultIMEWnd(hwnd)
            if not ime:
                return False
            res = ctypes.c_size_t(0)
            ok = user32.SendMessageTimeoutW(ime, 0x0283, 0x0005, 0, 0x0002, 30, ctypes.byref(res))  # WM_IME_CONTROL, IMC_GETOPENSTATUS
            return bool(ok) and res.value != 0
        except Exception:
            return False

    def maybe_auto(buf):
        """worker 執行緒：完整判斷；不是亂碼就把被攔掉的 Enter 補回去。"""
        if looks_like_garbage(buf, lex):
            do_convert(True)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] 自動偵測 放行（不像亂碼）: {buf[-40:]!r}", flush=True)
            kb.press(Key.enter); kb.release(Key.enter)

    def key_filter(msg, data):
        """選字模式中攔截導航/數字鍵，不讓它們進到目前的 App（輸入法的做法）。
        非選字模式：使用者繼續打字/按 Enter 就把小框收掉（不攔鍵）；Esc 復原。
        沒有小框：Enter 時若剛打的那串像注音亂碼，攔下 Enter 先自動轉換。"""
        down = msg in (0x100, 0x104)
        ctrl = user32.GetAsyncKeyState(0x11) & 0x8000
        shift = user32.GetAsyncKeyState(0x10) & 0x8000
        if data.vkCode == 0x51 and ctrl and state["enabled"]:
            # Ctrl+Q 是我們的快捷鍵：整個吞掉，不能讓 App 看到（Slack 的 Ctrl+Q = 結束程式）
            key_listener.suppress_event()
        if state["popup"] is None and not sel["active"]:
            if down and not ctrl:
                vk = data.vkCode
                fg = user32.GetForegroundWindow()
                if fg != typed["hwnd"]:                 # 換視窗就從頭記
                    typed["hwnd"], typed["buf"] = fg, ""
                if vk == 0x08:                          # Backspace
                    typed["buf"] = typed["buf"][:-1]
                elif vk == 0x0D:                        # Enter
                    buf, typed["buf"] = typed["buf"], ""
                    if state["enabled"] and state["auto_enter"] and not shift and not state["busy"] \
                            and CHEAP.match(buf) and re.search(r"[3467]", buf) and not ime_chinese(fg):
                        threading.Thread(target=maybe_auto, args=(buf,), daemon=True).start()
                        key_listener.suppress_event()
                elif vk in TYPED and not shift:
                    typed["buf"] = (typed["buf"] + TYPED[vk])[-200:]
                elif vk not in MODS:
                    typed["buf"] = ""
            elif down and ctrl and data.vkCode not in MODS:
                typed["buf"] = ""                        # Ctrl+任何鍵（貼上、Ctrl+Enter…）都重來
            return True
        if state["popup"] is not None and not sel["active"]:
            if down and data.vkCode not in MODS:
                if data.vkCode == 0x1B:                 # Esc -> 復原
                    jobs.put(("undo",))
                    key_listener.suppress_event()
                if not (data.vkCode == 0x51 and ctrl):   # Ctrl+Q（快捷鍵）留給選字
                    jobs.put(("close",))
            return True
        if not sel["active"] or sel.get("busy"):
            return True
        name = VK.get(data.vkCode)
        if name is None:
            return True
        if msg in (0x100, 0x104):           # WM_KEYDOWN / WM_SYSKEYDOWN
            jobs.put(("key", name, name if name.isdigit() else ""))
        key_listener.suppress_event()

    key_listener = keyboard.Listener(win32_event_filter=key_filter)
    key_listener.daemon = True
    key_listener.start()

    hk = keyboard.GlobalHotKeys({HOTKEY: on_hotkey})
    hk.daemon = True
    hk.start()
    print(f"ZhuyinFix 常駐中，快捷鍵 {HOTKEY}（Ctrl+C 結束）", flush=True)
    tray = start_tray(root, state, jobs)
    root.after(100, poll)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        if tray:
            try:
                tray.stop()
            except Exception:
                pass


def start_tray(root, state, jobs):
    """系統匣圖示：開/關、Enter 自動偵測、看 log、重啟、結束。沒裝 pystray 就略過。"""
    try:
        import pystray
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[tray] pystray/pillow 未安裝，略過系統匣", flush=True)
        return None

    def make_icon(on):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 62, 62), 14, fill=(37, 99, 235, 255) if on else (107, 114, 128, 255))
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\msjhbd.ttc", 40)
        except OSError:
            font = ImageFont.load_default()
        d.text((32, 34), "注", fill="white", font=font, anchor="mm")
        return img

    def toggle_enabled(icon, item):
        state["enabled"] = not state["enabled"]
        icon.icon = make_icon(state["enabled"])
        icon.title = f"ZhuyinFix {'啟用中' if state['enabled'] else '已暫停'}"

    def toggle_auto(icon, item):
        state["auto_enter"] = not state["auto_enter"]

    def open_log(icon, item):
        lp = os.path.join(BASE, "zhuyinfix.log")
        os.startfile(lp if os.path.exists(lp) else BASE)

    def open_dir(icon, item):
        os.startfile(BASE)

    def restart(icon, item):
        import subprocess
        # 先結束自己，再由 cmd 延遲 1 秒啟動新實例，避免兩份同時掛鍵盤 hook
        subprocess.Popen(["cmd.exe", "/c", "timeout /t 1 /nobreak >nul && wscript.exe \"" +
                          os.path.join(BASE, "ZhuyinFix.vbs") + "\""], cwd=BASE,
                         creationflags=0x08000000)       # CREATE_NO_WINDOW
        quit_(icon, item)

    def quit_(icon, item):
        icon.stop()
        jobs.put(("quit",))                # tk 不是 thread-safe，交給主執行緒 destroy

    menu = pystray.Menu(
        pystray.MenuItem(lambda i: "啟用（Ctrl+Q）", toggle_enabled, checked=lambda i: state["enabled"]),
        pystray.MenuItem("Enter 自動偵測亂碼", toggle_auto, checked=lambda i: state["auto_enter"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("開啟 log", open_log),
        pystray.MenuItem("開啟資料夾（改 user_phrases / learned）", open_dir),
        pystray.MenuItem("重新啟動", restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("結束", quit_),
    )
    icon = pystray.Icon("ZhuyinFix", make_icon(True), "ZhuyinFix 啟用中", menu)
    icon.run_detached()
    return icon


def _setup_logging():
    """pythonw 沒有 stdout/stderr；把輸出和所有未捕捉例外寫到 zhuyinfix.log，方便查為何閃退。"""
    import traceback
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zhuyinfix.log")
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 1_000_000:
            os.remove(log_path)
    except OSError:
        pass
    if sys.stdout is None or sys.stderr is None:
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 啟動 pid={os.getpid()} ===", flush=True)

    def hook(exc_type, exc, tb):
        print(f"[{time.strftime('%H:%M:%S')}] 未捕捉例外:", flush=True)
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        sys.stderr.flush()
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def main():
    _setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="直接轉換這段文字後結束")
    args = ap.parse_args()
    t0 = time.time()
    lex = Lexicon()
    print(f"詞庫載入 {len(lex.table)} 條，{time.time() - t0:.2f}s", file=sys.stderr)
    if args.text is not None:
        result, bpmf, pieces = convert(args.text, lex)
        print(bpmf)
        print(result)
        for ch, sy in pieces:
            if sy:
                print(f"  {ch} {sy}: {' '.join(lex.homophones(sy, 10))}")
        return
    try:
        run_daemon(lex)
    except BaseException:
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        print(f"=== {time.strftime('%H:%M:%S')} 結束 ===", flush=True)


if __name__ == "__main__":
    main()
