"""ZhuyinFix -- 全域快捷鍵：把「忘記切輸入法」打出來的英數亂碼還原成中文。

  su3cl3            -> 你好
  ji394t au04       -> 我愛吃麵

用法
  python zhuyinfix.py                 常駐，Ctrl+Shift+Z 觸發
  python zhuyinfix.py --text "su3cl3" 命令列測試，不常駐

觸發後流程
  1. 有選取文字 -> 轉換選取的那段
     沒選取     -> 自動 Shift+Home 選到行首再轉
  2. 結果直接貼回覆蓋，原本剪貼簿內容（純文字）會還原
  3. 游標旁跳出提示：原注音 -> 結果，8 秒沒動作自動消失
  4. 選字：再按一次 Ctrl+Shift+Z 進入選字模式（像輸入法）：
       ←→ 移到要改的字、↑↓ 在候選字裡移動、Enter 或數字鍵確定、
       Space 下一頁、Esc 結束。確定後自動跳到下一個字，最後一個字選完自動結束。
       換字會直接把剛貼上的那段重貼一次

詞庫：libchewing-data (LGPL-2.1) dict/chewing/tsi.csv + word.csv，放同目錄。
學習：選字模式每選一次字，就把該字與前後 1~2 字組成的詞以高詞頻寫進 learned.csv，
      下次同樣注音直接出那個字（像輸入法的自動學習）。想忘掉某個學錯的詞，刪那一行。
"""
import argparse
import csv
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
LEARN_BOOST = 500000.0
HOTKEY = "<ctrl>+<shift>+z"
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
                    if freq + 1 > raw.get(key, (None, -1))[1]:
                        raw[key] = (word, freq + 1)
                    self.total += freq + 1
                    if len(key) == 1:
                        d = self.chars.setdefault(key[0], {})
                        d[word] = max(d.get(word, 0), freq + 1)
        lt = math.log(self.total or 1)
        for key, (word, f) in raw.items():
            self.table[key] = (word, math.log(f) - lt)
        self.unknown = -lt - 5.0

    def learn(self, pieces, idx):
        """使用者在第 idx 個字選了新字：把包含該字、長度 1~3 的所有片段（例如
        船 / 上船 / 船了 / 上船了）以高詞頻寫進 learned.csv 並更新記憶體，
        下次同樣注音直接選它。user_phrases.csv（999999）仍優先於學習值。"""
        if not pieces[idx][1]:
            return
        n = len(pieces)
        seen = set()
        try:
            with open(LEARNED, encoding="utf-8") as f:
                seen = {l.strip() for l in f}
        except OSError:
            pass
        lines = []
        for a in range(max(0, idx - 2), idx + 1):
            for b in range(idx + 1, min(n, a + 3) + 1):
                seg = pieces[a:b]
                if not all(sy for _, sy in seg):
                    continue
                word = "".join(c for c, _ in seg)
                key = tuple(sy for _, sy in seg)
                lp = math.log(LEARN_BOOST + 1) - math.log(self.total or 1)
                cur = self.table.get(key)
                if cur is None or cur[1] < lp or cur[0] != word and cur[1] <= lp:
                    self.table[key] = (word, lp)
                if len(key) == 1:
                    self.chars.setdefault(key[0], {})[word] = LEARN_BOOST
                line = f"{word},{int(LEARN_BOOST)},{' '.join(key)}"
                if line not in seen:
                    lines.append(line)
                    seen.add(line)
        if lines:
            try:
                with open(LEARNED, "a", encoding="utf-8", newline="") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError:
                pass

    def homophones(self, syl, limit=20):
        d = self.chars.get(syl, {})
        return [c for c, _ in sorted(d.items(), key=lambda kv: -kv[1])][:limit]

    def best(self, syls):
        """Viterbi：把音節串切成詞庫裡機率總和最高的詞串。
        回傳 [(word, syl_tuple), ...]。"""
        n = len(syls)
        score = [-math.inf] * (n + 1)
        back = [None] * (n + 1)
        score[0] = 0.0
        for i in range(1, n + 1):
            for j in range(max(0, i - MAX_PHRASE), i):
                if score[j] == -math.inf:
                    continue
                hit = self.table.get(tuple(syls[j:i]))
                if hit:
                    w, lp = hit
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


def convert(text, lex):
    """回傳 (中文結果, 注音原文, pieces)；pieces = [(char, syllable|None), ...]
    給選字用，literal 的字 syllable 為 None。以空白切段；某段若含不合法音節（例如夾在
    句中的英文單字 ticket -> ㄔㄛ ㄏㄜ ㄍ ㄔ），整段視為英文原樣保留。"""
    tokens = []
    for m in re.finditer(r"\S+|\s+", text):
        chunk = m.group()
        if chunk.isspace():
            continue
        tk = tokenize(chunk)
        syls = [v for k, v in tk if k == "syl"]
        has_upper = any(c.isupper() for c in chunk)   # 注音打字不會出現大寫
        if syls and not has_upper and all(valid_syllable(x) for x in syls):
            tokens.extend(tk)
        else:
            tokens.append(("lit", chunk))
    pieces, bp, run = [], [], []

    def flush_run():
        if run:
            for word, key in lex.best(run):
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
    import pyperclip
    from pynput import keyboard
    from pynput.keyboard import Controller, Key
    import tkinter as tk

    user32 = ctypes.windll.user32
    kb = Controller()
    jobs = queue.Queue()
    SENTINEL = "\u200b__ZHUYINFIX__\u200b"
    state = {"hwnd": None, "pieces": [], "popup": None}

    def release_mods():
        for k in (Key.ctrl_l, Key.ctrl_r, Key.shift_l, Key.shift_r, Key.ctrl, Key.shift):
            try:
                kb.release(k)
            except Exception:
                pass

    def copy_sel():
        pyperclip.copy(SENTINEL)
        time.sleep(0.05)
        with kb.pressed(Key.ctrl):
            kb.press("c"); kb.release("c")
        for _ in range(12):
            time.sleep(0.04)
            t = pyperclip.paste()
            if t != SENTINEL:
                return t
        return None

    def paste_text(text, saved):
        pyperclip.copy(text)
        time.sleep(0.05)
        with kb.pressed(Key.ctrl):
            kb.press("v"); kb.release("v")
        time.sleep(0.25)
        try:
            pyperclip.copy(saved)
        except Exception:
            pass

    def on_hotkey():
        if state["popup"] is not None:
            jobs.put(("select",))
            return
        threading.Thread(target=do_convert, daemon=True).start()

    def do_convert():
        state["hwnd"] = user32.GetForegroundWindow()
        try:
            saved = pyperclip.paste()
        except Exception:
            saved = ""
        release_mods()
        time.sleep(0.08)
        text = copy_sel()
        if not text:
            with kb.pressed(Key.shift):
                kb.press(Key.home); kb.release(Key.home)
            time.sleep(0.08)
            text = copy_sel()
        if not text or not text.strip():
            pyperclip.copy(saved)
            jobs.put(("show", "（沒有抓到文字）", "", []))
            return
        result, bpmf, pieces = convert(text, lex)
        paste_text(result, saved)
        print(f"{text!r} -> {bpmf} -> {result}", flush=True)
        jobs.put(("show", result, bpmf, pieces))

    def replace_char(idx, new_ch):
        """把剛貼上的整段選起來重貼一次（只換第 idx 個字）。"""
        pieces = state["pieces"]
        pieces[idx] = (new_ch, pieces[idx][1])
        new_text = "".join(c for c, _ in pieces)
        try:
            saved = pyperclip.paste()
        except Exception:
            saved = ""
        if state["hwnd"] and user32.GetForegroundWindow() != state["hwnd"]:
            user32.SetForegroundWindow(state["hwnd"])
            time.sleep(0.15)
        with kb.pressed(Key.shift):
            for _ in range(len(new_text)):
                kb.press(Key.left); kb.release(Key.left)
        time.sleep(0.05)
        paste_text(new_text, saved)
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
    IDLE_MS = 20000
    PAGE = 9
    sel = {"active": False, "idx": 0, "cands": [], "page": 0, "cur": 0}

    def close_popup(back=True):
        w = state["popup"]
        state["popup"] = None
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        sel["active"] = False

    def show_popup(result, bpmf, pieces):
        close_popup(back=False)
        state["pieces"] = list(pieces)
        sel.update(active=False, idx=0, cands=[], page=0, cur=0)
        win = tk.Toplevel(root)
        state["popup"] = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        x, y = root.winfo_pointerxy()
        win.geometry(f"+{x + 16}+{y + 20}")
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
        hint = tk.Label(win, text="再按 Ctrl+Shift+Z 進入選字", fg=DIM, bg=BG,
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
            sel.update(idx=i, cands=[c for c in lex.homophones(sy, 60) if c != ch], page=0, cur=0)
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
            labels[idx].config(text=ch)
            sel["busy"] = True
            arm()
            pieces_now = list(state["pieces"])
            pieces_now[idx] = (ch, pieces_now[idx][1])
            lex.learn(pieces_now, idx)

            def worker():
                try:
                    replace_char(idx, ch)
                finally:
                    sel["busy"] = False
                    jobs.put(("advance",))
            threading.Thread(target=worker, daemon=True).start()

        def enter_select():
            arm()
            hint.config(text="←→ 換字位  ↑↓ 選候選  Enter/數字 確定  Space 下頁  Esc 結束")
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

    def key_filter(msg, data):
        """選字模式中攔截導航/數字鍵，不讓它們進到目前的 App（輸入法的做法）。"""
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
    root.after(100, poll)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="直接轉換這段文字後結束")
    args = ap.parse_args()
    t0 = time.time()
    lex = Lexicon()
    print(f"詞庫載入 {len(lex.table)} 條，{time.time() - t0:.1f}s", file=sys.stderr)
    if args.text is not None:
        result, bpmf, pieces = convert(args.text, lex)
        print(bpmf)
        print(result)
        for ch, sy in pieces:
            if sy:
                print(f"  {ch} {sy}: {' '.join(lex.homophones(sy, 10))}")
        return
    run_daemon(lex)


if __name__ == "__main__":
    main()
